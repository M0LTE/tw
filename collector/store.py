"""The delta log and the SQLite database built from it.

Storage model
-------------
Polling gives us ~20k work orders per run (~22 MB of JSON). Committing that
daily would add gigabytes to git within a year, so we commit only the *delta*:
new faults in full, changed faults as a patch of the changed fields, and a
tombstone for faults that dropped out of the feed. That is typically a few
hundred lines a day.

``data/deltas/<snapshot>.ndjson.gz`` is therefore the source of truth, and
``faults.db`` is a cache that ``rebuild`` regenerates from scratch. Anyone can
check our numbers by replaying the same files.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import model, sources

log = logging.getLogger(__name__)

DELTA_SUFFIX = ".ndjson.gz"

# 1: work order timestamps decoded as UTC, which put them an hour ahead during
#    British Summer Time (the feed publishes UK local time with a UTC label).
# 2: those fields corrected on ingest. Version 1 deltas are corrected on replay
#    rather than rewritten, so the committed log stays append-only.
FORMAT_VERSION = 2

# Salesforce business fields on the work order layers, all affected by the v1 bug.
V1_LOCAL_TIME_FIELDS = ("raised_at", "closure_at", "repair_complete_at", "last_modified_at")


# --------------------------------------------------------------------------- #
# Delta log
# --------------------------------------------------------------------------- #


@dataclass
class Change:
    """What changed for one kind of record (work orders, or public reports)."""

    appeared: dict[str, dict[str, Any]] = field(default_factory=dict)
    changed: dict[str, dict[str, Any]] = field(default_factory=dict)
    reappeared: dict[str, dict[str, Any]] = field(default_factory=dict)
    resolved: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.appeared or self.changed or self.reappeared or self.resolved)


@dataclass
class Delta:
    """One collection run's worth of change, across every kind of record."""

    observed_at: str
    source_counts: dict[str, int] = field(default_factory=dict)
    kinds: dict[str, Change] = field(default_factory=dict)

    def for_kind(self, kind: str) -> Change:
        return self.kinds.setdefault(kind, Change())

    @property
    def total(self) -> int:
        return sum(self.source_counts.values())

    def is_empty(self) -> bool:
        return all(change.is_empty() for change in self.kinds.values())

    def tally(self) -> dict[str, dict[str, int]]:
        return {
            kind: {
                "appeared": len(c.appeared),
                "changed": len(c.changed),
                "reappeared": len(c.reappeared),
                "resolved": len(c.resolved),
            }
            for kind, c in self.kinds.items()
        }


def delta_path(root: Path, observed_at: str) -> Path:
    """File name for a snapshot, e.g. 2026-08-04T06:11:00+00:00 -> 20260804T061100Z."""
    stamp = observed_at.replace("-", "").replace(":", "")
    stamp = stamp.replace("+0000", "Z").removesuffix("Z") + "Z"
    return root / f"{stamp}{DELTA_SUFFIX}"


def write_delta(root: Path, delta: Delta) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = delta_path(root, delta.observed_at)
    lines: list[str] = [
        json.dumps(
            {
                "op": "meta",
                "v": FORMAT_VERSION,
                "observed_at": delta.observed_at,
                "source_counts": delta.source_counts,
            },
            sort_keys=True,
        )
    ]

    def emit(op: str, record_id: str, kind: str, fields: dict | None = None) -> None:
        entry: dict[str, Any] = {"op": op, "id": record_id}
        if fields is not None:
            entry["f"] = fields
        # Work orders are the default kind, so older deltas without a "k" still
        # replay correctly.
        if kind != DEFAULT_KIND:
            entry["k"] = kind
        lines.append(json.dumps(entry, sort_keys=True))

    for kind in sorted(delta.kinds):
        change = delta.kinds[kind]
        for record_id, record in sorted(change.appeared.items()):
            emit("add", record_id, kind, record)
        for record_id, record in sorted(change.reappeared.items()):
            emit("back", record_id, kind, record)
        for record_id, patch in sorted(change.changed.items()):
            emit("set", record_id, kind, patch)
        for record_id in sorted(change.resolved):
            emit("gone", record_id, kind)

    body = ("\n".join(lines) + "\n").encode()
    # mtime=0 so identical content produces an identical file, keeping git quiet.
    with path.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as fh:
        fh.write(body)
    return path


def delta_files(root: Path) -> list[Path]:
    """Every delta, oldest first. Names are ISO-ish so lexical order is chronological."""
    if not root.exists():
        return []
    return sorted(root.glob(f"*{DELTA_SUFFIX}"))


def _migrate(entry: dict, version: int) -> dict:
    """Bring an older delta entry up to the current format."""
    if version >= FORMAT_VERSION:
        return entry
    fields = entry.get("f")
    if version < 2 and fields and entry.get("k", DEFAULT_KIND) == sources.WORK_ORDER:
        for key in V1_LOCAL_TIME_FIELDS:
            if fields.get(key):
                fields[key] = model.local_wall_clock_to_utc(
                    dt.datetime.fromisoformat(fields[key])
                ).isoformat(timespec="seconds")
    return entry


def read_delta(path: Path) -> Iterator[dict]:
    """Entries from one delta file, migrated to the current format."""
    version = FORMAT_VERSION
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("op") == "meta":
                version = int(entry.get("v", 1))
                yield entry
            else:
                yield _migrate(entry, version)


# --------------------------------------------------------------------------- #
# SQLite
# --------------------------------------------------------------------------- #

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


@dataclass(frozen=True)
class TableSpec:
    """How one kind of record maps onto the database."""

    table: str
    fields: tuple[str, ...]
    tracked: tuple[str, ...]
    live_column: str
    gone_column: str
    events_table: str | None


SPECS: dict[str, TableSpec] = {
    sources.WORK_ORDER: TableSpec(
        table="faults",
        fields=model.FIELDS,
        tracked=model.TRACKED_FIELDS,
        live_column="is_open",
        gone_column="resolved_at",
        events_table="fault_events",
    ),
    # Closed work orders share the fault schema; the interesting field is
    # `status`, which is Completed or Canceled rather than a lifecycle stage.
    sources.CLOSED: TableSpec(
        table="closed_faults",
        fields=model.FIELDS,
        tracked=("status", "closure_at", "repair_complete_at"),
        live_column="is_listed",
        gone_column="delisted_at",
        events_table=None,
    ),
    # Reports have no status to progress through, so there is nothing worth
    # recording beyond when we first and last saw them.
    sources.REPORT: TableSpec(
        table="reports",
        fields=model.REPORT_FIELDS,
        tracked=model.REPORT_TRACKED_FIELDS,
        live_column="is_current",
        gone_column="disappeared_at",
        events_table=None,
    ),
}

DEFAULT_KIND = sources.WORK_ORDER


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def current_state(conn: sqlite3.Connection, kind: str = DEFAULT_KIND) -> dict[str, dict[str, Any]]:
    """Latest known record for everything of ``kind`` still present in the feed."""
    spec = SPECS[kind]
    columns = ", ".join(spec.fields)
    rows = conn.execute(
        f"SELECT id, {columns} FROM {spec.table} WHERE {spec.live_column} = 1"
    )
    return {row["id"]: {k: row[k] for k in spec.fields} for row in rows}


def known_ids(conn: sqlite3.Connection, kind: str = DEFAULT_KIND) -> set[str]:
    """Every id of ``kind`` we have ever seen, so returns are not mistaken for arrivals."""
    return {row[0] for row in conn.execute(f"SELECT id FROM {SPECS[kind].table}")}


EVENT_SQL = (
    "INSERT INTO fault_events (fault_id, observed_at, kind, field, old_value, new_value) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


def _apply_delta(conn: sqlite3.Connection, entries: Iterable[dict]) -> None:
    observed_at = ""
    counts = {"appeared": 0, "changed": 0, "resolved": 0, "reappeared": 0}
    source_counts: dict[str, int] = {}

    for entry in entries:
        op = entry.get("op")

        if op == "meta":
            observed_at = entry["observed_at"]
            source_counts = entry.get("source_counts", {})
            continue

        if not observed_at:
            raise ValueError("delta is missing its meta line")

        record_id = entry["id"]
        spec = SPECS[entry.get("k", DEFAULT_KIND)]
        columns = list(spec.fields)

        def event(kind: str, field_name: str | None = None, old=None, new=None) -> None:
            if spec.events_table:
                conn.execute(EVENT_SQL, (record_id, observed_at, kind, field_name, old, new))

        if op in ("add", "back"):
            record = entry["f"]
            values = [record.get(c) for c in columns]
            conn.execute(
                f"INSERT INTO {spec.table} (id, {', '.join(columns)}, first_seen_at, last_seen_at, {spec.live_column}) "
                f"VALUES (?, {', '.join('?' * len(columns))}, ?, ?, 1) "
                "ON CONFLICT(id) DO UPDATE SET last_seen_at = excluded.last_seen_at",
                [record_id, *values, observed_at, observed_at],
            )
            if op == "back":
                # Seen again after we thought it had gone: either it was
                # reopened, or it briefly dropped out of the feed.
                conn.execute(
                    f"UPDATE {spec.table} SET {spec.live_column} = 1, {spec.gone_column} = NULL, "
                    "reappearances = reappearances + 1, last_seen_at = ?, "
                    + ", ".join(f"{c} = ?" for c in columns)
                    + " WHERE id = ?",
                    [observed_at, *values, record_id],
                )
                event("reappeared")
                counts["reappeared"] += 1
            else:
                event("appeared", new=record.get("status"))
                counts["appeared"] += 1

        elif op == "set":
            patch = entry["f"]
            previous = conn.execute(
                f"SELECT {', '.join(patch)} FROM {spec.table} WHERE id = ?", (record_id,)
            ).fetchone()
            assignments = ", ".join(f"{k} = ?" for k in patch)
            conn.execute(
                f"UPDATE {spec.table} SET {assignments}, last_seen_at = ? WHERE id = ?",
                [*patch.values(), observed_at, record_id],
            )
            for key, value in patch.items():
                if key in spec.tracked:
                    old = previous[key] if previous is not None else None
                    event(
                        "changed",
                        key,
                        None if old is None else str(old),
                        None if value is None else str(value),
                    )
            counts["changed"] += 1

        elif op == "gone":
            conn.execute(
                f"UPDATE {spec.table} SET {spec.live_column} = 0, {spec.gone_column} = ? WHERE id = ?",
                (observed_at, record_id),
            )
            event("resolved")
            counts["resolved"] += 1

        else:
            raise ValueError(f"unknown delta op {op!r}")

    if observed_at:
        conn.execute(
            "INSERT OR REPLACE INTO snapshots "
            "(observed_at, total, appeared, changed, resolved, reappeared, source_counts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                observed_at,
                sum(source_counts.values()),
                counts["appeared"],
                counts["changed"],
                counts["resolved"],
                counts["reappeared"],
                json.dumps(source_counts, sort_keys=True),
            ),
        )


def rebuild(db_path: Path, deltas_root: Path) -> sqlite3.Connection:
    """Recreate the database from the delta log."""
    if db_path.exists():
        db_path.unlink()
    for extra in (db_path.with_suffix(db_path.suffix + "-wal"), db_path.with_suffix(db_path.suffix + "-shm")):
        extra.unlink(missing_ok=True)

    conn = connect(db_path)
    files = delta_files(deltas_root)
    log.info("replaying %d delta file(s)", len(files))
    for path in files:
        _apply_delta(conn, read_delta(path))
    conn.commit()
    conn.execute("VACUUM")
    conn.commit()
    return conn

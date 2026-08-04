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

import gzip
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import model

log = logging.getLogger(__name__)

DELTA_SUFFIX = ".ndjson.gz"
FORMAT_VERSION = 1


# --------------------------------------------------------------------------- #
# Delta log
# --------------------------------------------------------------------------- #


@dataclass
class Delta:
    """One collection run's worth of change."""

    observed_at: str
    source_counts: dict[str, int] = field(default_factory=dict)
    appeared: dict[str, dict[str, Any]] = field(default_factory=dict)
    changed: dict[str, dict[str, Any]] = field(default_factory=dict)
    reappeared: dict[str, dict[str, Any]] = field(default_factory=dict)
    resolved: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.source_counts.values())

    def is_empty(self) -> bool:
        return not (self.appeared or self.changed or self.reappeared or self.resolved)


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
    for fault_id, record in sorted(delta.appeared.items()):
        lines.append(json.dumps({"op": "add", "id": fault_id, "f": record}, sort_keys=True))
    for fault_id, record in sorted(delta.reappeared.items()):
        lines.append(json.dumps({"op": "back", "id": fault_id, "f": record}, sort_keys=True))
    for fault_id, patch in sorted(delta.changed.items()):
        lines.append(json.dumps({"op": "set", "id": fault_id, "f": patch}, sort_keys=True))
    for fault_id in sorted(delta.resolved):
        lines.append(json.dumps({"op": "gone", "id": fault_id}, sort_keys=True))

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


def read_delta(path: Path) -> Iterator[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


# --------------------------------------------------------------------------- #
# SQLite
# --------------------------------------------------------------------------- #

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def current_state(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Latest known record for every fault still present in the feed."""
    columns = ", ".join(model.FIELDS)
    rows = conn.execute(f"SELECT id, {columns} FROM faults WHERE is_open = 1")
    return {row["id"]: {k: row[k] for k in model.FIELDS} for row in rows}


def _apply_delta(conn: sqlite3.Connection, entries: Iterable[dict]) -> None:
    observed_at = ""
    counts = {"appeared": 0, "changed": 0, "resolved": 0, "reappeared": 0}
    source_counts: dict[str, int] = {}
    columns = list(model.FIELDS)

    insert_sql = (
        f"INSERT INTO faults (id, {', '.join(columns)}, first_seen_at, last_seen_at, is_open) "
        f"VALUES (?, {', '.join('?' * len(columns))}, ?, ?, 1) "
        "ON CONFLICT(id) DO UPDATE SET last_seen_at = excluded.last_seen_at"
    )
    event_sql = (
        "INSERT INTO fault_events (fault_id, observed_at, kind, field, old_value, new_value) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    )

    for entry in entries:
        op = entry.get("op")

        if op == "meta":
            observed_at = entry["observed_at"]
            source_counts = entry.get("source_counts", {})
            continue

        if not observed_at:
            raise ValueError("delta is missing its meta line")
        fault_id = entry["id"]

        if op in ("add", "back"):
            record = entry["f"]
            values = [record.get(c) for c in columns]
            conn.execute(insert_sql, [fault_id, *values, observed_at, observed_at])
            if op == "back":
                # Seen again after we thought it was resolved: Thames Water
                # either reopened it or briefly dropped it from the feed.
                conn.execute(
                    "UPDATE faults SET is_open = 1, resolved_at = NULL, "
                    "reappearances = reappearances + 1, last_seen_at = ?, "
                    + ", ".join(f"{c} = ?" for c in columns)
                    + " WHERE id = ?",
                    [observed_at, *values, fault_id],
                )
                conn.execute(event_sql, (fault_id, observed_at, "reappeared", None, None, None))
                counts["reappeared"] += 1
            else:
                conn.execute(
                    event_sql, (fault_id, observed_at, "appeared", None, None, record.get("status"))
                )
                counts["appeared"] += 1

        elif op == "set":
            patch = entry["f"]
            previous = conn.execute(
                f"SELECT {', '.join(patch)} FROM faults WHERE id = ?", (fault_id,)
            ).fetchone()
            assignments = ", ".join(f"{k} = ?" for k in patch)
            conn.execute(
                f"UPDATE faults SET {assignments}, last_seen_at = ? WHERE id = ?",
                [*patch.values(), observed_at, fault_id],
            )
            for key, value in patch.items():
                if key in model.TRACKED_FIELDS:
                    old = previous[key] if previous is not None else None
                    conn.execute(
                        event_sql,
                        (
                            fault_id,
                            observed_at,
                            "changed",
                            key,
                            None if old is None else str(old),
                            None if value is None else str(value),
                        ),
                    )
            counts["changed"] += 1

        elif op == "gone":
            conn.execute(
                "UPDATE faults SET is_open = 0, resolved_at = ? WHERE id = ?",
                (observed_at, fault_id),
            )
            conn.execute(event_sql, (fault_id, observed_at, "resolved", None, None, None))
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

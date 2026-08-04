"""Poll Thames Water's fault map and append a delta to the log.

Run with ``python -m collector.collect``. Idempotent enough to run by hand: if
nothing has changed since the last run, no delta file is written.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

from . import arcgis, model, store
from .sources import SOURCES, WORK_ORDER, Source

log = logging.getLogger("collector")

ROOT = Path(__file__).resolve().parent.parent
DELTAS = ROOT / "data" / "deltas"
DB = ROOT / "data" / "faults.db"

# If a poll returns dramatically fewer rows than we hold, something is wrong at
# the source (a partial republish, an outage). Treating that as "20,000 faults
# fixed overnight" would poison the history, so we refuse to write the delta.
MIN_RETAINED_FRACTION = 0.5


def fetch(source: Source) -> dict[str, dict]:
    """All current records from one layer, keyed by record id."""
    layer_id = arcgis.resolve_layer_id(source.service_url, source.layer_name)
    layer_url = f"{source.service_url}/{layer_id}"
    log.info("fetching %s (%s)", source.label, layer_url)

    normalise = model.normalise if source.kind == WORK_ORDER else model.normalise_report
    records: dict[str, dict] = {}
    skipped = 0
    for feature in arcgis.query_all(layer_url):
        normalised = normalise(feature, source.key)
        if normalised is None:
            skipped += 1
            continue
        record_id, record = normalised
        records[record_id] = record

    log.info("  %d records (%d without an id)", len(records), skipped)
    return records


def classify(
    change: store.Change,
    live: dict[str, dict],
    previous: dict[str, dict],
    known: set[str],
) -> store.Change:
    """Split a poll into new / changed / returned / departed."""
    for record_id, record in live.items():
        if record_id in previous:
            patch = model.diff(previous[record_id], record)
            if patch:
                change.changed[record_id] = patch
        elif record_id in known:
            change.reappeared[record_id] = record
        else:
            change.appeared[record_id] = record

    change.resolved = sorted(set(previous) - set(live))
    return change


def build_delta(
    observed_at: str,
    live: dict[str, dict],
    source_counts: dict[str, int],
    previous: dict[str, dict],
    known_ids: set[str],
    kind: str = WORK_ORDER,
) -> store.Delta:
    delta = store.Delta(observed_at=observed_at, source_counts=source_counts)
    classify(delta.for_kind(kind), live, previous, known_ids)
    return delta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deltas", type=Path, default=DELTAS, help="directory holding the delta log"
    )
    parser.add_argument("--db", type=Path, default=DB, help="path to the SQLite database")
    parser.add_argument(
        "--rebuild-only",
        action="store_true",
        help="rebuild the database from the existing delta log without polling",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="write the delta even if the poll looks truncated",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", stream=sys.stderr
    )

    conn = store.rebuild(args.db, args.deltas)
    previous = {kind: store.current_state(conn, kind) for kind in store.SPECS}
    known = {kind: store.known_ids(conn, kind) for kind in store.SPECS}
    for kind in store.SPECS:
        log.info(
            "replayed %s: %d current, %d ever seen", kind, len(previous[kind]), len(known[kind])
        )

    if args.rebuild_only:
        conn.close()
        return 0

    observed_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    delta = store.Delta(observed_at=observed_at)
    live: dict[str, dict[str, dict]] = {kind: {} for kind in store.SPECS}

    for source in SOURCES:
        records = fetch(source)
        if not records:
            log.error(
                "%s returned no records; aborting rather than recording mass closure", source.key
            )
            conn.close()
            return 1
        delta.source_counts[source.key] = len(records)
        live[source.kind].update(records)

    for kind, spec in store.SPECS.items():
        before = previous[kind]
        if before:
            retained = len(set(before) & set(live[kind])) / len(before)
            if retained < MIN_RETAINED_FRACTION and not args.force:
                log.error(
                    "only %.1f%% of the %d current %s records came back; refusing to write a "
                    "delta (re-run with --force if the drop is genuine)",
                    retained * 100,
                    len(before),
                    kind,
                )
                conn.close()
                return 1
        classify(delta.for_kind(kind), live[kind], before, known[kind])

    for kind, tally in delta.tally().items():
        log.info(
            "%-11s %d new, %d changed, %d reappeared, %d gone",
            kind + ":",
            tally["appeared"],
            tally["changed"],
            tally["reappeared"],
            tally["resolved"],
        )

    if delta.is_empty():
        log.info("nothing changed; not writing a delta")
        conn.close()
        return 0

    path = store.write_delta(args.deltas, delta)
    log.info("wrote %s (%.1f KiB)", path.relative_to(ROOT), path.stat().st_size / 1024)

    conn.close()
    # Replay from scratch so the database on disk is exactly what the log says.
    conn = store.rebuild(args.db, args.deltas)
    open_now = conn.execute("SELECT count(*) FROM faults WHERE is_open = 1").fetchone()[0]
    reports_now = conn.execute("SELECT count(*) FROM reports WHERE is_current = 1").fetchone()[0]
    log.info("database rebuilt: %d open faults, %d live reports", open_now, reports_now)
    conn.close()

    # Consumed by the workflow to write the commit message, so it reports every
    # kind of record: a run that only picked up new public reports still moved
    # something, and saying "+0 new" would hide that.
    print(json.dumps({
        "observed_at": observed_at,
        "open": open_now,
        "reports": reports_now,
        "changes": delta.tally(),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

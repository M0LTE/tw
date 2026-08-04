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
from .sources import SOURCES, Source

log = logging.getLogger("collector")

ROOT = Path(__file__).resolve().parent.parent
DELTAS = ROOT / "data" / "deltas"
DB = ROOT / "data" / "faults.db"

# If a poll returns dramatically fewer rows than we hold, something is wrong at
# the source (a partial republish, an outage). Treating that as "20,000 faults
# fixed overnight" would poison the history, so we refuse to write the delta.
MIN_RETAINED_FRACTION = 0.5


def fetch(source: Source) -> dict[str, dict]:
    """All current records from one layer, keyed by fault id."""
    layer_id = arcgis.resolve_layer_id(source.service_url, source.layer_name)
    layer_url = f"{source.service_url}/{layer_id}"
    log.info("fetching %s (%s)", source.label, layer_url)

    records: dict[str, dict] = {}
    skipped = 0
    for feature in arcgis.query_all(layer_url):
        normalised = model.normalise(feature, source.key)
        if normalised is None:
            skipped += 1
            continue
        fault_id, record = normalised
        records[fault_id] = record

    log.info("  %d records (%d without an id)", len(records), skipped)
    return records


def build_delta(
    observed_at: str,
    live: dict[str, dict],
    source_counts: dict[str, int],
    previous: dict[str, dict],
    known_ids: set[str],
) -> store.Delta:
    delta = store.Delta(observed_at=observed_at, source_counts=source_counts)

    for fault_id, record in live.items():
        if fault_id in previous:
            patch = model.diff(previous[fault_id], record)
            if patch:
                delta.changed[fault_id] = patch
        elif fault_id in known_ids:
            delta.reappeared[fault_id] = record
        else:
            delta.appeared[fault_id] = record

    delta.resolved = sorted(set(previous) - set(live))
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
    previous = store.current_state(conn)
    known_ids = {row[0] for row in conn.execute("SELECT id FROM faults")}
    log.info("replayed state: %d open, %d ever seen", len(previous), len(known_ids))

    if args.rebuild_only:
        conn.close()
        return 0

    live: dict[str, dict] = {}
    source_counts: dict[str, int] = {}
    for source in SOURCES:
        records = fetch(source)
        if not records:
            log.error("%s returned no records; aborting rather than recording mass closure", source.key)
            conn.close()
            return 1
        source_counts[source.key] = len(records)
        live.update(records)

    if previous:
        retained = len(set(previous) & set(live)) / len(previous)
        if retained < MIN_RETAINED_FRACTION and not args.force:
            log.error(
                "only %.1f%% of the %d known open faults came back; refusing to write a delta "
                "(re-run with --force if the drop is genuine)",
                retained * 100,
                len(previous),
            )
            conn.close()
            return 1

    observed_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    delta = build_delta(observed_at, live, source_counts, previous, known_ids)

    log.info(
        "delta: %d new, %d changed, %d reappeared, %d resolved (total live %d)",
        len(delta.appeared),
        len(delta.changed),
        len(delta.reappeared),
        len(delta.resolved),
        delta.total,
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
    log.info("database rebuilt: %d open faults", open_now)
    conn.close()

    summary = {
        "observed_at": observed_at,
        "open": open_now,
        "appeared": len(delta.appeared),
        "changed": len(delta.changed),
        "reappeared": len(delta.reappeared),
        "resolved": len(delta.resolved),
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

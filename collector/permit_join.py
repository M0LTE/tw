"""What Thames Water's own street works permits say about their deadlines.

The point of #6. A work order sitting at *Repair Planning* for months raises a
question the fault feed alone cannot answer: when Thames Water dug up the road,
did the work run past the dates they themselves proposed? A permit overrun is a
missed deadline they set, against a highway authority with fixed penalty notices
behind it.

Two separate questions live here, and they did not come out the same way.

## The permit-level question, which works

*Of the works Thames Water finished, how many finished by the date they applied
to finish by?* This needs no join at all — both dates are on the same permit
record — so there is no matching error and no chance baseline to argue about.
This is what gets published.

## The fault-to-permit join, which does not

*Which fault on the map does this permit correspond to?* Both sides publish
British National Grid eastings and northings, so this is a metric distance in a
shared projection rather than the fuzzy address text #1 had to cope with. That
sounded like firmer ground and is not, for a reason the null model had to show:
Thames Water digs where its pipes are and its faults are where its pipes are, so
permits and faults cluster in the same streets whether or not any given pair is
related. Permuting which fault sits at which location leaves most of the
matching standing.

``--join`` reports it. Nothing derived from it is published — see ``match()``.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import gzip
import json
import logging
import math
import random
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("permit_join")

ROOT = Path(__file__).resolve().parent.parent
PERMITS = ROOT / "data" / "permits"
DB = ROOT / "data" / "faults.db"

# Permit dates are wall-clock commitments to a highway authority, so lateness is
# counted in London calendar days. The archive stores them in UTC, where a
# deadline of "end of 19 July" appears as 2026-07-19T23:00:00Z — midnight BST.
LONDON = ZoneInfo("Europe/London")

# Metres. A permit records where the road is opened, a work order where the
# fault is; on the same street those differ by a frontage or two.
RADIUS_M = 50
# A permit for a fault should not predate the fault being raised by much, and
# work planned long afterwards is likely unrelated.
BEFORE_DAYS = 2
AFTER_DAYS = 90


def _local(timestamp: str) -> dt.datetime:
    return dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(LONDON)


def held_months() -> set[str]:
    """The months we actually hold an archive for, as YYYY-MM."""
    return {Path(p).name.split(".")[0] for p in glob.glob(str(PERMITS / "*.ndjson.gz"))}


def load_permits() -> list[dict]:
    """Every committed monthly extract, one row per permit (not per event)."""
    by_permit: dict[str, dict] = {}
    for path in sorted(glob.glob(str(PERMITS / "*.ndjson.gz"))):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                record = json.loads(line)
                if record.get("op") == "meta":
                    continue
                ref = record.get("permit_reference_number")
                if not ref:
                    continue
                # Events arrive in lifecycle order; later ones carry the actual
                # dates and final status, so let them win. This also means a
                # permit extended by an approved variation is measured against
                # its revised end date, which is the fair comparison — an
                # extension that was granted is not an overrun.
                existing = by_permit.get(ref)
                if existing is None:
                    by_permit[ref] = dict(record)
                else:
                    for key, value in record.items():
                        if value is not None:
                            existing[key] = value
    return list(by_permit.values())


def days_late(permit: dict) -> int | None:
    """London calendar days between the proposed end and the actual end.

    Counted in whole days rather than fractions because the commitment is to a
    day, not an instant: most proposed ends land on midnight, so a job finishing
    at 09:00 the following morning is one day late, not "0.4 days over". Reading
    the raw difference as a duration understates it and produces a distribution
    of sub-day overruns that looks like a rounding artefact.

    None when the work has no recorded end — still running, cancelled, or
    finished in a month whose archive is not committed.
    """
    proposed, actual = permit.get("proposed_end_date"), permit.get("actual_end_date_time")
    if not proposed or not actual:
        return None
    # The last permitted calendar day is the day containing the instant one
    # second before the deadline.
    deadline = (_local(proposed) - dt.timedelta(seconds=1)).date()
    return (_local(actual).date() - deadline).days


def _duration_band(days: float) -> str:
    for limit, label in ((1, "under a day"), (2, "1-2 days"), (5, "2-5 days"),
                         (10, "5-10 days"), (30, "10-30 days")):
        if days < limit:
            return label
    return "over 30 days"


def by_start_month(finished: list[tuple[dict, int]], held: set[str] | None = None) -> list[dict]:
    """Late rate per starting month, against the observation window it had.

    The censoring correction, computed rather than asserted. A work still
    running when the last archive closes cannot be measured, so months near the
    end of the held range under-count long works — and long works overrun most.

    `short` is the control. Works finishing inside two days are fully observed
    whatever the window, so their rate isolates genuine month-to-month variation
    from the artefact; the *gap* between the two columns is what the missing
    observation window is worth. Computed here rather than written into the
    page so it cannot go stale as months are added.

    A month whose own archive we do not hold is dropped, not merely thinned. The
    only works visible from such a month are those whose completion landed in a
    later archive, which selects precisely for the long ones: 2026-06 shows 20.4%
    of its works running beyond ten days against 1.5-5.3% everywhere else. That
    is a structurally unrepresentative cohort, and no row count filter would
    catch it.
    """
    buckets: dict[str, dict] = collections.defaultdict(
        lambda: {"n": 0, "late": 0, "short_n": 0, "short_late": 0, "long_n": 0})
    for permit, late in finished:
        started = permit.get("actual_start_date_time")
        if not started:
            continue
        row = buckets[_local(started).strftime("%Y-%m")]
        days = (_local(permit["actual_end_date_time"]) - _local(started)).total_seconds() / 86400
        row["n"] += 1
        row["late"] += late > 0
        row["long_n"] += days > 10
        if days < 2:
            row["short_n"] += 1
            row["short_late"] += late > 0

    out = []
    for month, row in sorted(buckets.items()):
        # Thin months are the ragged edges of the held range, not cohorts.
        if row["n"] < 500 or not row["short_n"]:
            continue
        if held is not None and month not in held:
            continue
        pct = round(100 * row["late"] / row["n"], 1)
        short_pct = round(100 * row["short_late"] / row["short_n"], 1)
        out.append({
            "month": month, "n": row["n"], "pct": pct,
            "short_n": row["short_n"], "short_pct": short_pct,
            "gap": round(pct - short_pct, 1),
            "long_pct": round(100 * row["long_n"] / row["n"], 1),
        })
    return out


def overruns(permits: list[dict]) -> dict:
    """How often Thames Water finished by the date they applied to finish by.

    The breakdowns are not decoration. Works completed within one month's
    archive are disproportionately short works, and lateness rises steeply with
    both duration and category — so the headline rate is a floor, and these are
    the evidence for saying so rather than an unsupported hedge.
    """
    finished = [(p, late) for p in permits if (late := days_late(p)) is not None]
    by_duration: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    by_category: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    histogram: collections.Counter = collections.Counter()
    early = on_last_day = 0

    for permit, late in finished:
        histogram[late] += 1
        if late < 0:
            early += 1
        elif late == 0:
            on_last_day += 1
        started = permit.get("actual_start_date_time")
        if started:
            band = by_duration[_duration_band(
                (_local(permit["actual_end_date_time"]) - _local(started)).total_seconds() / 86400)]
            band[0] += 1
            band[1] += late > 0
        category = by_category[permit.get("work_category") or "unrecorded"]
        category[0] += 1
        category[1] += late > 0

    late_days = sorted(late for _, late in finished if late > 0)
    unfinished = collections.Counter(
        p.get("work_status") or "unrecorded" for p in permits if days_late(p) is None)
    cohorts = by_start_month(finished, held_months())

    return {
        "permits": len(permits),
        "finished": len(finished),
        "early": early,
        "on_last_day": on_last_day,
        "late": len(late_days),
        "late_pct": round(100 * len(late_days) / len(finished), 1) if finished else None,
        "late_by_one_day": histogram[1],
        "over_a_day_late": sum(1 for d in late_days if d > 1),
        "max_days_late": late_days[-1] if late_days else None,
        "by_duration": {k: {"n": v[0], "late": v[1], "pct": round(100 * v[1] / v[0], 1)}
                        for k, v in by_duration.items()},
        "by_category": {k: {"n": v[0], "late": v[1], "pct": round(100 * v[1] / v[0], 1)}
                        for k, v in by_category.items()},
        "unfinished": dict(unfinished.most_common()),
        "by_start_month": cohorts,
    }


def load_faults(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(row) for row in conn.execute(
            "SELECT id, work_order_number, street, postcode, raised_at, status,"
            "       journey_type, easting, northing FROM faults "
            "WHERE easting > 0 AND northing > 0 AND raised_at IS NOT NULL"
        )
    ]


def _grid(records, cell: float, key_e="easting", key_n="northing"):
    """Bucket by grid cell so matching is not quadratic over ~14k x ~27k.

    `cell` must equal the search radius: the caller only scans the eight
    neighbouring cells, so a cell smaller than the radius silently misses
    permits and a cell larger than it is wasted work. Taking it as a required
    argument rather than defaulting to RADIUS_M is deliberate — building the
    index at one size and querying it at another is exactly the bug that made
    the first parameter sweep return zero matches at every radius below 50m.
    """
    index = collections.defaultdict(list)
    for record in records:
        if record.get(key_e) is None or record.get(key_n) is None:
            continue
        index[(int(record[key_e] // cell), int(record[key_n] // cell))].append(record)
    return index


def match(faults: list[dict], permits: list[dict], radius: float = RADIUS_M) -> dict:
    """Permits within `radius` of a fault, starting in a plausible window.

    **Nothing derived from this is published.** Against a permutation null it
    tops out at 5.5x chance, at settings (10m, 7 days) that reach only 1.1% of
    faults — so roughly one in five surviving matches is still coincidence, and
    the covered slice is too thin to generalise from. Kept because the negative
    result is the finding, and because a later month of permits could change it.
    """
    index = _grid(permits, radius)
    hits: dict[str, list[dict]] = {}
    for fault in faults:
        raised = dt.datetime.fromisoformat(fault["raised_at"])
        cx, cy = int(fault["easting"] // radius), int(fault["northing"] // radius)
        found = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for permit in index.get((cx + dx, cy + dy), ()):
                    metres = math.dist(
                        (fault["easting"], fault["northing"]),
                        (permit["easting"], permit["northing"]))
                    if metres > radius:
                        continue
                    start = permit.get("proposed_start_date") or permit.get("actual_start_date_time")
                    if not start:
                        continue
                    lag = (dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
                           - raised).total_seconds() / 86400
                    if -BEFORE_DAYS <= lag <= AFTER_DAYS:
                        found.append({"permit": permit, "metres": round(metres, 1),
                                      "lag_days": round(lag, 2)})
        if found:
            found.sort(key=lambda h: (h["metres"], abs(h["lag_days"])))
            hits[fault["id"]] = found
    return hits


def null_model(faults: list[dict], permits: list[dict], radius: float = RADIUS_M,
               seeds=(1, 2, 3)) -> float:
    """Chance matching, with locations shuffled between faults.

    Permutation rather than date shifting, for the reason #1 established: the
    volume of work varies enough over time that shifting dates changes the
    opportunity pool and quietly flatters the matcher.

    `radius` has to be threaded through: a null model measured at one radius
    says nothing about the matcher running at another.
    """
    counts = []
    for seed in seeds:
        shuffled = [dict(f) for f in faults]
        places = [(f["easting"], f["northing"]) for f in shuffled]
        random.Random(seed).shuffle(places)
        for fault, (e, n) in zip(shuffled, places):
            fault["easting"], fault["northing"] = e, n
        counts.append(len(match(shuffled, permits, radius)))
    return sum(counts) / len(counts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--join", action="store_true",
                        help="also run the fault-to-permit join and its null model")
    parser.add_argument("--radius", type=float, default=RADIUS_M)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    permits = load_permits()
    if not permits:
        log.error("no permit extracts in %s — run collector.permits first", PERMITS)
        return 1

    result = overruns(permits)
    print(f"{result['permits']:,} Thames Water permits; "
          f"{result['finished']:,} have both a proposed and an actual end date")
    print(f"\nAgainst the end date Thames Water themselves applied for:")
    print(f"  finished early:           {result['early']:6,}")
    print(f"  finished on the last day: {result['on_last_day']:6,}")
    print(f"  finished late:            {result['late']:6,} ({result['late_pct']}%)"
          f"  of which {result['late_by_one_day']:,} by exactly one day")
    print(f"  more than a day late:     {result['over_a_day_late']:6,}"
          f"   (worst: {result['max_days_late']} days)")

    print("\n  late rate by how long the work actually took:")
    for band in ("under a day", "1-2 days", "2-5 days", "5-10 days", "10-30 days", "over 30 days"):
        if band in result["by_duration"]:
            row = result["by_duration"][band]
            print(f"    {band:14s} {row['n']:6,} {row['pct']:5.1f}%")
    print("\n  late rate by starting month, against its observation window:")
    print(f"    {'month':8s} {'works':>7} {'late':>7} {'short<2d':>9} {'late':>7} {'gap':>7} {'>10d':>6}")
    for row in result["by_start_month"]:
        print(f"    {row['month']:8s} {row['n']:7,} {row['pct']:6.1f}% "
              f"{row['short_n']:9,} {row['short_pct']:6.1f}% {row['gap']:+6.1f}pp {row['long_pct']:5.1f}%")

    print("\n  late rate by work category:")
    for name, row in sorted(result["by_category"].items(), key=lambda kv: -kv[1]["n"]):
        print(f"    {name:24s} {row['n']:6,} {row['pct']:5.1f}%")

    if args.join:
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        faults = load_faults(conn)
        conn.close()
        hits = match(faults, permits, args.radius)
        chance = null_model(faults, permits, args.radius)
        print(f"\nFault-to-permit join at {args.radius:.0f}m, "
              f"-{BEFORE_DAYS}..+{AFTER_DAYS} days (not published):")
        print(f"  faults matched: {len(hits):,} of {len(faults):,} "
              f"({100 * len(hits) / len(faults):.1f}%)")
        print(f"  expected by chance: {chance:.0f}   signal: "
              + (f"{len(hits) / chance:.1f}x" if chance else "n/a"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

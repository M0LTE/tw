"""Derive the compact JSON the web UI reads from the SQLite database.

The database keeps everything, including faults resolved long ago. The browser
only needs the live picture plus enough history to chart trends, so we emit a
few small, dictionary-encoded files instead of shipping the whole database.

Run with ``python -m collector.build_site``.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import gzip
import json
import logging
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Any, Iterable

from .model import STATUS_ORDER
from .sources import SOURCES

log = logging.getLogger("build_site")

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "faults.db"
OUT = ROOT / "web" / "data"
REFERENCE = ROOT / "data" / "reference"
POSTCODE_LA = REFERENCE / "postcode_la.json.gz"
LA_HOUSEHOLDS = REFERENCE / "la_households.json"

EPOCH = dt.date(2020, 1, 1)
# How much resolution history the "how fast do they fix things" panels use.
RESOLVED_WINDOW_DAYS = 365
# Age buckets, in days. The last bucket is open-ended.
AGE_BUCKETS = (1, 7, 30, 90, 180, 365, 730)


def day_index(value: str | None) -> int | None:
    """Days since EPOCH, our compact date encoding."""
    if not value:
        return None
    try:
        date = dt.date.fromisoformat(value[:10])
    except ValueError:
        return None
    return (date - EPOCH).days


class Dictionary:
    """Dictionary encoder: repeated strings become small integer indexes."""

    def __init__(self) -> None:
        self.values: list[str | None] = []
        self._index: dict[str | None, int] = {}

    def __call__(self, value: str | None) -> int:
        if value not in self._index:
            self._index[value] = len(self.values)
            self.values.append(value)
        return self._index[value]


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, separators=(",", ":"), sort_keys=False)
    path.write_text(text)
    log.info("%-22s %8.1f KiB", path.name, len(text) / 1024)


# --------------------------------------------------------------------------- #


def open_faults(conn: sqlite3.Connection, today: dt.date) -> dict:
    """Every currently-open fault, columnar and dictionary-encoded."""
    status, journey, city, source, work_type, priority = (Dictionary() for _ in range(6))
    cols: dict[str, list] = {k: [] for k in
                             ("id", "wo", "cn", "s", "j", "w", "p", "c", "n", "pc", "st", "r", "f", "lon", "lat")}

    # Oldest first, nulls last: the UI slices the head of this for its
    # "longest-running faults" table, so the order is part of the contract.
    rows = conn.execute(
        "SELECT id, work_order_number, case_number, status, journey_type, mid_level_work_type,"
        "       priority_flag, city, source, postcode, street, raised_at, first_seen_at, lon, lat "
        "FROM faults WHERE is_open = 1 ORDER BY raised_at IS NULL, raised_at"
    )
    for row in rows:
        cols["id"].append(row["id"])
        cols["wo"].append(row["work_order_number"])
        cols["cn"].append(row["case_number"])
        cols["s"].append(status(row["status"]))
        cols["j"].append(journey(row["journey_type"]))
        cols["w"].append(work_type(row["mid_level_work_type"]))
        cols["p"].append(priority(row["priority_flag"]))
        cols["c"].append(city(row["city"]))
        cols["n"].append(source(row["source"]))
        cols["pc"].append(row["postcode"])
        cols["st"].append(row["street"])
        cols["r"].append(day_index(row["raised_at"]))
        cols["f"].append(day_index(row["first_seen_at"]))
        cols["lon"].append(None if row["lon"] is None else round(row["lon"], 5))
        cols["lat"].append(None if row["lat"] is None else round(row["lat"], 5))

    # Status history, so a fault's timeline opens instantly with no extra fetch.
    index = {fault_id: i for i, fault_id in enumerate(cols["id"])}
    history: dict[int, list[list[int]]] = {}
    events = conn.execute(
        "SELECT e.fault_id, e.observed_at, e.new_value "
        "FROM fault_events e JOIN faults f ON f.id = e.fault_id "
        "WHERE f.is_open = 1 AND (e.kind = 'appeared' OR (e.kind = 'changed' AND e.field = 'status')) "
        "ORDER BY e.observed_at, e.id"
    )
    for row in events:
        i = index.get(row["fault_id"])
        if i is None:
            continue
        history.setdefault(i, []).append([day_index(row["observed_at"]), status(row["new_value"])])

    return {
        "epoch": EPOCH.isoformat(),
        "today": (today - EPOCH).days,
        "dict": {
            "status": status.values,
            "journey": journey.values,
            "work_type": work_type.values,
            "priority": priority.values,
            "city": city.values,
            "source": source.values,
        },
        "cols": cols,
        "history": {str(k): v for k, v in history.items()},
    }


def _percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p90": None, "mean": None, "max": None}
    ordered = sorted(values)
    return {
        "p50": round(statistics.median(ordered), 1),
        "p90": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))], 1),
        "mean": round(statistics.mean(ordered), 1),
        "max": round(ordered[-1], 1),
    }


def _bucket(days: float) -> str:
    for edge in AGE_BUCKETS:
        if days <= edge:
            return f"<={edge}"
    return f">{AGE_BUCKETS[-1]}"


def summary(conn: sqlite3.Connection, today: dt.date) -> dict:
    """KPIs, trends and league tables."""
    open_rows = conn.execute(
        "SELECT source, status, journey_type, high_level_journey_type, priority_flag, city,"
        "       outcode, raised_at, first_seen_at "
        "FROM faults WHERE is_open = 1"
    ).fetchall()

    ages = []
    by_status: collections.Counter[str] = collections.Counter()
    by_source: collections.Counter[str] = collections.Counter()
    by_journey: collections.Counter[str] = collections.Counter()
    by_priority: collections.Counter[str] = collections.Counter()
    by_bucket: collections.Counter[str] = collections.Counter()
    by_place: dict[str, dict[str, Any]] = {}

    for row in open_rows:
        raised = day_index(row["raised_at"])
        age = None if raised is None else (today - EPOCH).days - raised
        if age is not None and age >= 0:
            ages.append(float(age))
            by_bucket[_bucket(age)] += 1
        by_status[row["status"] or "Unknown"] += 1
        by_source[row["source"]] += 1
        by_journey[row["journey_type"] or "Unknown"] += 1
        by_priority[row["priority_flag"] or "Unknown"] += 1

        place = row["city"] or "Unknown"
        entry = by_place.setdefault(place, {"place": place, "n": 0, "ages": [], "over_year": 0})
        entry["n"] += 1
        if age is not None:
            entry["ages"].append(age)
            if age > 365:
                entry["over_year"] += 1

    places = []
    for entry in by_place.values():
        if entry["n"] < 20:  # ignore long-tail places where one fault swings the median
            continue
        places.append(
            {
                "place": entry["place"],
                "n": entry["n"],
                "median_age": round(statistics.median(entry["ages"]), 1) if entry["ages"] else None,
                "over_year": entry["over_year"],
            }
        )
    places.sort(key=lambda p: -p["n"])

    # Backlog over time, reconstructed from each fault's first_seen/resolved dates.
    first_snapshot = conn.execute("SELECT min(observed_at) FROM snapshots").fetchone()[0]
    start = dt.date.fromisoformat(first_snapshot[:10]) if first_snapshot else today
    spans = conn.execute("SELECT first_seen_at, resolved_at, source FROM faults").fetchall()
    day_span = [(day_index(r["first_seen_at"]), day_index(r["resolved_at"]), r["source"]) for r in spans]

    backlog: list[dict] = []
    keys = [s.key for s in SOURCES]
    cursor = start
    while cursor <= today:
        d = (cursor - EPOCH).days
        counts = {k: 0 for k in keys}
        for seen, resolved, source_key in day_span:
            if seen is not None and seen <= d and (resolved is None or resolved > d):
                counts[source_key] = counts.get(source_key, 0) + 1
        backlog.append({"d": d, **counts})
        cursor += dt.timedelta(days=1)

    # Flow: how many appear vs disappear each day. The gap is the backlog trend.
    flow_raised: collections.Counter[int] = collections.Counter()
    flow_resolved: collections.Counter[int] = collections.Counter()
    for seen, resolved, _ in day_span:
        if seen is not None:
            flow_raised[seen] += 1
        if resolved is not None:
            flow_resolved[resolved] += 1
    # Skip the first snapshot: every fault "arrives" on the day tracking began,
    # which is an artefact of starting to look, not 20,000 faults in one day.
    flow = [
        {"d": entry["d"], "raised": flow_raised.get(entry["d"], 0), "resolved": flow_resolved.get(entry["d"], 0)}
        for entry in backlog[1:]
    ]

    # Time to resolution, for faults we watched from start to finish.
    cutoff = (today - dt.timedelta(days=RESOLVED_WINDOW_DAYS)).isoformat()
    resolved_rows = conn.execute(
        "SELECT source, journey_type, raised_at, first_seen_at, resolved_at FROM faults "
        "WHERE is_open = 0 AND resolved_at >= ?",
        (cutoff,),
    ).fetchall()

    resolution_all: list[float] = []
    resolution_observed: list[float] = []
    resolution_by_journey: dict[str, list[float]] = collections.defaultdict(list)
    for row in resolved_rows:
        gone = day_index(row["resolved_at"])
        raised = day_index(row["raised_at"])
        seen = day_index(row["first_seen_at"])
        if gone is None:
            continue
        if raised is not None and gone >= raised:
            age = float(gone - raised)
            resolution_all.append(age)
            resolution_by_journey[row["journey_type"] or "Unknown"].append(age)
        # Faults whose whole life we observed give an unbiased figure; faults
        # already open when we started are censored and would flatter them.
        if seen is not None and gone >= seen:
            first = conn.execute("SELECT min(observed_at) FROM snapshots").fetchone()[0]
            if first and row["first_seen_at"] > first:
                resolution_observed.append(float(gone - (raised if raised is not None else seen)))

    snapshots = conn.execute(
        "SELECT observed_at, total, appeared, changed, resolved, reappeared FROM snapshots "
        "ORDER BY observed_at"
    ).fetchall()

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "epoch": EPOCH.isoformat(),
        "today": (today - EPOCH).days,
        "sources": [{"key": s.key, "label": s.label} for s in SOURCES],
        "status_order": list(STATUS_ORDER),
        "totals": {
            "open": len(open_rows),
            "ever_seen": conn.execute("SELECT count(*) FROM faults").fetchone()[0],
            "resolved": conn.execute("SELECT count(*) FROM faults WHERE is_open = 0").fetchone()[0],
            "snapshots": len(snapshots),
            "first_snapshot": first_snapshot,
            "latest_snapshot": snapshots[-1]["observed_at"] if snapshots else None,
        },
        "age": {**_percentiles(ages), "buckets": dict(by_bucket)},
        "by_status": dict(by_status),
        "by_source": dict(by_source),
        "by_journey": dict(by_journey.most_common()),
        "by_priority": dict(by_priority.most_common()),
        "places": places[:200],
        "backlog": backlog,
        "flow": flow,
        "resolution": {
            "window_days": RESOLVED_WINDOW_DAYS,
            "n": len(resolution_all),
            "since_raised": _percentiles(resolution_all),
            "fully_observed_n": len(resolution_observed),
            "fully_observed": _percentiles(resolution_observed),
            "by_journey": {
                k: {"n": len(v), **_percentiles(v)}
                for k, v in sorted(resolution_by_journey.items(), key=lambda kv: -len(kv[1]))
            },
        },
        "snapshots": [dict(r) for r in snapshots],
    }


# Every Flooding-journey work order carries this work type. It is the category
# Thames Water told Ofwat should not attract an automatic GSS payment.
EXTERNAL_SEWER_FLOODING = "Sewer flooding - external investigation"

# Below this many open faults, a local authority's rate is too noisy to rank,
# and it is likely one Thames Water only partly supplies.
MIN_FAULTS_PER_AREA = 30


def external_sewer_flooding(conn: sqlite3.Connection, today: dt.date) -> dict:
    """The external sewer flooding backlog, for the panel about their GSS position."""
    rows = conn.execute(
        "SELECT status, raised_at FROM faults WHERE is_open = 1 AND mid_level_work_type = ?",
        (EXTERNAL_SEWER_FLOODING,),
    ).fetchall()

    ages: list[float] = []
    by_status: collections.Counter[str] = collections.Counter()
    for row in rows:
        by_status[row["status"] or "Unknown"] += 1
        raised = day_index(row["raised_at"])
        if raised is not None:
            age = (today - EPOCH).days - raised
            if age >= 0:
                ages.append(float(age))

    return {
        "work_type": EXTERNAL_SEWER_FLOODING,
        "open": len(rows),
        "age": _percentiles(ages),
        "over_year": sum(1 for a in ages if a > 365),
        "over_90d": sum(1 for a in ages if a > 90),
        "by_status": dict(by_status.most_common()),
    }


def load_reference() -> tuple[dict[str, str], dict[str, dict]]:
    """postcode -> local authority code, and local authority -> household count.

    Both are committed under data/reference/ by collector/reference.py, so this
    stays offline and the published rates are reproducible from the repository.
    """
    postcode_la: dict[str, str] = {}
    if POSTCODE_LA.exists():
        with gzip.open(POSTCODE_LA, "rt", encoding="utf-8") as fh:
            postcode_la = json.load(fh)
    households: dict[str, dict] = {}
    if LA_HOUSEHOLDS.exists():
        households = json.loads(LA_HOUSEHOLDS.read_text())
    return postcode_la, households


def areas(conn: sqlite3.Connection, today: dt.date) -> dict:
    """Open faults by local authority, per 10,000 households.

    Grouping by Thames Water's free-text `City` gave a table that mostly ranked
    population. Local authorities are a real statistical geography with a
    published household count, so the counts become comparable rates.
    """
    postcode_la, households = load_reference()
    if not postcode_la or not households:
        return {"available": False, "rows": [], "coverage": None}

    buckets: dict[str, dict] = {}
    placed = unplaced = 0
    for row in conn.execute(
        "SELECT postcode, raised_at FROM faults WHERE is_open = 1"
    ):
        code = postcode_la.get(row["postcode"] or "", "")
        if not code or code not in households:
            unplaced += 1
            continue
        placed += 1
        entry = buckets.setdefault(code, {"ages": [], "n": 0, "over_year": 0})
        entry["n"] += 1
        raised = day_index(row["raised_at"])
        if raised is not None:
            age = (today - EPOCH).days - raised
            if age >= 0:
                entry["ages"].append(age)
                if age > 365:
                    entry["over_year"] += 1

    rows = []
    for code, entry in buckets.items():
        if entry["n"] < MIN_FAULTS_PER_AREA:
            continue
        homes = households[code]["households"]
        rows.append(
            {
                "code": code,
                "name": households[code]["name"],
                "n": entry["n"],
                "households": homes,
                "per_10k": round(entry["n"] / homes * 10_000, 2) if homes else None,
                "median_age": round(statistics.median(entry["ages"]), 1) if entry["ages"] else None,
                "over_year": entry["over_year"],
            }
        )
    rows.sort(key=lambda r: -(r["per_10k"] or 0))

    return {
        "available": True,
        "rows": rows,
        "min_faults": MIN_FAULTS_PER_AREA,
        "coverage": round(100 * placed / max(1, placed + unplaced), 1),
        "unplaced": unplaced,
        "source": "ONS Census 2021 table TS041; postcode to local authority via postcodes.io",
    }


def public_reports(conn: sqlite3.Connection, today: dt.date) -> dict:
    """Problems the public has reported that are not yet work orders.

    Thames Water keeps only a rolling window of these, so we also carry recently
    departed ones: a report that quietly aged out without becoming a work order
    is the most interesting thing in this dataset.
    """
    town = Dictionary()
    cols: dict[str, list] = {k: [] for k in ("id", "t", "pc", "st", "r", "f", "g", "lon", "lat")}

    rows = conn.execute(
        "SELECT id, town, postcode, street, reported_at, first_seen_at, disappeared_at, lon, lat "
        "FROM reports WHERE is_current = 1 OR disappeared_at >= ? "
        "ORDER BY reported_at IS NULL, reported_at DESC",
        ((today - dt.timedelta(days=90)).isoformat(),),
    )
    for row in rows:
        cols["id"].append(row["id"])
        cols["t"].append(town(row["town"]))
        cols["pc"].append(row["postcode"])
        cols["st"].append(row["street"])
        cols["r"].append(day_index(row["reported_at"]))
        cols["f"].append(day_index(row["first_seen_at"]))
        cols["g"].append(day_index(row["disappeared_at"]))
        cols["lon"].append(None if row["lon"] is None else round(row["lon"], 5))
        cols["lat"].append(None if row["lat"] is None else round(row["lat"], 5))

    return {
        "epoch": EPOCH.isoformat(),
        "today": (today - EPOCH).days,
        "dict": {"town": town.values},
        "cols": cols,
    }


def report_summary(conn: sqlite3.Connection, today: dt.date) -> dict:
    current = conn.execute("SELECT count(*) FROM reports WHERE is_current = 1").fetchone()[0]
    ever = conn.execute("SELECT count(*) FROM reports").fetchone()[0]
    gone = conn.execute("SELECT count(*) FROM reports WHERE is_current = 0").fetchone()[0]

    per_day: collections.Counter[int] = collections.Counter()
    for (reported_at,) in conn.execute("SELECT reported_at FROM reports WHERE is_current = 1"):
        day = day_index(reported_at)
        if day is not None:
            per_day[day] += 1

    oldest, newest = conn.execute(
        "SELECT min(reported_at), max(reported_at) FROM reports WHERE is_current = 1"
    ).fetchone()
    window = None
    if oldest and newest:
        window = (dt.date.fromisoformat(newest[:10]) - dt.date.fromisoformat(oldest[:10])).days + 1

    return {
        "current": current,
        "ever_seen": ever,
        "departed": gone,
        "retention_days": window,
        "oldest": oldest,
        "newest": newest,
        "per_day": [{"d": d, "n": n} for d, n in sorted(per_day.items())],
    }


def build(db_path: Path, out: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    latest = conn.execute("SELECT max(observed_at) FROM snapshots").fetchone()[0]
    today = dt.date.fromisoformat(latest[:10]) if latest else dt.date.today()

    payload = summary(conn, today)
    payload["reports"] = report_summary(conn, today)
    payload["areas"] = areas(conn, today)
    payload["external_flooding"] = external_sewer_flooding(conn, today)
    _write(out / "summary.json", payload)
    _write(out / "open.json", open_faults(conn, today))
    _write(out / "reports.json", public_reports(conn, today))
    conn.close()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    build(args.db, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

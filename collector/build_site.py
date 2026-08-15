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
import math
import re
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Any, Iterable

from .model import STATUS_ORDER, STATUS_RANK
from . import sources
from .sources import SOURCES

log = logging.getLogger("build_site")

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "faults.db"
OUT = ROOT / "web" / "data"
REFERENCE = ROOT / "data" / "reference"
POSTCODE_LA = REFERENCE / "postcode_la.json.gz"
LA_HOUSEHOLDS = REFERENCE / "la_households.json"
NOTES = ROOT / "data" / "notes.json"
PERMITS_DIR = ROOT / "data" / "permits"

# A closed-feed status that reads like an outcome and is not one. Thames Water
# marks a record "Repair Complete" while outstanding line items remain on it,
# and the count more often rises than falls at that moment; the pin then ages
# off the map over the following 72 hours. Treated as an absence of a verdict
# rather than as a repair. See #32 for the working.
INCONCLUSIVE_STATUS = "Repair Complete"

EPOCH = dt.date(2020, 1, 1)
# How much resolution history the "how fast do they fix things" panels use.
RESOLVED_WINDOW_DAYS = 365
# Cleared faults accumulate without limit, so the browsable list is capped.
# At the observed ~200 clearances a day this is a few thousand rows; the
# 2026-08-05 bulk departure alone was 4,272. Older ones stay in the database
# and the change log, they are just not shipped to the browser.
CLEARED_WINDOW_DAYS = 90
# Age buckets, in days. The last bucket is open-ended.
AGE_BUCKETS = (1, 7, 30, 90, 180, 365, 730)


def epoch_seconds(value: str | None) -> int | None:
    """Whole seconds since the Unix epoch, our encoding for a precise moment.

    Used where the time of day carries information. Collection is hourly, so a
    clearance is located to about an hour; a day index would throw that away.
    """
    if not value:
        return None
    try:
        return int(dt.datetime.fromisoformat(value).timestamp())
    except ValueError:
        return None


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


def open_faults(conn: sqlite3.Connection, today: dt.date, links: dict | None = None) -> dict:
    """Every currently-open fault, columnar and dictionary-encoded."""
    status, journey, city, source, work_type, priority = (Dictionary() for _ in range(6))
    cols: dict[str, list] = {k: [] for k in
                             ("id", "wo", "cn", "s", "j", "w", "p", "c", "n", "pc", "st", "r", "f", "lon", "lat", "ol")}

    # Oldest first, nulls last: the UI slices the head of this for its
    # "longest-running faults" table, so the order is part of the contract.
    rows = conn.execute(
        "SELECT id, work_order_number, case_number, status, journey_type, mid_level_work_type,"
        "       priority_flag, city, source, postcode, street, raised_at, first_seen_at, lon, lat,"
        "       open_line_items "
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
        # Outstanding work on the record. Shipped because it is the only field
        # that contradicts a status of "Repair Complete", which 84.6% of faults
        # carrying it do — see #32.
        cols["ol"].append(row["open_line_items"])

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
        # Reports that look like they prompted this fault, keyed by row index.
        "reports": {str(index[k]): v for k, v in (links or {}).items() if k in index},
    }


def cleared_faults(conn: sqlite3.Connection, today: dt.date) -> dict:
    """Faults that have left the open feed, newest departure first.

    Open faults are a bounded set; cleared ones accumulate forever, so this is
    capped at ``CLEARED_WINDOW_DAYS``. The window is about keeping the payload
    finite, not about the older records being uninteresting — they stay in the
    database and in the change log.

    Carries the closed-feed verdict where there is one. That is the whole point
    of the view: a bulk departure is exactly when "cleared" and "repaired" are
    most likely to differ, and the verdict is the only field that tells them
    apart.
    """
    status, journey, city, source, work_type, priority, verdict = (Dictionary() for _ in range(7))
    cols: dict[str, list] = {k: [] for k in
                             ("id", "wo", "cn", "s", "j", "w", "p", "c", "n", "pc", "st",
                              "r", "t", "v", "lon", "lat", "ol")}

    cutoff = (today - dt.timedelta(days=CLEARED_WINDOW_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT f.id, f.work_order_number, f.case_number, f.status, f.journey_type,"
        "       f.mid_level_work_type, f.priority_flag, f.city, f.source, f.postcode,"
        "       f.street, f.raised_at, f.resolved_at, f.lon, f.lat, f.open_line_items,"
        "       cf.status AS verdict "
        "FROM faults f LEFT JOIN closed_faults cf ON cf.id = f.id "
        "WHERE f.is_open = 0 AND f.resolved_at >= ? "
        "ORDER BY f.resolved_at DESC",
        (cutoff,),
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
        cols["t"].append(epoch_seconds(row["resolved_at"]))
        cols["v"].append(verdict(row["verdict"]))
        cols["ol"].append(row["open_line_items"])
        cols["lon"].append(None if row["lon"] is None else round(row["lon"], 5))
        cols["lat"].append(None if row["lat"] is None else round(row["lat"], 5))

    latest = conn.execute("SELECT max(observed_at) FROM snapshots").fetchone()[0]
    return {
        "epoch": EPOCH.isoformat(),
        "today": (today - EPOCH).days,
        # The moment the newest collection ran. Every "in the last N hours"
        # window counts back from here: the site is only ever as current as its
        # last poll, and counting from the reader's clock would silently drop
        # the newest departures for anyone loading between runs.
        "latest": epoch_seconds(latest),
        "window_days": CLEARED_WINDOW_DAYS,
        "dict": {
            "status": status.values,
            "journey": journey.values,
            "work_type": work_type.values,
            "priority": priority.values,
            "city": city.values,
            "source": source.values,
            "verdict": verdict.values,
        },
        "cols": cols,
    }


def survival(conn: sqlite3.Connection) -> dict:
    """How long a fault takes to clear, estimated properly (#5).

    The site's descriptive "time to clear" is length-biased: it averages the
    faults that cleared inside the observation window, which are by definition
    the quick ones. #5 set out the fix and correctly rejected plain Kaplan-Meier
    at the time, because 99.8% of faults were already open at the first snapshot
    — a *prevalent* cohort, with entry ages up to 1,069 days, and KM assumes
    observation from time zero.

    That objection is now avoidable rather than needing to be worked around.
    There is a genuine *incident* cohort: faults whose ``raised_at`` falls after
    collection began, every one of them watched from the moment Thames Water
    raised it. For those, plain KM is not a compromise, it is the right
    estimator, and the 38% still open are handled as what they are — censored,
    not resolved.

    Note the cohort is defined on ``raised_at``, not ``first_seen_at``. A fault
    can appear in the feed long after it was raised; one such record cleared
    966 days after its raised date. Those are still left-truncated and must stay
    out, or the bias this function exists to remove comes straight back in.

    The curve stops at the window. Nothing here can estimate beyond the age of
    the oldest fault in the cohort, so it is reported to that point and no
    further, with the number still at risk alongside so a reader can see the
    estimate thinning rather than trusting a tail drawn from a handful of
    records.
    """
    first, latest = conn.execute(
        "SELECT min(observed_at), max(observed_at) FROM snapshots").fetchone()
    if not first or not latest:
        return {}
    now = dt.datetime.fromisoformat(latest)
    # Collection *moments* whose departures are not evidence of anything, not
    # fault ids — the same set `summary()` uses, and it has to be applied the
    # same way: a fault that departed in one of those collections still counts
    # as cleared if the closed feed corroborates that particular record.
    flagged = uncorroborated_bulk_departures(conn)

    # (duration in days, was it observed clearing)
    observations: list[tuple[float, bool]] = []
    # Clearance times split by how long Thames Water holds a finished record on
    # the map, which is the floor under every number here.
    by_retention: dict[int, list[float]] = collections.defaultdict(list)
    for row in conn.execute(
        "SELECT f.id, f.raised_at, f.resolved_at, f.is_open, f.remain_on_map_hrs,"
        "       cf.id AS corroborated "
        "FROM faults f LEFT JOIN closed_faults cf ON cf.id = f.id "
        "WHERE f.raised_at > ? AND f.raised_at IS NOT NULL", (first,)
    ):
        raised = dt.datetime.fromisoformat(row["raised_at"])
        if row["is_open"]:
            observations.append(((now - raised).total_seconds() / 86400, False))
            continue
        if not row["resolved_at"]:
            continue
        days = (dt.datetime.fromisoformat(row["resolved_at"]) - raised).total_seconds() / 86400
        # A record that vanished in an uncorroborated bulk departure was not
        # observed being repaired, so counting it as a clearance would drag the
        # curve down for a reason that has nothing to do with the work. It stops
        # being observable at that moment, which is censoring.
        cleared = row["resolved_at"] not in flagged or row["corroborated"] is not None
        observations.append((days, cleared))
        if cleared and row["remain_on_map_hrs"]:
            by_retention[row["remain_on_map_hrs"]].append(days)
    if len(observations) < 100:
        return {}

    observations.sort()
    total = len(observations)
    events = sum(1 for _, cleared in observations if cleared)

    # Kaplan-Meier, with Greenwood's variance for the interval.
    curve: list[list[float]] = [[0.0, 1.0, float(total), 0.0]]
    at_risk = total
    survival_p = 1.0
    greenwood = 0.0
    i = 0
    while i < len(observations):
        t = observations[i][0]
        tied = [o for o in observations[i:] if o[0] == t]
        i += len(tied)
        cleared_here = sum(1 for _, c in tied if c)
        if cleared_here and at_risk > cleared_here:
            survival_p *= 1 - cleared_here / at_risk
            greenwood += cleared_here / (at_risk * (at_risk - cleared_here))
            curve.append([round(t, 4), round(survival_p, 6), float(at_risk),
                          round(survival_p * math.sqrt(greenwood), 6)])
        at_risk -= len(tied)

    horizon = observations[-1][0]

    # What the curve is actually measuring, which is not repair time.
    #
    # Thames Water keeps a record on the public map for a further
    # `remain_on_map_hrs` after finishing with it, and that shows up as a hard
    # floor: with a 24-hour retention the tenth percentile of clearances is 1.09
    # days, with 72 hours it is 3.10. So this estimates time until the pin
    # disappears, and it exceeds the time to repair by the retention period.
    #
    # Not subtracting it. The retention clock runs from the completion
    # timestamp, and #33 established that timestamp is revised forward on 69% of
    # records — every one of 2,001 revisions moved it later — so subtracting a
    # fixed offset would be arithmetic on a moving quantity. Reported instead.
    retention = [
        {"hours": hours, "n": len(days_list),
         "median_days": round(statistics.median(days_list), 2),
         "p10_days": round(sorted(days_list)[len(days_list) // 10], 2)}
        for hours, days_list in sorted(by_retention.items())
        if len(days_list) >= 50
    ]

    def at(days: float) -> dict | None:
        """The estimate at a horizon, with the risk set that supports it."""
        if days > horizon:
            return None
        point = curve[0]
        for row in curve:
            if row[0] <= days:
                point = row
            else:
                break
        remaining = sum(1 for d, _ in observations if d >= days)
        return {"days": days, "cleared_pct": round(100 * (1 - point[1]), 1),
                "at_risk": remaining, "se_pct": round(100 * point[3], 1)}

    # Downsampled for transport. The estimator runs on every distinct event
    # time; the browser draws a step function, so sampling it on a fixed grid
    # is visually identical and an order of magnitude smaller.
    step = max(horizon / 240, 1 / 96)
    sampled: list[list[float]] = []
    j = 0
    t = 0.0
    while t <= horizon + 1e-9:
        while j + 1 < len(curve) and curve[j + 1][0] <= t:
            j += 1
        sampled.append([round(t, 3), round(curve[j][1], 5)])
        t += step

    # The median from the estimator: the first time survival drops to or below
    # a half. None when it never does inside the window, which is the honest
    # answer rather than extrapolating one.
    km_median = next((row[0] for row in curve if row[1] <= 0.5), None)

    return {
        "cohort": total,
        "cleared": events,
        "censored": total - events,
        "horizon_days": round(horizon, 2),
        "tracking_began": first,
        "curve": sampled,
        "horizons": [h for h in (at(1), at(3), at(7), at(14), at(30)) if h],
        "median_days": round(km_median, 2) if km_median is not None else None,
        # The length-biased figure this replaces: the median over only those
        # faults that cleared, which can only contain the quick ones.
        "naive_median_days": round(
            statistics.median([d for d, c in observations if c]), 2) if events else None,
        # Not repair time — see the comment above. The floor under all of it.
        "retention": retention,
    }


def permits() -> dict:
    """Thames Water's own street works permits, against their own deadlines (#6).

    A different source on a different clock: DfT's Street Manager archive, one
    zip a month in arrears, where the fault map is polled hourly. It is here
    because it answers a question the fault feed cannot — whether the work
    Thames Water actually did finished by the date they applied to finish by.

    Empty when no monthly extract is committed, so the section simply does not
    render rather than showing a zero.
    """
    try:
        from collector.permit_join import load_permits, overruns
    except ImportError:  # pragma: no cover - zoneinfo data missing
        log.warning("permit analysis unavailable")
        return {}
    records = load_permits()
    if not records:
        return {}
    result = overruns(records)
    result["months"] = sorted(p.stem.replace(".ndjson", "")
                              for p in PERMITS_DIR.glob("*.ndjson.gz"))
    return result


def notes() -> dict:
    """Hand-written narrative entries, validated on the way through.

    The one part of the site not derived from the change log, because it is the
    part that needs judgement: the data can show that 13,600 work orders stopped
    being published, but only a person can say what that probably means.

    Validated rather than passed through: a typo in a block key would otherwise
    render as a silently missing paragraph, and a note that quietly loses half
    its reasoning is worse than no note.
    """
    if not NOTES.exists():
        log.warning("no %s; the narrative page will be empty", NOTES.name)
        return {"entries": []}

    raw = json.loads(NOTES.read_text())
    entries = []
    for i, entry in enumerate(raw.get("entries", [])):
        where = f"notes.json entry {i}"
        for required in ("date", "title", "body"):
            if not entry.get(required):
                raise ValueError(f"{where}: missing {required!r}")
        try:
            dt.date.fromisoformat(entry["date"])
        except ValueError as exc:
            raise ValueError(f"{where}: date {entry['date']!r} is not ISO 8601") from exc
        for block in entry["body"]:
            keys = set(block) & {"p", "list", "table"}
            if len(keys) != 1:
                raise ValueError(f"{where}: each body block needs exactly one of p/list/table, got {sorted(block)}")
        entries.append({k: v for k, v in entry.items() if not k.startswith("_")})

    entries.sort(key=lambda e: e["date"], reverse=True)
    return {"entries": entries}


def stage_occupancy(conn: sqlite3.Connection) -> dict:
    """Where the open backlog is sitting, and how long it has sat there.

    Deliberately *not* median time-in-stage. That figure is computable and
    wrong: a dwell is only complete when both the arrival and the departure are
    observed, so with a 10-day window we can only ever see visits shorter than
    10 days. The observed Investigation median came out at 0.17 days while
    2,194 of the 2,991 faults actually sitting in Investigation had not moved
    for over 5 days — the statistic measures the visits fast enough to fit in
    the window, and calls it the typical visit. That is the mistake #5 exists
    to warn about, applied to stages instead of totals.

    What is honest is the censored view: how long each *currently open* fault
    has been at its stage, reported as "at least", because we cannot see behind
    the start of collection. Every fault counted here is one Thames Water is
    publishing as open right now, so nothing is inferred.
    """
    moved = {
        row["fault_id"]: row["last"] for row in conn.execute(
            "SELECT fault_id, max(observed_at) AS last FROM fault_events "
            "WHERE kind = 'changed' AND field = 'status' GROUP BY fault_id"
        )
    }
    latest = conn.execute("SELECT max(observed_at) FROM snapshots").fetchone()[0]
    if not latest:
        return {"stages": [], "bucket_days": [], "backwards": [], "backwards_total": 0}
    now = dt.datetime.fromisoformat(latest)

    # Only thresholds the observation window can actually support. Publishing a
    # "held over 30 days" column while we have watched for 10 would print a
    # column of zeros that reads as "none are stuck", which is the opposite of
    # what it would mean. Buckets appear as the history grows.
    first = conn.execute("SELECT min(observed_at) FROM snapshots").fetchone()[0]
    window = (now - dt.datetime.fromisoformat(first)).total_seconds() / 86400 if first else 0
    buckets = tuple(b for b in (1, 7, 30, 90) if b <= window)
    stages: list[dict] = []
    for stage in STATUS_ORDER:
        rows = conn.execute(
            "SELECT id, first_seen_at FROM faults WHERE is_open = 1 AND status = ?", (stage,)
        ).fetchall()
        if not rows:
            continue
        # Since the last status change, or since we first saw it — whichever is
        # later is the most we can honestly claim.
        held = []
        for row in rows:
            since = max(moved.get(row["id"], ""), row["first_seen_at"])
            held.append((now - dt.datetime.fromisoformat(since)).total_seconds() / 86400)
        stages.append({
            "stage": stage,
            "n": len(held),
            "buckets": [sum(1 for d in held if d >= b) for b in buckets],
        })

    backwards: collections.Counter = collections.Counter()
    events: dict[str, list[str]] = collections.defaultdict(list)
    for row in conn.execute(
        "SELECT fault_id, old_value, new_value FROM fault_events "
        "WHERE kind = 'changed' AND field = 'status' AND old_value IS NOT NULL"
    ):
        a, b = STATUS_RANK.get(row["old_value"]), STATUS_RANK.get(row["new_value"])
        if a is not None and b is not None and b < a:
            backwards[f"{row['old_value']} → {row['new_value']}"] += 1

    return {
        "stages": stages,
        "bucket_days": list(buckets),
        "backwards": backwards.most_common(5),
        "backwards_total": sum(backwards.values()),
    }


def _peak_work_orders_before(conn: sqlite3.Connection, before: str, days: int = 7) -> int | None:
    """Highest open work order count in the week preceding `before`.

    Not "the previous snapshot": an event can unfold across several collections
    — the 2026-08-05 one ran from 18:47 to 23:18 — so the immediately preceding
    snapshot may already be part of the collapse and would understate it wildly.
    A peak over a window is unambiguous and needs no judgement about where the
    event started.
    """
    since = (dt.datetime.fromisoformat(before) - dt.timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT source_counts FROM snapshots WHERE observed_at < ? AND observed_at >= ?",
        (before, since),
    ).fetchall()
    if not rows:
        return None
    keys = [s.key for s in SOURCES if s.kind == sources.WORK_ORDER]
    return max(sum(json.loads(r["source_counts"]).get(k, 0) for k in keys) for r in rows)


# A collection's departures are treated as not-evidence-of-clearing when they
# are both far above the norm and almost entirely uncorroborated. Both tests
# together, because either alone is wrong: a big batch that the closed feed
# confirms is real work, and a small uncorroborated batch is ordinary noise.
BULK_DEPARTURE_MULTIPLE = 20      # times the median collection
BULK_DEPARTURE_CONFIRMED = 0.10   # share the closed feed corroborates


def uncorroborated_bulk_departures(conn: sqlite3.Connection) -> set[str]:
    """Collections whose departures cannot be read as work being cleared.

    Note carefully what this is *not*: it is not the ingestion guard. Every one
    of these records was believed, stored and is browsable under Faults →
    Cleared. #24 settled that a poll verified as complete gets recorded however
    implausible it looks, and that stands. This is the separate, downstream
    question of whether a departure counts as *evidence* — and the answer has
    been "only if the closed feed corroborates it" since #24 too. This just
    applies that test at collection level so it also catches the 5 August 18:47
    collapse, which the live guard never flagged because it fell under the
    threshold at the time.

    Without it a single collection contributes a bar 200x the median and the
    chart it sits in conveys nothing (#30).
    """
    rows = conn.execute(
        "SELECT f.resolved_at AS moment, COUNT(*) AS n, "
        "       SUM(CASE WHEN cf.id IS NOT NULL THEN 1 ELSE 0 END) AS confirmed "
        "FROM faults f LEFT JOIN closed_faults cf ON cf.id = f.id "
        "WHERE f.resolved_at IS NOT NULL GROUP BY f.resolved_at"
    ).fetchall()
    if not rows:
        return set()
    typical = statistics.median(r["n"] for r in rows) or 1
    return {
        r["moment"] for r in rows
        if r["n"] > typical * BULK_DEPARTURE_MULTIPLE
        and r["confirmed"] / r["n"] < BULK_DEPARTURE_CONFIRMED
    }


def anomalous_snapshots(conn: sqlite3.Connection) -> set[str]:
    """Snapshots where an unusual share of known records stopped appearing.

    The retrieval was verified complete, so the departures are recorded in full
    and are browsable. What they are kept out of is the *duration* statistics:
    a source-side event that empties a layer would otherwise register as
    thousands of faults "resolved" on the hour and silently rewrite every median
    on the site. A departure observed here counts once the closed feed
    corroborates it — Thames Water saying "Completed" is evidence; the record
    merely ceasing to be published is not (#24).
    """
    flagged = {
        row[0] for row in conn.execute(
            "SELECT observed_at FROM snapshots WHERE anomalous IS NOT NULL"
        )
    }
    return flagged | uncorroborated_bulk_departures(conn)


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

    # Backlog and flow per *snapshot*, not per calendar day. Bucketing by day
    # threw away half the resolution once collection went twice-daily, and left
    # both charts in their empty state for a full day after tracking began.
    # Timestamps are epoch seconds so unevenly spaced observations plot correctly.
    observed = [r[0] for r in conn.execute("SELECT observed_at FROM snapshots ORDER BY observed_at")]
    first_snapshot = observed[0] if observed else None
    spans = conn.execute("SELECT first_seen_at, resolved_at, source FROM faults").fetchall()
    flagged = anomalous_snapshots(conn)
    corroborated = {r[0] for r in conn.execute("SELECT id FROM closed_faults")}
    departures = conn.execute("SELECT id, resolved_at FROM faults WHERE resolved_at IS NOT NULL").fetchall()

    backlog: list[dict] = []
    flow: list[dict] = []
    keys = [s.key for s in SOURCES if s.kind == sources.WORK_ORDER]
    for i, moment in enumerate(observed):
        counts = {k: 0 for k in keys}
        for row in spans:
            if row["first_seen_at"] <= moment and (
                row["resolved_at"] is None or row["resolved_at"] > moment
            ):
                counts[row["source"]] = counts.get(row["source"], 0) + 1
        stamp = int(dt.datetime.fromisoformat(moment).timestamp())
        backlog.append({"t": stamp, "total": sum(counts.values()), **counts})
        # The first snapshot is everything arriving at once, which is an artefact
        # of starting to look rather than a day's worth of faults.
        if i:
            # A chart called "arriving vs clearing" has to apply the same test as
            # the duration figures: departures from a flagged collection are not
            # evidence anything was cleared unless the closed feed says so. Drawn
            # as clearing they are also 558x the median bar, which flattens every
            # other collection to nothing (#30). Reported separately so the site
            # can mark the collection rather than silently dropping the count.
            if moment in flagged:
                gone = sum(1 for r in departures
                           if r["resolved_at"] == moment and r["id"] in corroborated)
                withheld = sum(1 for r in departures if r["resolved_at"] == moment) - gone
            else:
                gone = sum(1 for r in spans if r["resolved_at"] == moment)
                withheld = 0
            flow.append({
                "t": stamp,
                "raised": sum(1 for r in spans if r["first_seen_at"] == moment),
                "resolved": gone,
                **({"withheld": withheld} if withheld else {}),
            })

    # Time to resolution, for faults we watched from start to finish.
    cutoff = (today - dt.timedelta(days=RESOLVED_WINDOW_DAYS)).isoformat()
    # Departures seen in a flagged snapshot are excluded unless Thames Water's
    # own closed feed corroborates them; see `anomalous_snapshots`.
    candidates = conn.execute(
        "SELECT f.source, f.journey_type, f.raised_at, f.first_seen_at, f.resolved_at,"
        "       cf.id AS corroborated "
        "FROM faults f LEFT JOIN closed_faults cf ON cf.id = f.id "
        "WHERE f.is_open = 0 AND f.resolved_at >= ?",
        (cutoff,),
    ).fetchall()
    admitted = [
        row for row in candidates
        if row["resolved_at"] not in flagged or row["corroborated"] is not None
    ]
    quarantined = len(candidates) - len(admitted)
    resolved_rows = admitted

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

    # Two different things can be wrong with the newest poll, and they need
    # saying differently. `truncated` means we could not read a layer completely
    # and applied nothing, so the backlog shown is stale. `anomalous` means we
    # read it fine and a great many records really did stop appearing, so the
    # backlog is current but a big part of it is unexplained. Publishing neither
    # would leave a reader to assume an ordinary week.
    # A flag is a property of a *period*, not of one collection. Keying this to
    # "the newest snapshot is flagged" made the warning vanish the moment normal
    # collection resumed, while the backlog it explained was still 63% down and
    # unexplained on the page (#25). It stays up while it is still shaping what
    # the site reports — which is exactly while departures are being held out of
    # the duration figures.
    flagged_row = conn.execute(
        "SELECT observed_at, truncated, anomalous, resolved, source_counts FROM snapshots "
        "WHERE truncated IS NOT NULL OR anomalous IS NOT NULL "
        "ORDER BY observed_at DESC LIMIT 1"
    ).fetchone()
    stale = None
    if flagged_row and (quarantined or flagged_row["observed_at"] == snapshots[-1]["observed_at"]):
        counts = json.loads(flagged_row["source_counts"])
        stale = {
            "kind": "truncated" if flagged_row["truncated"] else "anomalous",
            "observed_at": flagged_row["observed_at"],
            "is_latest": flagged_row["observed_at"] == snapshots[-1]["observed_at"],
            "retained": json.loads(flagged_row["anomalous"] or flagged_row["truncated"] or "{}"),
            "source_open": sum(counts.get(s.key, 0) for s in SOURCES if s.kind == sources.WORK_ORDER),
            "our_open": len(open_rows),
            "departed": flagged_row["resolved"],
            "quarantined": quarantined,
            # What the backlog stood at before the flagged collection, so the
            # banner can state the size of the drop rather than leaving the
            # reader to find a previous figure that is no longer on the page.
            # Work-order layers only: `snapshots.total` sums all five, which
            # would quietly inflate the "down from" figure by ~4,000 reports and
            # closed records.
            "open_before": _peak_work_orders_before(conn, flagged_row["observed_at"]),
        }

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
        # Present only while the newest poll is one we refused to apply.
        "truncated": stale,
        "age": {**_percentiles(ages), "buckets": dict(by_bucket)},
        "by_status": dict(by_status),
        "by_source": dict(by_source),
        "by_journey": dict(by_journey.most_common()),
        "by_priority": dict(by_priority.most_common()),
        "places": places[:200],
        "backlog": backlog,
        "flow": flow,
        "snapshot_times": [int(dt.datetime.fromisoformat(o).timestamp()) for o in observed],
        "resolution": {
            "window_days": RESOLVED_WINDOW_DAYS,
            # Departures from flagged snapshots that the closed feed does not
            # corroborate, and which therefore do not count towards any duration
            # below. Published rather than merely applied: silently dropping
            # records from a statistic is the same sin as silently including
            # them, and the reader is entitled to know the denominator moved.
            "quarantined": quarantined,
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


# A report and a work order share no key, so the link can only be inferred.
# Exact street + postcode within this window is high precision; widening past
# seven days adds nothing (measured: 241 matched reports at both 7 and 14 days).
MATCH_BEFORE_DAYS = 1   # slack for clock skew between the two feeds
# Chosen by measurement, not taste. Permuting addresses between reports while
# holding dates fixed gives the rate at which this matcher fires on a street
# that simply had work anyway. Widening the window buys real matches and chance
# matches together, and past three days the chance ones dominate:
#
#   window     real   chance   signal   excess (real - chance)
#   -1..+1d    1477      353     4.2x     1124
#   -1..+2d    1697      496     3.4x     1201
#   -1..+3d    1850      631     2.9x     1219   <- most attributable links
#   -1..+7d    1964     1025     1.9x      939   <- was here; net loss
#
# Going from 3 to 7 days added 114 real matches and 394 chance ones. Three days
# maximises the links actually attributable to a report having been made.
MATCH_AFTER_DAYS = 3


# Thames Water's `Street` is a full address line, not a street: about 70% of
# records begin with a house number, in no consistent format —
# "5,MANDEVILLE CLOSE", "21 MANDEVILLE CLOSE TILEHURST", "TILEHURST ROAD 47A".
# Keying on it raw meant a report at №3 could never match a work order at №5 on
# the same street, which cost roughly half of all real links (#28).
_HOUSE_NUMBER_PREFIX = re.compile(r"^[0-9]+[A-Z]?\s*[,\-]?\s*")
_HOUSE_NUMBER_SUFFIX = re.compile(r"\s+[0-9]+[A-Z]?$")


def _street_name(street: str) -> str:
    """The street part of an address line, with the house number removed."""
    text = re.sub(r"\s+", " ", street.strip().upper())
    text = _HOUSE_NUMBER_PREFIX.sub("", text)
    text = _HOUSE_NUMBER_SUFFIX.sub("", text)
    # Punctuation is inconsistent enough to be noise on its own.
    text = re.sub(r"[^A-Z ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _match_key(street: str | None, postcode: str | None) -> tuple[str, str] | None:
    """Street and postcode — genuinely, which is what the site has always claimed.

    Deliberately not postcode alone. 4% of postcodes carry more than one street
    even after normalisation, so postcode-only would assert a link between
    different streets, and its null model is half again as large for 328 extra
    matches. Street level keeps the claim defensible.
    """
    if not street or not postcode:
        return None
    name = _street_name(street)
    if not name:
        return None
    return (name, re.sub(r"\s+", " ", postcode.strip().upper()))


def cross_links(conn: sqlite3.Connection) -> tuple[dict, dict]:
    """Associate public reports with work orders raised at the same address.

    Returns ``(by_report, by_fault)``. Deliberately keeps every candidate rather
    than picking the closest in time: where an address has more than one work
    order in the window the ambiguity is real, and choosing silently would
    present a guess as a fact.
    """
    # One entry per work order, not one per table it appears in. A work order we
    # watched leave the open feed sits in *both* `faults` and `closed_faults` —
    # 5,927 of them do — and iterating the tables naively listed each one twice.
    # That told 787 reports "more than one work order fits, so which followed
    # from this report is ambiguous" when it was the same work order shown
    # twice: a fabricated ambiguity, in the one place the site is careful not to
    # present a guess as a fact.
    #
    # `faults` wins where both exist, because it is what the open feed said, and
    # its `is_open` is the real answer to whether the work order is still live.
    # The old code hardcoded open=1 for everything in `faults`, so a departed
    # fault rendered as a clickable link into a list that only holds open ones —
    # a link that silently did nothing.
    seen: dict[str, dict] = {}
    for table in ("closed_faults", "faults"):
        live = "is_open" if table == "faults" else "0 AS is_open"
        for row in conn.execute(
            f"SELECT id, work_order_number, street, postcode, raised_at, journey_type,"
            f"       status, {live} FROM {table} WHERE raised_at IS NOT NULL"
        ):
            seen[row["id"]] = {**dict(row), "open": row["is_open"]}

    candidates: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for work_order in seen.values():
        key = _match_key(work_order["street"], work_order["postcode"])
        if key:
            candidates[key].append(work_order)

    by_report: dict[str, list[dict]] = {}
    by_fault: dict[str, list[dict]] = collections.defaultdict(list)

    for report in conn.execute(
        "SELECT id, street, postcode, reported_at FROM reports WHERE reported_at IS NOT NULL"
    ):
        key = _match_key(report["street"], report["postcode"])
        if not key:
            continue
        reported = dt.datetime.fromisoformat(report["reported_at"])
        hits = []
        for work_order in candidates.get(key, []):
            lag = (dt.datetime.fromisoformat(work_order["raised_at"]) - reported).total_seconds() / 86400
            if -MATCH_BEFORE_DAYS <= lag <= MATCH_AFTER_DAYS:
                hits.append({
                    "id": work_order["id"],
                    "wo": work_order["work_order_number"],
                    "status": work_order["status"],
                    "journey": work_order["journey_type"],
                    "raised": day_index(work_order["raised_at"]),
                    "open": work_order["open"],
                    "lag_days": round(lag, 2),
                })
        if hits:
            hits.sort(key=lambda h: h["lag_days"])
            by_report[report["id"]] = hits
            for hit in hits:
                by_fault[hit["id"]].append({
                    "id": report["id"],
                    "reported": day_index(report["reported_at"]),
                    "lag_days": hit["lag_days"],
                })

    return by_report, dict(by_fault)


def closure_outcomes(conn: sqlite3.Connection) -> dict:
    """What actually happened to faults that left the open feed.

    Until now the site could only say a fault "stopped appearing". The closed
    work order layers give Thames Water's own verdict — Completed or Canceled —
    for those we can match, which is the difference between a repair and a
    cancellation.
    """
    listed = dict(conn.execute(
        "SELECT status, count(*) FROM closed_faults GROUP BY status"
    ).fetchall())

    gone = conn.execute("SELECT count(*) FROM faults WHERE is_open = 0").fetchone()[0]
    matched = dict(conn.execute(
        "SELECT cf.status, count(*) FROM faults f JOIN closed_faults cf ON cf.id = f.id "
        "WHERE f.is_open = 0 GROUP BY cf.status"
    ).fetchall())
    resolved_matched = sum(matched.values())
    # "Repair Complete" is not a verdict, whatever it sounds like. Thames Water
    # applies it to records that overwhelmingly still carry outstanding line
    # items — 79.8% of the closed feed's, against 2.8% of its "Completed" ones —
    # and 72.6% of transitions into it come with that count going *up*. Counting
    # it as accounted-for made "of the faults we can explain, x% were cancelled"
    # quietly imply the rest were repaired. It belongs with the unexplained. #32
    inconclusive = matched.pop(INCONCLUSIVE_STATUS, 0)
    conclusive = resolved_matched - inconclusive

    return {
        "listed": listed,
        "listed_total": sum(listed.values()),
        "with_closure_date": conn.execute(
            "SELECT count(*) FROM closed_faults WHERE closure_at IS NOT NULL"
        ).fetchone()[0],
        # Of the faults we watched leave the open feed, how many carry a verdict
        # that actually says what happened.
        "departed": gone,
        "matched": conclusive,
        "matched_by_status": matched,
        "inconclusive": inconclusive,
        "inconclusive_status": INCONCLUSIVE_STATUS,
        "unexplained": gone - conclusive,
    }


def public_reports(conn: sqlite3.Connection, today: dt.date, links: dict | None = None) -> dict:
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

    index = {report_id: i for i, report_id in enumerate(cols["id"])}
    return {
        "epoch": EPOCH.isoformat(),
        "today": (today - EPOCH).days,
        "dict": {"town": town.values},
        "cols": cols,
        # Work orders raised at the same address shortly after, by row index.
        "faults": {str(index[k]): v for k, v in (links or {}).items() if k in index},
        "match_window": {"before_days": MATCH_BEFORE_DAYS, "after_days": MATCH_AFTER_DAYS},
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
    payload["closure"] = closure_outcomes(conn)
    payload["stages"] = stage_occupancy(conn)
    payload["survival"] = survival(conn)
    by_report, by_fault = cross_links(conn)
    payload["links"] = {"reports_matched": len(by_report), "faults_matched": len(by_fault)}
    _write(out / "summary.json", payload)
    _write(out / "open.json", open_faults(conn, today, by_fault))
    _write(out / "notes.json", notes())
    _write(out / "permits.json", permits())
    _write(out / "cleared.json", cleared_faults(conn, today))
    _write(out / "reports.json", public_reports(conn, today, by_report))
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

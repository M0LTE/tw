"""Tests for the delta/replay machinery.

The whole point of the project is that yesterday's numbers still mean the same
thing tomorrow, so these focus on the tracking logic rather than the HTTP layer.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import model, sources, store  # noqa: E402
from collector.collect import build_delta  # noqa: E402


def feature(work_order_id: str, **overrides):
    attrs = {
        "WorkOrderID": work_order_id,
        "WorkOrderNumber": "0100" + work_order_id[-4:],
        "CaseNumber": "0150" + work_order_id[-4:],
        "CaseID": "500" + work_order_id[-4:],
        "CaseRecordType": "Customer",
        "JourneyType": "Blockage",
        "HighLevelJourneyType": "Blockage",
        "MidLevelWorkType": "Sewer blockage",
        "PriorityFlag": "N/A",
        "WorkOrderStatus": "Reported",
        "Street": "1 HIGH STREET",
        "Postcode": "tw10 6lx",
        "City": "RICHMOND",
        "OpenWorkOrderEasting": 518407.95,
        "OpenWorkOrderNorthing": 174769.37,
        "WorkOrderRaisedDate": 1770256515000,
        "LastModifiedDate": 1770265361000,
        "OpenWorkOrderLineItemCount": 1,
        "ClosedWorkOrderLineItemCount": 0,
        "RemainOnMapInHrs": 72,
        "ShowOnMapIndicator": "Yes",
    }
    attrs.update(overrides)
    return {"attributes": attrs, "geometry": {"x": -0.297, "y": 51.459}}


def records(*features, source="waste"):
    out = {}
    for f in features:
        fault_id, record = model.normalise(f, source)
        out[fault_id] = record
    return out


class NormalisationTests(unittest.TestCase):
    def test_maps_expected_fields(self):
        fault_id, record = model.normalise(feature("WO1"), "waste")
        self.assertEqual(fault_id, "WO1")
        self.assertEqual(record["source"], "waste")
        self.assertEqual(record["status"], "Reported")
        self.assertEqual(record["street"], "1 HIGH STREET")
        self.assertEqual(record["lat"], 51.459)

    def test_postcode_is_upper_cased_and_split(self):
        _, record = model.normalise(feature("WO1"), "waste")
        self.assertEqual(record["postcode"], "TW10 6LX")
        self.assertEqual(record["outcode"], "TW10")

    def test_outcode_handles_every_uk_postcode_shape(self):
        cases = {
            "M1 1AE": "M1", "B33 8TH": "B33", "CR2 6XH": "CR2",
            "DN55 1PT": "DN55", "EC1A 1BB": "EC1A", "W1A 0AX": "W1A",
        }
        for postcode, expected in cases.items():
            self.assertEqual(model.outcode_of(postcode), expected, postcode)
        for junk in (None, "", "not a postcode", "TW10"):
            self.assertIsNone(model.outcode_of(junk))

    def test_dates_become_iso_utc(self):
        _, record = model.normalise(feature("WO1"), "waste")
        self.assertEqual(record["raised_at"], "2026-02-05T01:55:15+00:00")
        self.assertIsNone(record["closure_at"])

    def test_implausible_dates_are_dropped(self):
        # A zero timestamp would otherwise read as a fault raised in 1970 and
        # dominate every "oldest fault" figure on the site.
        for junk in (0, -1, 4_200_000_000_000, "", None, "not a date"):
            _, record = model.normalise(feature("WO1", WorkOrderRaisedDate=junk), "waste")
            self.assertIsNone(record["raised_at"], junk)

    def test_salesforce_dates_are_uk_local_not_utc(self):
        # The feed publishes WorkOrderRaisedDate as UK wall-clock time carrying a
        # UTC epoch label. Compared against created_date (ArcGIS editor tracking,
        # genuinely UTC) the gap is exactly +3600s in every BST month and ~0 in
        # GMT, so these have to be read as Europe/London.
        summer = int(dt.datetime(2026, 8, 1, 13, 21, 49, tzinfo=dt.timezone.utc).timestamp() * 1000)
        _, record = model.normalise(feature("WO1", WorkOrderRaisedDate=summer), "waste")
        self.assertEqual(record["raised_at"], "2026-08-01T12:21:49+00:00", "BST is UTC+1")

        winter = int(dt.datetime(2026, 1, 15, 13, 21, 49, tzinfo=dt.timezone.utc).timestamp() * 1000)
        _, record = model.normalise(feature("WO1", WorkOrderRaisedDate=winter), "waste")
        self.assertEqual(record["raised_at"], "2026-01-15T13:21:49+00:00", "GMT is UTC")

    def test_report_dates_stay_utc(self):
        # The pending-pins layer uses ArcGIS editor-tracking fields, which are
        # already UTC and must not be shifted.
        stamp = int(dt.datetime(2026, 7, 31, 16, 33, 24, tzinfo=dt.timezone.utc).timestamp() * 1000)
        _, record = model.normalise_report(pin("r-1", CreationDate=stamp), "reported")
        self.assertEqual(record["reported_at"], "2026-07-31T16:33:24+00:00")

    def test_features_without_an_id_are_rejected(self):
        self.assertIsNone(model.normalise(feature("WO1", WorkOrderID=None), "waste"))
        self.assertIsNone(model.normalise(feature("WO1", WorkOrderID="  "), "waste"))

    def test_blank_strings_normalise_to_none(self):
        _, record = model.normalise(feature("WO1", City="   ", ThoroughFare=""), "waste")
        self.assertIsNone(record["city"])
        self.assertIsNone(record["thoroughfare"])


class DeltaTests(unittest.TestCase):
    def test_classifies_new_changed_and_resolved(self):
        previous = records(feature("A"), feature("B"))
        live = records(feature("A", WorkOrderStatus="Repair Underway"), feature("C"))

        change = build_delta(
            "2026-01-02T00:00:00+00:00", live, {"waste": 2}, previous, {"A", "B"}
        ).for_kind(sources.WORK_ORDER)

        self.assertEqual(set(change.appeared), {"C"})
        self.assertEqual(set(change.changed), {"A"})
        self.assertEqual(change.changed["A"], {"status": "Repair Underway"})
        self.assertEqual(change.resolved, ["B"])
        self.assertEqual(change.reappeared, {})

    def test_unchanged_faults_produce_nothing(self):
        previous = records(feature("A"))
        delta = build_delta("2026-01-02T00:00:00+00:00", records(feature("A")), {"waste": 1}, previous, {"A"})
        self.assertTrue(delta.is_empty())

    def test_a_fault_seen_again_is_a_reappearance_not_a_new_fault(self):
        change = build_delta(
            "2026-01-03T00:00:00+00:00", records(feature("A")), {"waste": 1}, {}, {"A"}
        ).for_kind(sources.WORK_ORDER)
        self.assertEqual(set(change.reappeared), {"A"})
        self.assertEqual(change.appeared, {})


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.deltas = self.root / "deltas"
        self.db = self.root / "faults.db"
        self.addCleanup(self.tmp.cleanup)

    def poll(self, day: int, live: dict, previous: dict, known: set[str]) -> store.Delta:
        stamp = (dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(days=day)).isoformat()
        delta = build_delta(stamp, live, {"waste": len(live)}, previous, known)
        store.write_delta(self.deltas, delta)
        return delta

    def replay(self):
        return store.rebuild(self.db, self.deltas)

    def test_full_lifecycle_is_reconstructed(self):
        day0 = records(feature("A"), feature("B"))
        self.poll(0, day0, {}, set())

        day1 = records(feature("A", WorkOrderStatus="Investigation"), feature("B"))
        self.poll(1, day1, day0, {"A", "B"})

        day2 = records(feature("A", WorkOrderStatus="Repair Underway"))  # B disappears
        self.poll(2, day2, day1, {"A", "B"})

        conn = self.replay()
        a = conn.execute("SELECT * FROM faults WHERE id = 'A'").fetchone()
        self.assertEqual(a["status"], "Repair Underway")
        self.assertEqual(a["is_open"], 1)
        self.assertEqual(a["first_seen_at"][:10], "2026-01-01")
        self.assertEqual(a["last_changed_at"][:10], "2026-01-03")

        b = conn.execute("SELECT * FROM faults WHERE id = 'B'").fetchone()
        self.assertEqual(b["is_open"], 0)
        self.assertEqual(b["resolved_at"][:10], "2026-01-03")

        statuses = conn.execute(
            "SELECT new_value FROM fault_events WHERE fault_id = 'A' "
            "AND (kind = 'appeared' OR field = 'status') ORDER BY id"
        ).fetchall()
        self.assertEqual([r[0] for r in statuses], ["Reported", "Investigation", "Repair Underway"])
        conn.close()

    def test_last_changed_at_does_not_track_mere_presence(self):
        """#22 — the column means what its name says, and nothing more.

        An unchanged record emits no delta entry, so `last_changed_at` cannot
        advance for one. That is correct, not a bug to be "fixed": maintaining a
        true last-seen would mean rewriting every live row every collection for a
        fact the schema already implies. This test exists so the semantics are
        pinned rather than rediscovered.
        """
        day0 = records(feature("A"), feature("B"))
        self.poll(0, day0, {}, set())
        # A changes on day 1; B is present and untouched throughout.
        day1 = records(feature("A", WorkOrderStatus="Investigation"), feature("B"))
        self.poll(1, day1, day0, {"A", "B"})
        self.poll(2, day1, day1, {"A", "B"})

        conn = self.replay()
        a, b = (conn.execute("SELECT * FROM faults WHERE id = ?", (i,)).fetchone() for i in ("A", "B"))
        conn.close()

        self.assertEqual(a["last_changed_at"][:10], "2026-01-02", "A last changed on day 1")
        self.assertEqual(b["last_changed_at"][:10], "2026-01-01",
                         "B was present on day 2 but unchanged since day 0")
        # Presence is still recoverable: B is live, so it was in every snapshot.
        self.assertEqual(b["is_open"], 1)
        self.assertIsNone(b["resolved_at"])

    def test_a_truncated_poll_records_counts_without_touching_history(self):
        """#21 — a source-side purge must be observable without being believed.

        Two failure modes, both bad: applying the implied departures writes tens
        of thousands of phantom closures, and discarding the poll loses the only
        evidence the source moved at all. A truncated delta keeps the counts and
        applies no record change, so the snapshot and the fault table disagree —
        and that disagreement is the signal.
        """
        day0 = records(feature("A"), feature("B"), feature("C"), feature("D"))
        self.poll(0, day0, {}, set())

        # The source comes back with a quarter of what it had.
        stamp = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc).isoformat()
        truncated = store.Delta(observed_at=stamp, source_counts={"waste": 1})
        truncated.truncated = {"work_order": 0.25}
        store.write_delta(self.deltas, truncated)

        conn = self.replay()
        open_now = conn.execute("SELECT count(*) FROM faults WHERE is_open = 1").fetchone()[0]
        self.assertEqual(open_now, 4, "no fault may be closed on a truncated poll")
        self.assertEqual(
            conn.execute("SELECT count(*) FROM fault_events WHERE kind = 'resolved'").fetchone()[0],
            0, "a truncated poll must not emit resolution events",
        )

        row = conn.execute("SELECT * FROM snapshots WHERE observed_at = ?", (stamp,)).fetchone()
        conn.close()
        self.assertIsNotNone(row, "the observation itself must survive")
        self.assertEqual(json.loads(row["truncated"]), {"work_order": 0.25})
        self.assertEqual(json.loads(row["source_counts"]), {"waste": 1},
                         "what the layer actually reported is kept")

    def test_an_anomalous_snapshot_is_applied_in_full(self):
        """#24 — magnitude flags, it does not veto.

        The counterpart to the truncated case: there the retrieval was suspect
        and nothing was applied. Here the retrieval was verified complete, so
        the departures are real and every one is recorded.
        """
        day0 = records(feature("A"), feature("B"), feature("C"), feature("D"))
        self.poll(0, day0, {}, set())

        stamp = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc).isoformat()
        delta = build_delta(stamp, records(feature("A")), {"waste": 1}, day0, set(day0))
        delta.anomalous = {"work_order": 0.25}
        store.write_delta(self.deltas, delta)

        conn = self.replay()
        self.assertEqual(
            conn.execute("SELECT count(*) FROM faults WHERE is_open = 0").fetchone()[0], 3,
            "a verified-complete poll is believed, however large the drop",
        )
        row = conn.execute("SELECT anomalous, truncated FROM snapshots WHERE observed_at = ?",
                           (stamp,)).fetchone()
        conn.close()
        self.assertEqual(json.loads(row["anomalous"]), {"work_order": 0.25})
        self.assertIsNone(row["truncated"], "nothing was withheld, so nothing is marked truncated")

    def test_anomalous_departures_are_kept_out_of_duration_stats_unless_corroborated(self):
        """#24 — record it all, but do not let one event rewrite every median.

        A source-side purge would otherwise register as thousands of faults
        resolved on the hour. Thames Water's own closed feed saying "Completed"
        is evidence; a record merely ceasing to be published is not.
        """
        from collector import build_site

        # Raised before the poll window, so a resolution has a positive duration.
        RAISED = 1764579600000
        day0 = records(*(feature(i, WorkOrderRaisedDate=RAISED) for i in "ABCD"))
        self.poll(0, day0, {}, set())

        stamp = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc).isoformat()
        live = records(feature("A", WorkOrderRaisedDate=RAISED))
        delta = build_delta(stamp, live, {"waste": 1}, day0, set(day0))
        delta.anomalous = {"work_order": 0.25}
        # B is corroborated by the closed feed; C and D just stopped appearing.
        delta.for_kind(sources.CLOSED).appeared = {
            "B": records(feature("B", WorkOrderStatus="Completed"), source="clean_closed")["B"],
        }
        store.write_delta(self.deltas, delta)

        conn = self.replay()
        conn.row_factory = sqlite3.Row
        result = build_site.summary(conn, dt.date(2026, 1, 2))
        conn.close()

        self.assertEqual(result["resolution"]["quarantined"], 2, "C and D are not evidence")
        self.assertEqual(result["resolution"]["n"], 1, "only the corroborated departure counts")

    def test_bulk_uncorroborated_departures_are_detected_after_the_fact(self):
        """#30 — the live guard misses collections that fall under its threshold.

        The 5 August 18:47 collapse took 4,142 records at 0.1% corroboration and
        was never flagged, because it was only ~20% of the total at the time. It
        still is not evidence of clearing, and drawn as such it is a bar 200x the
        median. Both tests are required: a large *confirmed* batch is real work.
        """
        from collector import build_site

        day0 = records(*(feature(f"F{i}") for i in range(60)))
        self.poll(0, day0, {}, set())

        # A big, wholly unconfirmed departure: 50 of 60 vanish, none closed.
        stamp = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc).isoformat()
        keep = records(*(feature(f"F{i}") for i in range(10)))
        store.write_delta(self.deltas, build_delta(stamp, keep, {"waste": 10}, day0, set(day0)))
        # Then ordinary churn, comfortably under the multiple.
        for day, n in ((2, 9), (3, 8), (4, 7), (5, 6)):
            later = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(days=day)
            nxt = records(*(feature(f"F{i}") for i in range(n)))
            store.write_delta(self.deltas, build_delta(
                later.isoformat(), nxt, {"waste": n},
                records(*(feature(f"F{i}") for i in range(n + 1))), set(day0)))

        conn = self.replay()
        conn.row_factory = sqlite3.Row
        flagged = build_site.uncorroborated_bulk_departures(conn)
        conn.close()
        self.assertIn(stamp, flagged, "a huge unconfirmed departure is not evidence of clearing")
        self.assertEqual(len(flagged), 1, "ordinary churn must not be swept up")

    def test_a_large_but_confirmed_departure_is_not_flagged(self):
        # The other half of the test: if Thames Water's closed feed says the work
        # was done, a big batch is real work and must count.
        from collector import build_site

        day0 = records(*(feature(f"F{i}") for i in range(60)))
        self.poll(0, day0, {}, set())

        stamp = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc).isoformat()
        keep = records(*(feature(f"F{i}") for i in range(10)))
        delta = build_delta(stamp, keep, {"waste": 10}, day0, set(day0))
        delta.for_kind(sources.CLOSED).appeared = {
            k: v for k, v in records(
                *(feature(f"F{i}", WorkOrderStatus="Completed") for i in range(10, 60)),
                source="waste_closed").items()
        }
        store.write_delta(self.deltas, delta)

        conn = self.replay()
        conn.row_factory = sqlite3.Row
        flagged = build_site.uncorroborated_bulk_departures(conn)
        conn.close()
        self.assertNotIn(stamp, flagged, "a confirmed batch is work, however large")

    def test_stage_buckets_never_exceed_the_observation_window(self):
        """#4 — a threshold we cannot observe must not be published as zero.

        A "held over 30 days" column, printed after ten days of watching, is a
        column of zeros that reads as "nothing is stuck" while meaning "we have
        not looked long enough to know". Buckets appear only as the history
        earns them.
        """
        from collector import build_site

        day0 = records(feature("A"), feature("B"))
        self.poll(0, day0, {}, set())
        self.poll(2, records(feature("A", WorkOrderStatus="Investigation"), feature("B")),
                  day0, {"A", "B"})

        conn = self.replay()
        conn.row_factory = sqlite3.Row
        result = build_site.stage_occupancy(conn)
        conn.close()

        # Two days of history: the 1-day bucket is earned, 7 and 30 are not.
        self.assertEqual(result["bucket_days"], [1])
        for stage in result["stages"]:
            self.assertEqual(len(stage["buckets"]), 1, stage["stage"])

    def test_stage_occupancy_counts_only_open_faults(self):
        from collector import build_site

        day0 = records(feature("A"), feature("B"), feature("C"))
        self.poll(0, day0, {}, set())
        # C departs; it must not be counted as sitting at a stage.
        self.poll(1, records(feature("A"), feature("B")), day0, {"A", "B", "C"})

        conn = self.replay()
        conn.row_factory = sqlite3.Row
        result = build_site.stage_occupancy(conn)
        conn.close()
        self.assertEqual(sum(s["n"] for s in result["stages"]), 2,
                         "a departed fault is not stuck anywhere")

    def test_backwards_transitions_are_counted(self):
        from collector import build_site

        day0 = records(feature("A", WorkOrderStatus="Repair Underway"))
        self.poll(0, day0, {}, set())
        back = records(feature("A", WorkOrderStatus="Investigation"))
        self.poll(1, back, day0, {"A"})

        conn = self.replay()
        conn.row_factory = sqlite3.Row
        result = build_site.stage_occupancy(conn)
        conn.close()
        self.assertEqual(result["backwards_total"], 1)
        self.assertEqual(result["backwards"][0][0], "Repair Underway → Investigation")

    def test_a_truncated_delta_is_never_considered_empty(self):
        # `is_empty` gates whether a delta is written at all; a counts-only
        # observation has no record ops and would otherwise be dropped.
        delta = store.Delta(observed_at="2026-01-02T00:00:00+00:00", source_counts={"waste": 1})
        self.assertTrue(delta.is_empty())
        delta.truncated = {"work_order": 0.25}
        self.assertFalse(delta.is_empty())

    def test_reappearance_reopens_without_duplicating(self):
        day0 = records(feature("A"))
        self.poll(0, day0, {}, set())
        self.poll(1, {}, day0, {"A"})           # vanishes
        self.poll(2, day0, {}, {"A"})           # comes back

        conn = self.replay()
        self.assertEqual(conn.execute("SELECT count(*) FROM faults").fetchone()[0], 1)
        row = conn.execute("SELECT * FROM faults WHERE id = 'A'").fetchone()
        self.assertEqual(row["is_open"], 1)
        self.assertIsNone(row["resolved_at"])
        self.assertEqual(row["reappearances"], 1)
        conn.close()

    def test_replay_is_deterministic(self):
        day0 = records(feature("A"), feature("B"))
        self.poll(0, day0, {}, set())
        day1 = records(feature("A", WorkOrderStatus="Investigation"))
        self.poll(1, day1, day0, {"A", "B"})

        def fingerprint():
            conn = self.replay()
            rows = conn.execute(
                "SELECT id, status, is_open, resolved_at, first_seen_at, last_changed_at "
                "FROM faults ORDER BY id"
            ).fetchall()
            events = conn.execute(
                "SELECT fault_id, observed_at, kind, field, new_value FROM fault_events ORDER BY id"
            ).fetchall()
            conn.close()
            return [tuple(r) for r in rows], [tuple(e) for e in events]

        self.assertEqual(fingerprint(), fingerprint())

    def test_snapshot_totals_are_recorded(self):
        day0 = records(feature("A"), feature("B"))
        self.poll(0, day0, {}, set())
        conn = self.replay()
        row = conn.execute("SELECT * FROM snapshots").fetchone()
        self.assertEqual(row["total"], 2)
        self.assertEqual(row["appeared"], 2)
        self.assertEqual(json.loads(row["source_counts"]), {"waste": 2})
        conn.close()

    def test_delta_files_sort_chronologically(self):
        # Replay order comes from the filename, so lexical order must match time.
        names = [
            store.delta_path(self.deltas, s).name
            for s in ("2026-01-09T23:00:00+00:00", "2026-01-10T01:00:00+00:00", "2026-02-01T00:00:00+00:00")
        ]
        self.assertEqual(names, sorted(names))

    def test_untracked_field_changes_do_not_create_events(self):
        day0 = records(feature("A"))
        self.poll(0, day0, {}, set())
        # LastModifiedDate ticks constantly; it is recorded but is not progress.
        day1 = records(feature("A", LastModifiedDate=1770265999000))
        delta = self.poll(1, day1, day0, {"A"})
        self.assertEqual(set(delta.for_kind(sources.WORK_ORDER).changed["A"]), {"last_modified_at"})

        conn = self.replay()
        self.assertEqual(
            conn.execute("SELECT count(*) FROM fault_events WHERE kind = 'changed'").fetchone()[0], 0
        )
        self.assertEqual(
            conn.execute("SELECT last_modified_at FROM faults WHERE id='A'").fetchone()[0],
            model.epoch_ms_to_iso(1770265999000),
        )
        conn.close()


def pin(global_id: str, **overrides):
    attrs = {
        "GlobalID": global_id,
        "OBJECTID": 1,
        "ProblemType": 1,
        "Street": "3 Mandeville Close",
        "Town": "Reading",
        "Postcode": "rg30 4jt",
        "CreationDate": 1785515580000,
        "EditDate": 1785515580000,
    }
    attrs.update(overrides)
    return {"attributes": attrs, "geometry": {"x": -1.02895, "y": 51.44715}}


def report_records(*features, source="reported"):
    out = {}
    for f in features:
        report_id, record = model.normalise_report(f, source)
        out[report_id] = record
    return out


class ReportNormalisationTests(unittest.TestCase):
    """Public reports: the layer behind the 'Leak' pins on Thames Water's map."""

    def test_maps_expected_fields(self):
        report_id, record = model.normalise_report(pin("abc-123"), "reported")
        self.assertEqual(report_id, "abc-123")
        self.assertEqual(record["street"], "3 Mandeville Close")
        self.assertEqual(record["town"], "Reading")
        self.assertEqual(record["problem_type"], 1)
        self.assertEqual(record["lat"], 51.44715)

    def test_postcode_is_normalised_like_work_orders(self):
        _, record = model.normalise_report(pin("abc-123"), "reported")
        self.assertEqual(record["postcode"], "RG30 4JT")
        self.assertEqual(record["outcode"], "RG30")

    def test_globalid_is_the_identity(self):
        # The map app only requests OBJECTID/Street/Postcode/Town, but the
        # layer also exposes a stable GlobalID -- without it these reports
        # could not be tracked at all.
        self.assertIsNone(model.normalise_report(pin("abc"), "reported")[1].get("id"))
        self.assertIsNone(model.normalise_report(pin(None), "reported"))
        self.assertIsNone(model.normalise_report(pin("   "), "reported"))

    def test_reported_at_comes_from_creation_date(self):
        _, record = model.normalise_report(pin("abc-123"), "reported")
        self.assertEqual(record["reported_at"], model.epoch_ms_to_iso(1785515580000))


class ReportTrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.deltas = self.root / "deltas"
        self.db = self.root / "faults.db"
        self.addCleanup(self.tmp.cleanup)

    def poll(self, day, *, faults=None, reports=None, prev_faults=None, prev_reports=None,
             known_faults=(), known_reports=()):
        stamp = (dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(days=day)).isoformat()
        delta = store.Delta(observed_at=stamp, source_counts={"waste": len(faults or {})})
        from collector.collect import classify

        classify(delta.for_kind(sources.WORK_ORDER), faults or {}, prev_faults or {}, set(known_faults))
        classify(delta.for_kind(sources.REPORT), reports or {}, prev_reports or {}, set(known_reports))
        store.write_delta(self.deltas, delta)
        return delta

    def test_reports_and_faults_land_in_separate_tables(self):
        faults = records(feature("A"))
        reports = report_records(pin("r-1"), pin("r-2", GlobalID="r-2"))
        self.poll(0, faults=faults, reports=reports)

        conn = store.rebuild(self.db, self.deltas)
        self.assertEqual(conn.execute("SELECT count(*) FROM faults").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM reports").fetchone()[0], 2)
        # A report must never be counted as an open fault: it would drag the
        # headline backlog age down with a week-old record.
        self.assertEqual(
            conn.execute("SELECT count(*) FROM faults WHERE is_open = 1").fetchone()[0], 1
        )
        row = conn.execute("SELECT * FROM reports WHERE id = 'r-1'").fetchone()
        self.assertEqual(row["postcode"], "RG30 4JT")
        self.assertEqual(row["is_current"], 1)
        self.assertIsNone(row["disappeared_at"])
        conn.close()

    def test_report_ageing_out_is_recorded_not_forgotten(self):
        # Thames Water keeps only ~7 days of these, so the day one drops out is
        # the only chance to record that it existed.
        reports = report_records(pin("r-1"))
        self.poll(0, reports=reports)
        self.poll(1, reports={}, prev_reports=reports, known_reports={"r-1"})

        conn = store.rebuild(self.db, self.deltas)
        row = conn.execute("SELECT * FROM reports WHERE id = 'r-1'").fetchone()
        self.assertEqual(row["is_current"], 0)
        self.assertEqual(row["disappeared_at"][:10], "2026-01-02")
        self.assertEqual(row["first_seen_at"][:10], "2026-01-01")
        conn.close()

    def test_reports_generate_no_fault_events(self):
        self.poll(0, reports=report_records(pin("r-1")))
        conn = store.rebuild(self.db, self.deltas)
        self.assertEqual(conn.execute("SELECT count(*) FROM fault_events").fetchone()[0], 0)
        conn.close()

    def test_old_deltas_without_a_kind_still_replay_as_work_orders(self):
        # The first delta ever written predates report collection and has no
        # "k" field; replaying it must not break or change meaning.
        legacy = store.Delta(observed_at="2026-01-01T00:00:00+00:00", source_counts={"waste": 1})
        legacy.for_kind(sources.WORK_ORDER).appeared = records(feature("A"))
        path = store.write_delta(self.deltas, legacy)
        body = gzip.decompress(path.read_bytes()).decode()
        self.assertNotIn('"k"', body)

        conn = store.rebuild(self.db, self.deltas)
        self.assertEqual(conn.execute("SELECT count(*) FROM faults").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM reports").fetchone()[0], 0)
        conn.close()

    def test_version_1_deltas_get_their_timestamps_corrected_on_replay(self):
        # Deltas written before the timezone fix hold BST timestamps an hour
        # ahead. They are corrected on read rather than rewritten, so the
        # committed log stays append-only and old numbers still reconcile.
        legacy = store.Delta(observed_at="2026-08-04T00:00:00+00:00", source_counts={"waste": 1})
        legacy.for_kind(sources.WORK_ORDER).appeared = {
            "A": {**records(feature("A"))["A"], "raised_at": "2026-08-01T13:21:49+00:00"}
        }
        path = store.write_delta(self.deltas, legacy)

        # Rewrite the meta line to claim v1, as the real early deltas do.
        lines = gzip.decompress(path.read_bytes()).decode().splitlines()
        lines[0] = json.dumps({**json.loads(lines[0]), "v": 1}, sort_keys=True)
        with path.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as fh:
            fh.write(("\n".join(lines) + "\n").encode())

        conn = store.rebuild(self.db, self.deltas)
        self.assertEqual(
            conn.execute("SELECT raised_at FROM faults WHERE id='A'").fetchone()[0],
            "2026-08-01T12:21:49+00:00",
        )
        conn.close()

    def test_current_deltas_are_not_shifted_again(self):
        # The same value written at the current version must survive replay
        # untouched, or every rebuild would walk timestamps backwards.
        delta = store.Delta(observed_at="2026-08-04T00:00:00+00:00", source_counts={"waste": 1})
        delta.for_kind(sources.WORK_ORDER).appeared = {
            "A": {**records(feature("A"))["A"], "raised_at": "2026-08-01T12:21:49+00:00"}
        }
        store.write_delta(self.deltas, delta)
        conn = store.rebuild(self.db, self.deltas)
        self.assertEqual(
            conn.execute("SELECT raised_at FROM faults WHERE id='A'").fetchone()[0],
            "2026-08-01T12:21:49+00:00",
        )
        conn.close()

    def test_report_ids_do_not_collide_with_fault_ids(self):
        # Same id in both feeds must stay two distinct records.
        faults = records(feature("SHARED"))
        reports = report_records(pin("SHARED"))
        self.poll(0, faults=faults, reports=reports)
        conn = store.rebuild(self.db, self.deltas)
        self.assertEqual(conn.execute("SELECT count(*) FROM faults WHERE id='SHARED'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM reports WHERE id='SHARED'").fetchone()[0], 1)
        conn.close()


class AnalysisTests(unittest.TestCase):
    """The derived figures published on the site (issues #2 and #3)."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "faults.db"
        deltas = Path(self.tmp.name) / "deltas"

        # Two authorities, deliberately different sizes: a raw count ranks the
        # big one worse, a rate ranks the small one worse.
        big = [feature(f"B{i:04d}", Postcode="TW10 6LX", MidLevelWorkType="Sewer blockage")
               for i in range(60)]
        small = [feature(f"S{i:04d}", Postcode="RG30 4JT",
                         JourneyType="Flooding",
                         MidLevelWorkType="Sewer flooding - external investigation")
                 for i in range(40)]
        delta = store.Delta(observed_at="2026-01-01T00:00:00+00:00", source_counts={"waste": 100})
        delta.for_kind(sources.WORK_ORDER).appeared = records(*big, *small)
        store.write_delta(deltas, delta)
        store.rebuild(self.db, deltas).close()

        reference = Path(self.tmp.name) / "reference"
        reference.mkdir()
        import gzip as gz

        with gz.open(reference / "postcode_la.json.gz", "wt") as fh:
            json.dump({"TW10 6LX": "E09000027", "RG30 4JT": "E06000038"}, fh)
        (reference / "la_households.json").write_text(json.dumps({
            "E09000027": {"name": "Richmond upon Thames", "households": 100_000},
            "E06000038": {"name": "Reading", "households": 10_000},
        }))

        from collector import build_site
        self.build_site = build_site
        self._patched = (build_site.POSTCODE_LA, build_site.LA_HOUSEHOLDS, build_site.MIN_FAULTS_PER_AREA)
        build_site.POSTCODE_LA = reference / "postcode_la.json.gz"
        build_site.LA_HOUSEHOLDS = reference / "la_households.json"
        build_site.MIN_FAULTS_PER_AREA = 10
        self.addCleanup(self.restore)

    def restore(self):
        (self.build_site.POSTCODE_LA, self.build_site.LA_HOUSEHOLDS,
         self.build_site.MIN_FAULTS_PER_AREA) = self._patched

    def connect(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def test_areas_rank_by_rate_not_raw_count(self):
        conn = self.connect()
        result = self.build_site.areas(conn, dt.date(2026, 3, 1))
        conn.close()
        self.assertTrue(result["available"])
        rows = {r["name"]: r for r in result["rows"]}
        # Richmond has more faults, Reading has far more per household.
        self.assertGreater(rows["Richmond upon Thames"]["n"], rows["Reading"]["n"])
        self.assertEqual(rows["Richmond upon Thames"]["per_10k"], 6.0)
        self.assertEqual(rows["Reading"]["per_10k"], 40.0)
        self.assertEqual(result["rows"][0]["name"], "Reading", "should rank by rate")

    def test_areas_report_coverage_and_drop_unplaceable_postcodes(self):
        conn = self.connect()
        # B0000-B0009: ten of the hundred faults get an unplaceable postcode.
        conn.execute("UPDATE faults SET postcode = 'ZZ99 9ZZ' WHERE id LIKE 'B000%'")
        conn.commit()
        result = self.build_site.areas(conn, dt.date(2026, 3, 1))
        conn.close()
        self.assertEqual(result["unplaced"], 10)
        self.assertAlmostEqual(result["coverage"], 90.0, places=1)

    def test_areas_degrade_gracefully_without_reference_data(self):
        self.build_site.POSTCODE_LA = Path(self.tmp.name) / "missing.json.gz"
        conn = self.connect()
        result = self.build_site.areas(conn, dt.date(2026, 3, 1))
        conn.close()
        self.assertFalse(result["available"])
        self.assertEqual(result["rows"], [])

    def test_external_flooding_counts_only_that_work_type(self):
        conn = self.connect()
        result = self.build_site.external_sewer_flooding(conn, dt.date(2026, 3, 1))
        conn.close()
        self.assertEqual(result["open"], 40, "must not include the 60 blockages")
        self.assertEqual(result["work_type"], "Sewer flooding - external investigation")
        self.assertEqual(result["by_status"], {"Reported": 40})


class SourceDispatchTests(unittest.TestCase):
    """Guards the bug where a third source kind used the wrong normaliser."""

    def test_every_source_kind_has_a_normaliser(self):
        from collector.collect import NORMALISERS

        for source in sources.SOURCES:
            self.assertIn(source.kind, NORMALISERS, f"{source.key} has no normaliser")

    def test_every_source_kind_has_a_table(self):
        for source in sources.SOURCES:
            self.assertIn(source.kind, store.SPECS, f"{source.key} has no table spec")

    def test_closed_work_orders_use_the_work_order_normaliser(self):
        # They were silently going through normalise_report, which produced
        # report-shaped rows keyed on GlobalID instead of WorkOrderID.
        from collector.collect import NORMALISERS

        normalise = NORMALISERS[sources.CLOSED]
        record_id, record = normalise(feature("WO9", WorkOrderStatus="Completed"), "clean_closed")
        self.assertEqual(record_id, "WO9", "must key on WorkOrderID")
        self.assertEqual(record["status"], "Completed")
        self.assertIn("work_order_number", record)
        self.assertNotIn("reported_at", record, "must not be a report record")


class ClosureOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "faults.db"
        deltas = Path(self.tmp.name) / "deltas"

        # Day 0: three faults open.
        day0 = records(feature("A"), feature("B"), feature("C"))
        d0 = store.Delta(observed_at="2026-01-01T00:00:00+00:00", source_counts={"waste": 3})
        d0.for_kind(sources.WORK_ORDER).appeared = day0
        store.write_delta(deltas, d0)

        # Day 1: A and B leave the open feed; both turn up closed, one cancelled.
        d1 = store.Delta(observed_at="2026-01-02T00:00:00+00:00", source_counts={"waste": 1})
        d1.for_kind(sources.WORK_ORDER).resolved = ["A", "B"]
        d1.for_kind(sources.CLOSED).appeared = {
            "A": records(feature("A", WorkOrderStatus="Completed"), source="clean_closed")["A"],
            "B": records(feature("B", WorkOrderStatus="Canceled"), source="clean_closed")["B"],
            # A closed record we never saw open, which is the common case.
            "Z": records(feature("Z", WorkOrderStatus="Completed"), source="clean_closed")["Z"],
        }
        store.write_delta(deltas, d1)
        store.rebuild(self.db, deltas).close()

    def test_closed_records_land_in_their_own_table(self):
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT count(*) FROM closed_faults").fetchone()[0], 3)
        # `faults` must stay exactly what the open feed said.
        self.assertEqual(conn.execute("SELECT count(*) FROM faults").fetchone()[0], 3)
        self.assertIsNone(
            conn.execute("SELECT id FROM faults WHERE id='Z'").fetchone(),
            "a closed-only record must not appear in faults",
        )
        conn.close()

    def test_outcomes_separate_completed_from_cancelled(self):
        from collector import build_site

        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        result = build_site.closure_outcomes(conn)
        conn.close()

        self.assertEqual(result["departed"], 2)
        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["matched_by_status"], {"Completed": 1, "Canceled": 1})
        self.assertEqual(result["unexplained"], 0)
        self.assertEqual(result["listed_total"], 3)

    def test_repair_complete_is_not_counted_as_an_outcome(self):
        """#32 — the status that sounds like a verdict and is not one.

        Thames Water applies "Repair Complete" to records that overwhelmingly
        still carry open line items — 79.8% of the closed feed's, against 2.8%
        of those marked "Completed" — and 688 of 947 observed transitions into
        it saw that count rise rather than fall. Counting it as accounted-for
        made "of the faults we can explain, x% were cancelled" imply the rest
        were repaired, which is exactly the overstatement this site exists to
        catch.
        """
        from collector import build_site

        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE closed_faults SET status='Repair Complete' WHERE id='A'")
        conn.commit()
        conn.row_factory = sqlite3.Row
        result = build_site.closure_outcomes(conn)
        conn.close()

        self.assertEqual(result["departed"], 2)
        self.assertEqual(result["matched"], 1, "only the cancellation says what happened")
        self.assertEqual(result["matched_by_status"], {"Canceled": 1})
        self.assertNotIn("Repair Complete", result["matched_by_status"])
        self.assertEqual(result["inconclusive"], 1)
        self.assertEqual(result["inconclusive_status"], "Repair Complete")
        # It is still a departure we cannot account for, so it must not vanish
        # from the arithmetic — departed = matched + unexplained throughout.
        self.assertEqual(result["unexplained"], 1)
        self.assertEqual(result["matched"] + result["unexplained"], result["departed"])

    def test_open_payload_ships_the_line_item_count(self):
        """The only field that contradicts a finished-sounding status (#32)."""
        from collector import build_site

        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE faults SET status='Repair Complete', open_line_items=4 WHERE id='C'")
        conn.commit()
        conn.row_factory = sqlite3.Row
        payload = build_site.open_faults(conn, dt.date(2026, 1, 2))
        conn.close()

        i = payload["cols"]["id"].index("C")
        self.assertEqual(payload["cols"]["ol"][i], 4,
                         "the browser cannot flag the contradiction without this")

    def test_unmatched_departures_are_counted_not_assumed_fixed(self):
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM closed_faults WHERE id = 'B'")
        conn.commit()
        conn.row_factory = sqlite3.Row
        from collector import build_site

        result = build_site.closure_outcomes(conn)
        conn.close()
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["unexplained"], 1, "a departure with no closed record is unknown")

    def test_cleared_payload_carries_the_verdict_or_admits_it_has_none(self):
        """#20 — the browsable list of departures.

        The verdict column is the reason this view exists, so a departure the
        closed feed does not corroborate must come through as null rather than
        being quietly rendered as a repair.
        """
        from collector import build_site

        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM closed_faults WHERE id = 'B'")
        conn.commit()
        conn.row_factory = sqlite3.Row
        payload = build_site.cleared_faults(conn, dt.date(2026, 1, 2))
        conn.close()

        cols = payload["cols"]
        verdicts = dict(zip(cols["id"], (payload["dict"]["verdict"][v] for v in cols["v"])))
        self.assertEqual(verdicts, {"A": "Completed", "B": None})
        self.assertNotIn("C", verdicts, "a still-open fault must not appear")
        self.assertNotIn("Z", verdicts, "a closed-only record was never observed departing")

    def test_cleared_payload_locates_a_departure_to_the_collection_not_the_day(self):
        """#23 — hourly collection is pointless if the payload rounds to days."""
        from collector import build_site

        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE faults SET resolved_at='2026-01-02T18:47:59+00:00' WHERE id='A'")
        conn.commit()
        conn.row_factory = sqlite3.Row
        payload = build_site.cleared_faults(conn, dt.date(2026, 1, 2))
        conn.close()

        moment = payload["cols"]["t"][payload["cols"]["id"].index("A")]
        self.assertEqual(
            dt.datetime.fromtimestamp(moment, dt.timezone.utc).isoformat(),
            "2026-01-02T18:47:59+00:00",
            "the time of day must survive into the payload",
        )
        # "In the last N hours" counts back from here, so it has to be present.
        self.assertIsNotNone(payload["latest"], "the newest collection moment must be published")

    def test_cleared_payload_respects_its_retention_window(self):
        from collector import build_site

        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        # A year on, both departures are outside a 90-day window.
        payload = build_site.cleared_faults(conn, dt.date(2027, 1, 2))
        conn.close()
        self.assertEqual(payload["cols"]["id"], [])
        self.assertEqual(payload["window_days"], build_site.CLEARED_WINDOW_DAYS)

    def test_cleared_payload_is_newest_departure_first(self):
        from collector import build_site

        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE faults SET resolved_at='2026-01-01T00:00:00+00:00' WHERE id='A'")
        conn.commit()
        conn.row_factory = sqlite3.Row
        payload = build_site.cleared_faults(conn, dt.date(2026, 1, 2))
        conn.close()
        # B departed on the 2nd, A on the 1st: the view opens on what just went.
        self.assertEqual(payload["cols"]["id"], ["B", "A"])


class CrossLinkTests(unittest.TestCase):
    """Associating reports with work orders raised at the same address (#17)."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "faults.db"
        deltas = Path(self.tmp.name) / "deltas"

        raised = int(dt.datetime(2026, 1, 2, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
        late = int(dt.datetime(2026, 3, 1, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
        d = store.Delta(observed_at="2026-01-01T00:00:00+00:00", source_counts={"waste": 2})
        d.for_kind(sources.WORK_ORDER).appeared = {
            # Same address as the report below, raised the next day.
            **records(feature("HIT", Street="3 mandeville  close", Postcode="rg30 4jt",
                              WorkOrderRaisedDate=raised)),
            # Same address but two months later: outside the window.
            **records(feature("LATE", Street="3 MANDEVILLE CLOSE", Postcode="RG30 4JT",
                              WorkOrderRaisedDate=late)),
            # Different address entirely.
            **records(feature("OTHER", Street="9 OTHER ROAD", Postcode="RG30 4JT",
                              WorkOrderRaisedDate=raised)),
            # Same street, different house number, and Thames Water's own
            # comma-without-a-space formatting. A leak outside №3 is routinely
            # worked on under №5, so this has to match (#28).
            **records(feature("NEIGHBOUR", Street="5,MANDEVILLE CLOSE", Postcode="RG30 4JT",
                              WorkOrderRaisedDate=raised)),
        }
        reported = int(dt.datetime(2026, 1, 1, 16, 33, tzinfo=dt.timezone.utc).timestamp() * 1000)
        d.for_kind(sources.REPORT).appeared = report_records(
            pin("rep-1", Street="3 Mandeville Close", Postcode="RG30 4JT", CreationDate=reported)
        )
        store.write_delta(deltas, d)
        store.rebuild(self.db, deltas).close()

    def links(self):
        from collector import build_site

        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return build_site.cross_links(conn)
        finally:
            conn.close()

    def test_matches_across_whitespace_and_case(self):
        by_report, by_fault = self.links()
        self.assertIn("rep-1", by_report)
        self.assertIn("HIT", [m["id"] for m in by_report["rep-1"]])
        self.assertIn("HIT", by_fault)
        self.assertEqual(by_fault["HIT"][0]["id"], "rep-1")

    def test_matches_a_neighbouring_house_on_the_same_street(self):
        """#28 — `Street` is an address line, not a street.

        Keying on it raw meant a report at №3 could never reach a work order at
        №5, which is the commonest shape of a real link: a leak in the road
        outside one house gets worked on under its neighbour's address. It cost
        roughly half of all links, and the report at 3 Mandeville Close that
        exposed it went a week showing nothing while a Visible Leak
        Investigation sat on the Faults page one door away.
        """
        by_report, _ = self.links()
        self.assertIn("NEIGHBOUR", [m["id"] for m in by_report["rep-1"]])

    def test_street_name_strips_house_numbers_but_not_street_names(self):
        from collector.build_site import _street_name

        for raw, expected in (
            ("3 Mandeville Close", "MANDEVILLE CLOSE"),
            ("5,MANDEVILLE CLOSE", "MANDEVILLE CLOSE"),
            ("  3   mandeville  close ", "MANDEVILLE CLOSE"),
            ("47A TILEHURST ROAD", "TILEHURST ROAD"),
            # Thames Water also puts the number at the end.
            ("TILEHURST ROAD 47A", "TILEHURST ROAD"),
            # A name with no number at all must survive intact.
            ("NORTH LODGE", "NORTH LODGE"),
        ):
            self.assertEqual(_street_name(raw), expected, raw)

    def test_ignores_work_orders_outside_the_window(self):
        by_report, _ = self.links()
        self.assertNotIn("LATE", [m["id"] for m in by_report["rep-1"]])

    def test_ignores_a_different_street_in_the_same_postcode(self):
        by_report, _ = self.links()
        self.assertNotIn("OTHER", [m["id"] for m in by_report["rep-1"]])

    def test_house_numbers_are_not_mistaken_for_street_names(self):
        # Stripping must not be so eager that two different streets collapse
        # together: '9 OTHER ROAD' is a different street, not a different number.
        by_report, _ = self.links()
        self.assertNotIn("OTHER", [m["id"] for m in by_report["rep-1"]])

    def test_keeps_every_candidate_rather_than_picking_one(self):
        # Ambiguity is real; silently choosing the closest would present a guess
        # as a fact.
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO faults (id, source, street, postcode, raised_at, status,"
            " first_seen_at, last_changed_at, is_open) VALUES"
            " ('TWIN','waste','3 MANDEVILLE CLOSE','RG30 4JT','2026-01-03T09:00:00+00:00',"
            " 'Reported','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00',1)"
        )
        conn.commit()
        conn.close()
        by_report, _ = self.links()
        # HIT and NEIGHBOUR (same street, next door) plus TWIN, raised a day later.
        self.assertEqual({m["id"] for m in by_report["rep-1"]}, {"HIT", "NEIGHBOUR", "TWIN"})
        # Sorted by lag, so the furthest in time is last. HIT and NEIGHBOUR share
        # a raised time, so assert on the ordering that is actually determinate.
        self.assertEqual(by_report["rep-1"][-1]["id"], "TWIN")

    def test_a_work_order_in_both_tables_is_listed_once(self):
        """#1 — a work order we watched close sits in `faults` *and*
        `closed_faults`. Listing the tables naively showed it twice, which the
        UI then reported as "more than one work order fits, so which followed
        from this report is ambiguous" — a fabricated ambiguity in the one place
        the site takes care not to present a guess as a fact.
        """
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO closed_faults (id, source, street, postcode, raised_at, status,"
            " first_seen_at, last_changed_at, is_listed) SELECT id, source, street, postcode,"
            " raised_at, 'Completed', first_seen_at, last_changed_at, 1 FROM faults WHERE id='HIT'"
        )
        conn.commit()
        conn.close()

        by_report, _ = self.links()
        hits = [m for m in by_report["rep-1"] if m["id"] == "HIT"]
        self.assertEqual(len(hits), 1, "the same work order must not appear twice")

    def test_a_departed_work_order_is_not_flagged_open(self):
        # The link payload's `open` decides whether the UI renders a clickable
        # link into the open-fault list. Marking a departed fault open produced
        # a link that silently did nothing.
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE faults SET is_open = 0, resolved_at = '2026-01-05T00:00:00+00:00'"
                     " WHERE id = 'HIT'")
        conn.commit()
        conn.close()

        by_report, _ = self.links()
        hit = next(m for m in by_report["rep-1"] if m["id"] == "HIT")
        self.assertEqual(hit["open"], 0, "a departed fault is not open")

    def test_a_report_with_no_address_matches_nothing(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE reports SET street = NULL WHERE id = 'rep-1'")
        conn.commit()
        conn.close()
        by_report, by_fault = self.links()
        self.assertEqual(by_report, {})
        self.assertEqual(by_fault, {})


class BuildSiteTests(unittest.TestCase):
    def test_day_index_round_trips(self):
        from collector.build_site import EPOCH, day_index

        self.assertEqual(day_index("2020-01-01T00:00:00+00:00"), 0)
        self.assertEqual(day_index("2020-01-02T23:59:59+00:00"), 1)
        self.assertIsNone(day_index(None))
        self.assertEqual(EPOCH.isoformat(), "2020-01-01")

    def test_age_buckets_are_exclusive_and_total(self):
        from collector.build_site import AGE_BUCKETS, _bucket

        counts = {}
        for days in range(0, 1200):
            counts[_bucket(days)] = counts.get(_bucket(days), 0) + 1
        self.assertEqual(sum(counts.values()), 1200)
        self.assertEqual(_bucket(1), "<=1")
        self.assertEqual(_bucket(2), "<=7")
        self.assertEqual(_bucket(AGE_BUCKETS[-1] + 1), f">{AGE_BUCKETS[-1]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class PermitTests(unittest.TestCase):
    """Street Manager permit extraction (#6)."""

    def test_promoter_filter_excludes_the_near_misses(self):
        from collector.permits import PROMOTER

        # Real promoter names from the July 2026 archive. Two of these are
        # councils whose name contains "Thames", and one is a separate company
        # building the super sewer — none of them is Thames Water.
        self.assertTrue(PROMOTER.search("THAMES WATER"))
        self.assertTrue(PROMOTER.search("Thames Water Utilities Ltd"))
        for other in ("ROYAL BOROUGH OF KINGSTON UPON THAMES",
                      "LONDON BOROUGH OF RICHMOND UPON THAMES",
                      "THAMES TIDEWAY TUNNEL LTD",
                      "SEVERN TRENT WATER"):
            self.assertIsNone(PROMOTER.search(other), other)

    def test_coordinates_are_parsed_from_the_wkt_point(self):
        from collector.permits import POINT

        m = POINT.search("POINT(465761 366901)")
        self.assertEqual((float(m.group(1)), float(m.group(2))), (465761.0, 366901.0))
        # Whitespace and decimals both occur in the archive.
        m = POINT.search("POINT (511234.5 178900.25)")
        self.assertEqual((float(m.group(1)), float(m.group(2))), (511234.5, 178900.25))
        self.assertIsNone(POINT.search(""))

    def test_later_permit_events_win_when_merging(self):
        """A permit's actual end date arrives on a later event than its grant."""
        import gzip, json as _json
        from collector import permit_join

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-07.ndjson.gz"
            with gzip.open(path, "wt") as out:
                out.write(_json.dumps({"op": "meta", "month": "2026-07"}) + "\n")
                out.write(_json.dumps({
                    "permit_reference_number": "P-1", "event_type": "PERMIT_GRANTED",
                    "easting": 500000.0, "northing": 180000.0,
                    "proposed_end_date": "2026-07-10T00:00:00.000Z",
                    "actual_end_date_time": None}) + "\n")
                out.write(_json.dumps({
                    "permit_reference_number": "P-1", "event_type": "WORK_STOP",
                    "easting": 500000.0, "northing": 180000.0,
                    "proposed_end_date": "2026-07-10T00:00:00.000Z",
                    "actual_end_date_time": "2026-07-14T00:00:00.000Z"}) + "\n")

            original = permit_join.PERMITS
            permit_join.PERMITS = Path(tmp)
            try:
                permits = permit_join.load_permits()
            finally:
                permit_join.PERMITS = original

        self.assertEqual(len(permits), 1, "events collapse to one row per permit")
        self.assertEqual(permits[0]["actual_end_date_time"], "2026-07-14T00:00:00.000Z")

    def test_events_without_coordinates_still_carry_their_end_date(self):
        """Dropping point-less events lost completions for the biggest works.

        2,959 of 16,653 permits in the July archive carry no coordinates at all,
        and they are disproportionately Major and Standard works — the ones that
        overrun most (54.3% late against 17.5%). Filtering them out at load time
        removed them from the overrun figures entirely.
        """
        import gzip, json as _json
        from collector import permit_join

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-07.ndjson.gz"
            with gzip.open(path, "wt") as out:
                out.write(_json.dumps({"op": "meta", "month": "2026-07"}) + "\n")
                out.write(_json.dumps({
                    "permit_reference_number": "P-2", "event_type": "PERMIT_GRANTED",
                    "work_category": "Major", "easting": None, "northing": None,
                    "proposed_end_date": "2026-07-10T23:00:00.000Z",
                    "actual_end_date_time": None}) + "\n")
                out.write(_json.dumps({
                    "permit_reference_number": "P-2", "event_type": "WORK_STOP",
                    "work_category": "Major", "easting": None, "northing": None,
                    "proposed_end_date": "2026-07-10T23:00:00.000Z",
                    "actual_end_date_time": "2026-07-20T10:00:00.000Z"}) + "\n")

            original = permit_join.PERMITS
            permit_join.PERMITS = Path(tmp)
            try:
                permits = permit_join.load_permits()
            finally:
                permit_join.PERMITS = original

        self.assertEqual(len(permits), 1)
        self.assertEqual(permit_join.days_late(permits[0]), 10)
        # ...and the join must still tolerate them rather than raising.
        self.assertEqual(permit_join._grid(permits, 50), {})

    def test_lateness_is_counted_in_london_calendar_days(self):
        """A deadline is a day, not an instant.

        Proposed ends are stored in UTC, so "end of 19 July" is 19T23:00Z —
        midnight BST. Work finishing at 09:00 the next morning is one day late.
        Subtracting the raw timestamps calls that "0.4 days over", which reads
        as a rounding artefact and understates every overrun by most of a day.
        """
        from collector.permit_join import days_late

        end_of_19_july = "2026-07-19T23:00:00.000Z"
        # Finished at 23:15 BST on the 19th — inside the last permitted day.
        self.assertEqual(days_late({"proposed_end_date": end_of_19_july,
                                    "actual_end_date_time": "2026-07-19T22:15:00.000Z"}), 0)
        # Finished at 09:00 BST on the 20th — one day late, not 0.4.
        self.assertEqual(days_late({"proposed_end_date": end_of_19_july,
                                    "actual_end_date_time": "2026-07-20T08:00:00.000Z"}), 1)
        # Finished two days early.
        self.assertEqual(days_late({"proposed_end_date": end_of_19_july,
                                    "actual_end_date_time": "2026-07-17T12:00:00.000Z"}), -2)
        # No recorded end: still running, or cancelled. Not an overrun.
        self.assertIsNone(days_late({"proposed_end_date": end_of_19_july,
                                     "actual_end_date_time": None}))

    def test_start_month_cohorts_drop_months_we_hold_no_archive_for(self):
        """#34 — a month missing its own archive is the wrong sample, not a small one.

        The only works visible from such a month are those that happened to
        finish in a later one, which selects precisely for the longest jobs.
        2026-06 shows 20.4% of its works running beyond ten days against
        1.5–5.3% everywhere else, on 657 works — no row-count threshold catches
        that, so it is excluded on the structural ground instead.
        """
        from collector import permit_join

        def work(month, day_len, late):
            start = f"2026-{month}-02T08:00:00.000Z"
            end = f"2026-{month}-{2 + day_len:02d}T08:00:00.000Z"
            # Deadline is the day the work ends, minus `late` days.
            due = f"2026-{month}-{2 + day_len - late:02d}T23:00:00.000Z"
            return ({"actual_start_date_time": start, "actual_end_date_time": end,
                     "proposed_end_date": due}, late)

        finished = ([work("05", 1, 1)] * 600) + ([work("06", 1, 1)] * 600)
        held = permit_join.by_start_month(finished, held={"2026-05"})
        self.assertEqual([r["month"] for r in held], ["2026-05"])
        # Without the filter both survive, so the filter is what excludes it —
        # not the 500-work threshold.
        either = permit_join.by_start_month(finished)
        self.assertEqual([r["month"] for r in either], ["2026-05", "2026-06"])

    def test_start_month_cohorts_carry_a_fully_observed_control(self):
        """The gap is only an argument if the control is counted the same way."""
        from collector import permit_join

        def work(days, late):
            end_day = 2 + days
            return ({"actual_start_date_time": "2026-05-02T08:00:00.000Z",
                     "actual_end_date_time": f"2026-05-{end_day:02d}T08:00:00.000Z",
                     "proposed_end_date": f"2026-05-{end_day - late:02d}T23:00:00.000Z"}, late)

        # 400 short jobs, none late; 200 long jobs, all late.
        finished = ([work(1, 0)] * 400) + ([work(20, 5)] * 200)
        row = permit_join.by_start_month(finished, held={"2026-05"})[0]
        self.assertEqual(row["n"], 600)
        self.assertEqual(row["short_n"], 400, "under-two-day jobs are the control")
        self.assertEqual(row["pct"], 33.3)
        self.assertEqual(row["short_pct"], 0.0)
        self.assertEqual(row["gap"], 33.3, "the gap is what the long jobs add")
        self.assertEqual(row["long_pct"], 33.3)

    def test_grid_cell_must_match_the_search_radius(self):
        """Indexing at one cell size and querying at another silently misses.

        This is not hypothetical: the first parameter sweep reported zero
        matches at 10m and 25m purely because the index was built at the 50m
        default, and it read as "no signal at tight radii" rather than a bug.
        """
        from collector import permit_join

        permits = [{"easting": 500000.0, "northing": 180000.0,
                    "proposed_start_date": "2026-07-05T00:00:00.000Z"}]
        faults = [{"id": "f1", "easting": 500008.0, "northing": 180000.0,
                   "raised_at": "2026-07-04T00:00:00+00:00"}]
        # 8m apart: inside 10m, and must be found when the radius says so.
        self.assertEqual(len(permit_join.match(faults, permits, 10)), 1)
        self.assertEqual(len(permit_join.match(faults, permits, 50)), 1)
        # 8m apart is outside a 5m radius, whatever the index granularity.
        self.assertEqual(len(permit_join.match(faults, permits, 5)), 0)

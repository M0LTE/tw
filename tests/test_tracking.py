"""Tests for the delta/replay machinery.

The whole point of the project is that yesterday's numbers still mean the same
thing tomorrow, so these focus on the tracking logic rather than the HTTP layer.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import model, store  # noqa: E402
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

        delta = build_delta("2026-01-02T00:00:00+00:00", live, {"waste": 2}, previous, {"A", "B"})

        self.assertEqual(set(delta.appeared), {"C"})
        self.assertEqual(set(delta.changed), {"A"})
        self.assertEqual(delta.changed["A"], {"status": "Repair Underway"})
        self.assertEqual(delta.resolved, ["B"])
        self.assertEqual(delta.reappeared, {})

    def test_unchanged_faults_produce_nothing(self):
        previous = records(feature("A"))
        delta = build_delta("2026-01-02T00:00:00+00:00", records(feature("A")), {"waste": 1}, previous, {"A"})
        self.assertTrue(delta.is_empty())

    def test_a_fault_seen_again_is_a_reappearance_not_a_new_fault(self):
        delta = build_delta("2026-01-03T00:00:00+00:00", records(feature("A")), {"waste": 1}, {}, {"A"})
        self.assertEqual(set(delta.reappeared), {"A"})
        self.assertEqual(delta.appeared, {})


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
        self.assertEqual(a["last_seen_at"][:10], "2026-01-03")

        b = conn.execute("SELECT * FROM faults WHERE id = 'B'").fetchone()
        self.assertEqual(b["is_open"], 0)
        self.assertEqual(b["resolved_at"][:10], "2026-01-03")

        statuses = conn.execute(
            "SELECT new_value FROM fault_events WHERE fault_id = 'A' "
            "AND (kind = 'appeared' OR field = 'status') ORDER BY id"
        ).fetchall()
        self.assertEqual([r[0] for r in statuses], ["Reported", "Investigation", "Repair Underway"])
        conn.close()

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
                "SELECT id, status, is_open, resolved_at, first_seen_at, last_seen_at "
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
        self.assertEqual(set(delta.changed["A"]), {"last_modified_at"})

        conn = self.replay()
        self.assertEqual(
            conn.execute("SELECT count(*) FROM fault_events WHERE kind = 'changed'").fetchone()[0], 0
        )
        self.assertEqual(
            conn.execute("SELECT last_modified_at FROM faults WHERE id='A'").fetchone()[0],
            model.epoch_ms_to_iso(1770265999000),
        )
        conn.close()


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

"""Normalisation of raw ArcGIS features into the flat records we store.

The two work-order layers share a schema, so one mapping covers both.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

# Fields we persist, in the order they appear in the ``faults`` table.
# ``id`` is WorkOrderID: a Salesforce record id, unique and non-null across all
# 20k live rows, and stable for the life of the work order.
FIELDS: tuple[str, ...] = (
    "source",
    "work_order_number",
    "case_number",
    "case_id",
    "case_record_type",
    "journey_type",
    "high_level_journey_type",
    "mid_level_work_type",
    "priority_flag",
    "status",
    "street",
    "thoroughfare",
    "postcode",
    "outcode",
    "city",
    "lon",
    "lat",
    "easting",
    "northing",
    "raised_at",
    "closure_at",
    "repair_complete_at",
    "last_modified_at",
    "open_line_items",
    "closed_line_items",
    "remain_on_map_hrs",
    "show_on_map",
)

# Changes to these fields are recorded as events in ``fault_events``. Everything
# else is treated as a correction to the record rather than progress on the fault.
TRACKED_FIELDS: tuple[str, ...] = (
    "status",
    "priority_flag",
    "journey_type",
    "high_level_journey_type",
    "mid_level_work_type",
    "closure_at",
    "repair_complete_at",
    "open_line_items",
    "closed_line_items",
    "street",
    "postcode",
    "city",
)

# The lifecycle Thames Water exposes, in the order work is meant to progress.
STATUS_ORDER: tuple[str, ...] = (
    "Reported",
    "Investigation",
    "Repair Planning",
    "Repair Underway",
    "Repair Complete",
)
STATUS_RANK = {name: i for i, name in enumerate(STATUS_ORDER)}

_OUTCODE = re.compile(r"^\s*([A-Z]{1,2}\d[A-Z\d]?)\s*\d[A-Z]{2}\s*$", re.I)

MIN_PLAUSIBLE_MS = 946_684_800_000    # 2000-01-01
MAX_PLAUSIBLE_MS = 4_102_444_800_000  # 2100-01-01


def epoch_ms_to_iso(value: Any) -> str | None:
    """ArcGIS dates are epoch milliseconds (UTC). Store them as ISO 8601."""
    if value in (None, ""):
        return None
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    # Sentinel values (0, negatives, far-future dates) turn up in the feed. A
    # work order stamped 1970 would dominate every "oldest fault" figure, so
    # anything outside 2000-2100 is treated as missing.
    if not MIN_PLAUSIBLE_MS <= ms <= MAX_PLAUSIBLE_MS:
        return None
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec="seconds")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return None if number is None else int(number)


def outcode_of(postcode: str | None) -> str | None:
    """The district half of a UK postcode, e.g. 'TW10 6LX' -> 'TW10'."""
    if not postcode:
        return None
    match = _OUTCODE.match(postcode)
    return match.group(1).upper() if match else None


def normalise(feature: dict, source_key: str) -> tuple[str, dict[str, Any]] | None:
    """Turn one ArcGIS feature into ``(fault_id, record)``.

    Returns None for features without a usable identity, which we cannot track.
    """
    attrs = feature.get("attributes") or {}
    fault_id = _text(attrs.get("WorkOrderID"))
    if not fault_id:
        return None

    geometry = feature.get("geometry") or {}
    postcode = _text(attrs.get("Postcode"))
    postcode = postcode.upper() if postcode else None

    record: dict[str, Any] = {
        "source": source_key,
        "work_order_number": _text(attrs.get("WorkOrderNumber")),
        "case_number": _text(attrs.get("CaseNumber")),
        "case_id": _text(attrs.get("CaseID")),
        "case_record_type": _text(attrs.get("CaseRecordType")),
        "journey_type": _text(attrs.get("JourneyType")),
        "high_level_journey_type": _text(attrs.get("HighLevelJourneyType")),
        "mid_level_work_type": _text(attrs.get("MidLevelWorkType")),
        "priority_flag": _text(attrs.get("PriorityFlag")),
        "status": _text(attrs.get("WorkOrderStatus")),
        "street": _text(attrs.get("Street")),
        "thoroughfare": _text(attrs.get("ThoroughFare")),
        "postcode": postcode,
        "outcode": outcode_of(postcode),
        "city": _text(attrs.get("City")),
        "lon": _num(geometry.get("x")),
        "lat": _num(geometry.get("y")),
        "easting": _num(attrs.get("OpenWorkOrderEasting")),
        "northing": _num(attrs.get("OpenWorkOrderNorthing")),
        "raised_at": epoch_ms_to_iso(attrs.get("WorkOrderRaisedDate")),
        "closure_at": epoch_ms_to_iso(attrs.get("WorkOrderClosureDate")),
        "repair_complete_at": epoch_ms_to_iso(attrs.get("WORepairCompleteDateTime")),
        "last_modified_at": epoch_ms_to_iso(attrs.get("LastModifiedDate")),
        "open_line_items": _int(attrs.get("OpenWorkOrderLineItemCount")),
        "closed_line_items": _int(attrs.get("ClosedWorkOrderLineItemCount")),
        "remain_on_map_hrs": _int(attrs.get("RemainOnMapInHrs")),
        "show_on_map": _text(attrs.get("ShowOnMapIndicator")),
    }
    return fault_id, record


def diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Fields in ``new`` whose value differs from ``old``."""
    return {k: v for k, v in new.items() if old.get(k) != v}


# --------------------------------------------------------------------------- #
# Public reports (the "pending pins" layer)
# --------------------------------------------------------------------------- #

# Problems reported by the public that have not yet become work orders. A far
# thinner record: no status, no work order number, no repair lifecycle — just
# what was reported, where, and when. Identity is the layer's GlobalID.
REPORT_FIELDS: tuple[str, ...] = (
    "source",
    "problem_type",
    "street",
    "postcode",
    "outcode",
    "town",
    "lon",
    "lat",
    "reported_at",
    "edited_at",
)

REPORT_TRACKED_FIELDS: tuple[str, ...] = ("street", "postcode", "town")


def normalise_report(feature: dict, source_key: str) -> tuple[str, dict[str, Any]] | None:
    """Turn one pending-pin feature into ``(report_id, record)``."""
    attrs = feature.get("attributes") or {}
    report_id = _text(attrs.get("GlobalID"))
    if not report_id:
        return None

    geometry = feature.get("geometry") or {}
    postcode = _text(attrs.get("Postcode"))
    postcode = postcode.upper() if postcode else None

    record: dict[str, Any] = {
        "source": source_key,
        "problem_type": _int(attrs.get("ProblemType")),
        "street": _text(attrs.get("Street")),
        "postcode": postcode,
        "outcode": outcode_of(postcode),
        "town": _text(attrs.get("Town")),
        "lon": _num(geometry.get("x")),
        "lat": _num(geometry.get("y")),
        "reported_at": epoch_ms_to_iso(attrs.get("CreationDate")),
        "edited_at": epoch_ms_to_iso(attrs.get("EditDate")),
    }
    return report_id, record

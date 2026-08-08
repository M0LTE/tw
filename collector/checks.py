"""Re-derive the figures quoted on the About page and in the README.

Every published figure is supposed to be reproducible from the committed change
log. Some are counts the site computes anyway; a few are one-off measurements
quoted as prose, and those are the ones that quietly go stale. This module
recomputes them so the claim can be checked rather than trusted.

    python -m collector.checks

It deliberately reuses ``build_site.cross_links`` rather than reimplementing
the address match. Two separate bugs have come from not doing so: an ad-hoc key
that treated a missing street as ``""`` bucketed every address-less record
together, and the production key itself silently required an identical house
number until #28. Measuring the thing the site actually does is the point.
"""

from __future__ import annotations

import collections
import sqlite3

from .build_site import DB, cross_links


def _pct(n: int, total: int) -> str:
    return f"{100 * n / total:.1f}%" if total else "–"


def leak_label(conn: sqlite3.Connection) -> None:
    """How well does the pending-pins layer's blanket "Leak" label hold up?

    ``ProblemType`` is 1 on every report, so the label cannot be per-report.
    The only available check is what kind of work order appeared on the same
    street shortly after. One vote per report, using its closest match in
    time — which is the candidate the site lists first.
    """
    by_report, _ = cross_links(conn)

    journeys: dict[str, str | None] = {}
    for table in ("faults", "closed_faults"):
        for row in conn.execute(f"SELECT id, journey_type FROM {table}"):
            journeys.setdefault(row["id"], row["journey_type"])

    counts = collections.Counter(
        journeys.get(hits[0]["id"]) for hits in by_report.values()
    )
    total = sum(counts.values())

    print(f'"Leak" label — {total} reports matched to a work order')
    for journey, n in counts.most_common():
        print(f"  {n:5d}  {_pct(n, total):>6}  {journey or '(no journey type)'}")
    leak = sum(n for journey, n in counts.items() if journey and "Leak" in journey)
    print(f"  → leak investigations: {leak}/{total} = {_pct(leak, total)}\n")


def report_links(conn: sqlite3.Connection) -> None:
    """The report-to-fault link rate, and the denominator it is not."""
    by_report, _ = cross_links(conn)
    total = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    addressable = conn.execute(
        "SELECT COUNT(*) FROM reports"
        " WHERE COALESCE(street,'') != '' AND COALESCE(postcode,'') != ''"
    ).fetchone()[0]
    ambiguous = sum(1 for hits in by_report.values() if len(hits) > 1)

    print("Report-to-fault links")
    print(f"  reports:                 {total}")
    print(f"  with a usable address:   {addressable}")
    print(f"  linked:                  {len(by_report)}  ({_pct(len(by_report), addressable)} of addressable)")
    print(f"  matching >1 work order:  {ambiguous}")
    print("  Not a conversion rate: address-less reports cannot match at all,")
    print("  and a report can be acted on without its own work order.\n")


def missing_addresses(conn: sqlite3.Connection) -> None:
    """Records the source publishes with no address.

    Reports carry a town; work orders do not, so the two are counted on the
    fields each actually has.
    """
    print("Records published with no address")
    total = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    blank = conn.execute(
        "SELECT COUNT(*) FROM reports WHERE COALESCE(street,'') = ''"
        " AND COALESCE(town,'') = '' AND COALESCE(postcode,'') = ''"
    ).fetchone()[0]
    print(f"  reports:      {blank:5d} / {total:6d}  {_pct(blank, total)}")

    for table in ("faults", "closed_faults"):
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        blank = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE COALESCE(street,'') = ''"
            " AND COALESCE(postcode,'') = ''"
        ).fetchone()[0]
        print(f"  {table:13s} {blank:5d} / {total:6d}  {_pct(blank, total)}")
    print()


def main() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        leak_label(conn)
        report_links(conn)
        missing_addresses(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

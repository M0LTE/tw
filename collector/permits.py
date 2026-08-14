"""Extract Thames Water's street works permits from DfT's Street Manager archive.

Street Manager is the statutory system every utility uses to apply for permission
to dig up a road in England. Its open data is what turns "this fault has been at
Repair Planning for three months" into a checkable question: was a permit ever
applied for, was it granted, and did the work overrun the dates Thames Water
themselves proposed?

Deliberately a separate concern from ``collect.py``. Different source, different
cadence, different failure modes — and this one publishes monthly in arrears,
where the fault map is polled hourly.

    python -m collector.permits --month 2026-07

## Finding the archive

The documentation describes only an SNS push subscription, which a static site
driven by a cron has nowhere to receive. There is also a public S3 bucket, which
neither the Street Manager docs nor the GOV.UK guidance mentions: the docs site's
own "Archived notifications" page is a JavaScript bucket browser, and its
``config.bucketUrl`` points at ``opendata.manage-roadworks.service.gov.uk``. It is
listable and readable unauthenticated, so no endpoint and no credentials are
needed and the zero-infrastructure property survives.

Layout is ``permit/YYYY/MM.zip``, roughly a gigabyte a month holding about 1.1
million individual JSON files — one per permit lifecycle event. A month is
published on the first of the following month.

## Why only an extract is committed

A month of national permits is ~1.9 GB raw and overwhelmingly other people's
utilities. Thames Water's share is a small fraction, so the archive is streamed
and filtered and only the filtered records are committed, keeping the same
"derived from a committed record" property the fault change log has.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import logging
import re
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

log = logging.getLogger("permits")

ROOT = Path(__file__).resolve().parent.parent
PERMITS = ROOT / "data" / "permits"
ARCHIVE = "https://opendata.manage-roadworks.service.gov.uk"

# Matched on the promoter name rather than a hardcoded SWA code. The code is
# recorded in the extract so it can be checked, but an organisation can hold
# more than one and a name match fails visibly rather than silently returning
# nothing if a code changes.
PROMOTER = re.compile(r"THAMES WATER", re.I)

# `POINT(465761 366901)` — British National Grid eastings and northings, the
# same projection Thames Water publishes on every work order, so faults and
# permits can be joined by metric distance rather than fuzzy text.
POINT = re.compile(r"POINT\s*\(\s*(-?[\d.]+)\s+(-?[\d.]+)\s*\)", re.I)

# Kept per event. The archive carries about forty fields; these are the ones a
# permit-versus-fault comparison needs, and dropping the rest keeps the
# committed extract small enough to belong in git.
FIELDS = (
    "work_reference_number", "permit_reference_number",
    "promoter_swa_code", "promoter_organisation", "highway_authority",
    "street_name", "town", "usrn", "works_location_coordinates",
    "work_category", "activity_type", "traffic_management_type",
    "proposed_start_date", "proposed_end_date",
    "actual_start_date_time", "actual_end_date_time",
    "work_status", "permit_status", "permit_conditions",
    "is_traffic_sensitive", "works_location_type",
)


def month_url(month: str) -> str:
    year, mm = month.split("-")
    return f"{ARCHIVE}/permit/{year}/{mm}.zip"


def download(month: str, into: Path) -> Path:
    url = month_url(month)
    target = into / f"{month}.zip"
    log.info("fetching %s", url)
    digest = hashlib.md5()
    with urllib.request.urlopen(url, timeout=600) as response, target.open("wb") as handle:
        total = int(response.headers.get("Content-Length", 0))
        # S3 returns the object's MD5 as the ETag for single-part uploads, so a
        # byte-for-byte check is available for free. Content-Length alone is not
        # enough: it confirms the length, not the content.
        etag = (response.headers.get("ETag") or "").strip('"')
        read = 0
        while chunk := response.read(1 << 20):
            handle.write(chunk)
            digest.update(chunk)
            read += len(chunk)
        log.info("  %.0f MB", read / 1e6)
    if total and read != total:
        raise OSError(f"{url}: got {read} bytes, expected {total}")
    if etag and "-" not in etag and digest.hexdigest() != etag:
        raise OSError(f"{url}: MD5 {digest.hexdigest()} does not match ETag {etag}")
    return target


class TruncatedArchive(Exception):
    """The published zip has no central directory, so it is incomplete at source.

    Distinguished from a failed download because the remedy is different: there
    is nothing to retry. 2026-06 is a live example — 847 MB whose bytes match the
    ETag exactly, ending mid-filename, re-uploaded on 21 July. See #31.
    """


def extract(archive: Path) -> tuple[list[dict], int]:
    """Thames Water's events from one monthly archive, and the total scanned."""
    kept: list[dict] = []
    try:
        bundle_cm = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise TruncatedArchive(
            f"{archive.name} has no readable central directory — the published "
            f"archive is truncated, not the download") from exc
    with bundle_cm as bundle:
        names = bundle.namelist()
        log.info("scanning %d events", len(names))
        for i, name in enumerate(names):
            try:
                doc = json.loads(bundle.read(name))
            except (json.JSONDecodeError, KeyError, OSError):
                continue
            data = doc.get("object_data") or {}
            if not PROMOTER.search(data.get("promoter_organisation") or ""):
                continue
            record = {key: data.get(key) for key in FIELDS}
            record["event_type"] = doc.get("event_type")
            record["event_time"] = doc.get("event_time")
            point = POINT.search(record.get("works_location_coordinates") or "")
            record["easting"] = float(point.group(1)) if point else None
            record["northing"] = float(point.group(2)) if point else None
            kept.append(record)
            if i and i % 250_000 == 0:
                log.info("  %d/%d scanned, %d kept", i, len(names), len(kept))
    return kept, len(names)


def write(month: str, records: list[dict], scanned: int) -> Path:
    PERMITS.mkdir(parents=True, exist_ok=True)
    path = PERMITS / f"{month}.ndjson.gz"
    # Sorted so the committed file is stable: the archive's iteration order is
    # not guaranteed, and a reordered file would show as a whole-file diff.
    records.sort(key=lambda r: (r.get("permit_reference_number") or "",
                                r.get("event_time") or ""))
    with gzip.open(path, "wt", encoding="utf-8") as out:
        out.write(json.dumps({
            "op": "meta", "month": month, "source": month_url(month),
            "scanned": scanned, "kept": len(records),
            "promoter": PROMOTER.pattern,
        }, sort_keys=True) + "\n")
        for record in records:
            out.write(json.dumps(record, sort_keys=True) + "\n")
    log.info("wrote %s (%d events, %.1f KiB)", path.name, len(records),
             path.stat().st_size / 1024)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", help="YYYY-MM; defaults to the most recent published month")
    parser.add_argument("--keep", type=Path, help="directory to keep the downloaded archive in")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        stream=sys.stderr)

    # A month is published on the first of the next one, so the newest that can
    # exist is the previous calendar month.
    month = args.month or (dt.date.today().replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")

    with tempfile.TemporaryDirectory() as tmp:
        into = args.keep or Path(tmp)
        into.mkdir(parents=True, exist_ok=True)
        archive = into / f"{month}.zip"
        if not archive.exists():
            archive = download(month, into)
        records, scanned = extract(archive)

    if not records:
        log.error("no %s records in %s — has the promoter name changed?", PROMOTER.pattern, month)
        return 1
    write(month, records, scanned)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

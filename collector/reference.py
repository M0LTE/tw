"""Build the reference data the Places table needs to normalise fault counts.

Raw fault counts track how many people live somewhere, so a league table of them
mostly reports population. To get a rate we need a denominator, and to get a
denominator we need a real statistical geography rather than Thames Water's
free-text town names.

Two lookups, both cached into ``data/reference/`` and committed, so that
``build_site`` never needs the network and every published figure stays
reproducible from the repository alone:

* postcode -> local authority, from postcodes.io (Open Government Licence,
  derived from ONS/Ordnance Survey open data)
* local authority -> household count, from ONS Census 2021 table TS041 via the
  NOMIS API (``NM_2059_1``)

Run with ``python -m collector.reference``. It is incremental: postcodes already
in the cache are not looked up again, so a routine run costs a handful of
requests.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("reference")

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "faults.db"
REFERENCE = ROOT / "data" / "reference"
POSTCODE_LA = REFERENCE / "postcode_la.json.gz"
LA_HOUSEHOLDS = REFERENCE / "la_households.json"

POSTCODES_IO = "https://api.postcodes.io/postcodes"
BATCH = 100
# ONS Census 2021, TS041 "Number of households", local authority districts.
NOMIS_TS041 = (
    "https://www.nomisweb.co.uk/api/v01/dataset/NM_2059_1.data.csv"
    "?geography=TYPE154&measures=20100&select=geography_code,geography_name,obs_value"
)
USER_AGENT = "thames-water-fault-tracker (+https://github.com/M0LTE/tw)"


def _request(url: str, payload: dict | None = None, attempts: int = 4):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": USER_AGENT}
    if body:
        headers["Content-Type"] = "application/json"
    last: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(2**attempt)
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            log.warning("retrying (%s)", exc)
    raise RuntimeError(f"giving up on {url}") from last


def load_cache() -> dict[str, str]:
    if not POSTCODE_LA.exists():
        return {}
    with gzip.open(POSTCODE_LA, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def save_cache(mapping: dict[str, str]) -> None:
    REFERENCE.mkdir(parents=True, exist_ok=True)
    body = json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode()
    with POSTCODE_LA.open("wb") as raw, gzip.GzipFile(
        filename="", mode="wb", fileobj=raw, mtime=0
    ) as fh:
        fh.write(body)


def resolve_postcodes(postcodes: list[str], cache: dict[str, str]) -> dict[str, str]:
    """Add any postcodes missing from the cache. Unmatched ones are cached as ''."""
    missing = sorted(p for p in postcodes if p not in cache)
    if not missing:
        log.info("all %d postcodes already cached", len(postcodes))
        return cache

    log.info("looking up %d new postcodes in %d batches", len(missing), -(-len(missing) // BATCH))
    for i in range(0, len(missing), BATCH):
        batch = missing[i : i + BATCH]
        data = json.loads(_request(POSTCODES_IO, {"postcodes": batch}))
        for entry in data.get("result", []):
            result = entry.get("result")
            # Cache misses too, so a bad postcode is not retried every run.
            cache[entry["query"]] = (
                (result.get("codes", {}) or {}).get("admin_district", "") if result else ""
            )
        if (i // BATCH) % 20 == 0:
            log.info("  %d/%d", min(i + BATCH, len(missing)), len(missing))
        time.sleep(0.2)  # be polite to a free service
    return cache


def fetch_households() -> dict[str, dict]:
    log.info("fetching ONS Census 2021 TS041 household counts")
    text = _request(NOMIS_TS041).decode("utf-8-sig")
    out: dict[str, dict] = {}
    for line in text.splitlines()[1:]:
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 3 or not parts[0]:
            continue
        code, name, value = parts[0], parts[1], parts[2]
        try:
            out[code] = {"name": name, "households": int(float(value))}
        except ValueError:
            continue
    log.info("  %d local authorities", len(out))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", stream=sys.stderr
    )

    conn = sqlite3.connect(args.db)
    postcodes = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT postcode FROM faults WHERE postcode IS NOT NULL "
            "UNION SELECT DISTINCT postcode FROM reports WHERE postcode IS NOT NULL"
        )
    ]
    conn.close()

    cache = resolve_postcodes(postcodes, load_cache())
    save_cache(cache)
    matched = sum(1 for v in cache.values() if v)
    log.info(
        "postcode cache: %d entries, %d matched to a local authority (%.1f%%)",
        len(cache),
        matched,
        100 * matched / max(1, len(cache)),
    )

    REFERENCE.mkdir(parents=True, exist_ok=True)
    LA_HOUSEHOLDS.write_text(json.dumps(fetch_households(), sort_keys=True, indent=1))
    log.info("wrote %s and %s", POSTCODE_LA.name, LA_HOUSEHOLDS.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

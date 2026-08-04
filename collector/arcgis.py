"""Minimal ArcGIS REST client (stdlib only, so CI needs no dependencies)."""

from __future__ import annotations

import gzip
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

log = logging.getLogger(__name__)

USER_AGENT = "thames-water-fault-tracker (+https://github.com/M0LTE/tw)"
PAGE_SIZE = 2000
MAX_ATTEMPTS = 5
TIMEOUT = 120


class ArcGisError(RuntimeError):
    pass


def _get(url: str, params: dict[str, Any]) -> dict:
    """GET a JSON document, retrying transient failures with exponential backoff."""
    query = urllib.parse.urlencode({**params, "f": "json"})
    full = f"{url}?{query}"
    last: Exception | None = None

    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            delay = 2**attempt
            log.warning("retrying in %ss (%s)", delay, last)
            time.sleep(delay)
        try:
            req = urllib.request.Request(
                full, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                body = json.loads(raw)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last = exc
            continue

        # ArcGIS reports errors with HTTP 200 and an "error" key.
        if isinstance(body, dict) and "error" in body:
            err = body["error"]
            last = ArcGisError(f"{err.get('code')}: {err.get('message')} {err.get('details')}")
            # 4xx-style ArcGIS errors will not fix themselves; fail fast.
            if err.get("code") in (400, 403, 404):
                raise last
            continue

        return body

    raise ArcGisError(f"giving up on {url} after {MAX_ATTEMPTS} attempts") from last


def resolve_layer_id(service_url: str, layer_name: str) -> int:
    """Find a layer's numeric id by name, so we survive the service being republished."""
    meta = _get(service_url, {})
    for layer in [*meta.get("layers", []), *meta.get("tables", [])]:
        if layer.get("name") == layer_name:
            return int(layer["id"])
    available = [layer.get("name") for layer in meta.get("layers", [])]
    raise ArcGisError(f"layer {layer_name!r} not found in {service_url} (have {available})")


def query_all(layer_url: str) -> Iterator[dict]:
    """Page through every feature in a layer, in OBJECTID order.

    A stable sort matters: without it ArcGIS gives no ordering guarantee across
    pages, so ``resultOffset`` paging can silently skip or duplicate rows.
    """
    offset = 0
    while True:
        page = _get(
            layer_url + "/query",
            {
                "where": "1=1",
                "outFields": "*",
                "outSR": 4326,
                "returnGeometry": "true",
                "orderByFields": "OBJECTID ASC",
                "resultOffset": offset,
                "resultRecordCount": PAGE_SIZE,
            },
        )
        features = page.get("features", [])
        if not features:
            return
        yield from features
        offset += len(features)
        if not page.get("exceededTransferLimit") and len(features) < PAGE_SIZE:
            return


def layer_count(layer_url: str) -> int:
    return int(_get(layer_url + "/query", {"where": "1=1", "returnCountOnly": "true"})["count"])

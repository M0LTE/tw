"""Registry of the ArcGIS feature services behind Thames Water's public fault map.

The map at https://www.thameswater.co.uk/help/report-a-problem#/view-problems-map
is a React app (``varpo-ui``) that renders ArcGIS feature layers hosted on
services2.arcgis.com under Thames Water's org id ``g6o32ZDQ33GpCIu3``. The layers
are public and queryable, so we read them directly rather than scraping the map.

Layer ids inside a FeatureServer have changed before (the app itself carries both
a ``*PRD`` and a legacy service name for each), so we resolve layers by *name*
at run time instead of hard-coding an index.
"""

from __future__ import annotations

from dataclasses import dataclass

ORG = "g6o32ZDQ33GpCIu3"
BASE = f"https://services2.arcgis.com/{ORG}/arcgis/rest/services"


WORK_ORDER = "work_order"
REPORT = "report"


@dataclass(frozen=True)
class Source:
    """One feature layer we poll."""

    key: str
    """Short stable identifier, stored on every row we collect from this layer."""

    label: str
    service: str
    layer_name: str
    kind: str = WORK_ORDER
    """Work orders and public reports have different schemas and live in
    different tables; see collector/schema.sql."""

    @property
    def service_url(self) -> str:
        return f"{BASE}/{self.service}/FeatureServer"


SOURCES: tuple[Source, ...] = (
    Source(
        key="waste",
        label="Waste water",
        service="WWOPWOPRD",
        layer_name="WasteWaterOpenWorkOrder",
    ),
    Source(
        key="clean",
        label="Clean water",
        service="CWOPWOPRD",
        layer_name="CleanWaterOpenWorkOrder",
    ),
    # Problems the public has reported that have not yet become work orders.
    # The map labels these simply "Leak". Thames Water keeps only a rolling
    # seven days of them, so anything not collected daily is lost for good.
    Source(
        key="reported",
        label="Public reports",
        service="Public_Website_Pending_Pins",
        layer_name="Point layer",
        kind=REPORT,
    ),
)

SOURCES_BY_KEY = {s.key: s for s in SOURCES}

WORK_ORDER_SOURCES = tuple(s for s in SOURCES if s.kind == WORK_ORDER)
REPORT_SOURCES = tuple(s for s in SOURCES if s.kind == REPORT)

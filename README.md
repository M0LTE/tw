# Thames Water fault tracker

Thames Water publishes a [map of current problems](https://www.thameswater.co.uk/help/report-a-problem#/view-problems-map)
— leaks, blockages, flooding, pollution — but it only ever shows *now*. A fault open for
two years looks exactly like one raised this morning, and once a pin disappears there is no
record it was ever there.

This project snapshots that map every day and keeps the history, so you can ask the
questions the map cannot answer:

- Is the backlog growing or shrinking?
- How long does a leak actually take to fix?
- How many faults have been open for over a year?
- Which towns wait longest?

At the time of writing the two feeds carry **~20,000 open work orders**, with a median age
of **68 days**, **989 open for more than a year**, and the oldest raised in **August 2023**.

---

## How it works

```
ArcGIS feature layers  ──▶  collector/collect.py  ──▶  data/deltas/*.ndjson.gz   (committed)
                                                              │
                                                              ▼
                                                    collector/store.py  replay
                                                              │
                                                              ▼
                                                       data/faults.db     (derived)
                                                              │
                                                              ▼
                                              collector/build_site.py  ──▶  web/data/*.json
                                                                                  │
                                                                                  ▼
                                                                          GitHub Pages
```

A twice-daily GitHub Action polls the feeds, appends a change log entry, rebuilds the database
and republishes the site.

### Why a change log rather than daily database commits

Each poll returns ~20,000 records, about 22 MB of JSON. Committing that daily would add
gigabytes to git within a year. Instead each run commits only what *changed*: new faults in
full, changed faults as a patch of the changed fields, and a tombstone for faults that
dropped out of the feed. In practice that is **40–50 KB a day**.

`data/faults.db` is therefore a derived artefact, rebuilt from the change log on every run
and never committed. The upshot is that every figure on the site is reproducible from the
raw record — clone the repo, run one command, and you get the same database.

## The data source

The map at `thameswater.co.uk/help/report-a-problem#/view-problems-map` is a React app
(`varpo-ui`) which renders public ArcGIS feature layers hosted under Thames Water's
organisation id `g6o32ZDQ33GpCIu3`. Two layers carry the faults:

| Layer | Service | Rows | What it is |
|---|---|---|---|
| `CleanWaterOpenWorkOrder` | `CWOPWOPRD/FeatureServer` | ~9,500 | Work orders |
| `WasteWaterOpenWorkOrder` | `WWOPWOPRD/FeatureServer` | ~10,700 | Work orders |
| `CleanWaterClosedWorkOrder` | `CWCLWOPRD/FeatureServer` | ~500 | Closed work orders |
| `WasteWaterClosedWorkOrder` | `WWCLWOPRD/FeatureServer` | ~1,500 | Closed work orders |
| `Point layer` | `Public_Website_Pending_Pins/FeatureServer` | ~2,100 | Public reports |

All read unauthenticated, with `outSR=4326` for WGS84 coordinates. Layer *ids* inside each
service have changed before, so `collector/arcgis.py` resolves them by name at run time.

Each **work order** carries a stable Salesforce id, the raised date, a status along the
lifecycle `Reported → Investigation → Repair Planning → Repair Underway → Repair Complete`,
a priority flag, street/postcode/town, and a point geometry. Every one of the ~20,000 live
records has a non-null, unique `WorkOrderID`, which is what we key history on.

### The pending pins matter more than they look

When someone reports a problem it appears on the map immediately as a pin the app simply
labels **"Leak"** — before any work order exists. Those pins have no reference number and no
repair status, and **Thames Water keeps only a rolling ~7 days of them**. A report that never
becomes a work order therefore disappears from the public record entirely, with nothing to
show it was ever made. Daily collection is the only way to see how many reports actually
turn into work.

They live in their own `reports` table rather than in `faults`: a report has no status and
no lifecycle, and folding ~2,000 week-old records into the backlog would quietly wreck the
headline age figures.

> Note for anyone reading the layer definitions in the map's JS bundle: the app requests only
> `OBJECTID, Street, Postcode, Town` from this layer, but the layer itself also exposes
> `GlobalID`, `CreationDate` and `EditDate`. The `GlobalID` is unique and non-null, which is
> what makes these trackable at all — read the layer metadata, not the app's `outFields`.

The map draws other layers we still ignore: planned improvement works and supply-interruption
bulletins, neither of which is a fault.

## Data model

`faults` — one row per work order ever seen, holding Thames Water's own fields plus our
observations: `first_seen_at`, `last_seen_at`, `resolved_at`, `is_open`, `reappearances`.

`fault_events` — every observed change: `appeared`, `changed` (with field, old and new
value), `resolved`, `reappeared`. This is what drives the per-fault timeline.

`reports` — one row per public report ever seen, with `reported_at` (Thames Water's own
timestamp), `first_seen_at`, `last_seen_at` and `disappeared_at`. Because the source keeps
only ~7 days, `disappeared_at` is the record that a report existed at all.

`closed_faults` — work orders Thames Water publishes as finished, carrying a
`WorkOrderStatus` of `Completed` or `Canceled`. Kept apart from `faults` so that table stays
exactly what the open feed said; they join on `id` at build time. This is the only way to
tell a repair from a cancellation.

`snapshots` — one row per collection run, with per-source counts.

See [`collector/schema.sql`](collector/schema.sql).

## Reference data

`data/reference/` holds two committed lookups so `build_site` never needs the network and
every published rate stays reproducible from the repository:

| File | Contents | Source |
|---|---|---|
| `postcode_la.json.gz` | postcode → local authority code | [postcodes.io](https://postcodes.io), OGL |
| `la_households.json` | local authority → household count | ONS Census 2021 table TS041 (NOMIS `NM_2059_1`) |

Rebuild or top up with `python -m collector.reference`. It only looks up postcodes it has not
seen, so a routine run costs a handful of requests.

## Running it locally

No dependencies beyond Python 3.11+ — the collector is stdlib only.

```bash
python -m collector.collect        # poll, append a delta, rebuild the database
python -m collector.build_site     # emit web/data/*.json
python -m http.server -d web 8000  # open http://localhost:8000
```

Useful flags:

```bash
python -m collector.collect --rebuild-only   # rebuild the database, do not poll
python -m collector.collect --force          # write the delta even if the poll looks truncated
```

Run the tests with:

```bash
python -m unittest discover -s tests -v
```

## Setting up the scheduled run

1. **Settings → Pages → Source: GitHub Actions.**
2. Merge to the default branch. GitHub only fires `schedule` triggers there; until then use
   **Actions → Collect faults → Run workflow** to trigger it by hand.
3. The workflow needs no secrets — it uses the built-in `GITHUB_TOKEN`.

The workflow runs at 06:15 and 18:15 UTC. It runs the tests first, and refuses to write a delta
if the poll looks truncated (see below), so a bad day at the source cannot corrupt history.

## Caveats

These matter if you are going to quote the numbers at anyone.

- **"Cleared" means "stopped appearing"**, not "confirmed fixed", unless the fault also
  turns up in the closed feed. Where it does, Thames Water's own `Completed`/`Canceled`
  verdict is shown. Where it does not — currently about 40% of departures — we genuinely do
  not know what happened, and the site says so rather than assuming a repair.
- **Time-to-fix is measured from Thames Water's own raised date** to the day the fault left
  the map. Faults already open when tracking began are included, so if anything the figure
  flatters them.
- **Only faults Thames Water chooses to publish are here.** Records carry a
  `ShowOnMapIndicator` flag; anything they suppress is invisible to this and to the public.
- **Faults occasionally reappear** after dropping out for a day. They are counted once and
  flagged with `reappearances`, not double-counted as a new fault.
- **The feed can be republished mid-poll.** If fewer than half of the known open faults come
  back, the collector aborts rather than recording tens of thousands of phantom closures.
  Override with `--force` when a drop is genuine.
- **History starts the day collection starts.** Ages come from Thames Water's raised dates
  and so reach back to 2023, but backlog and flow trends only begin from the first snapshot.
- **Work order timestamps are published as UK local time with a UTC label.** We convert them
  on ingest; deltas written before this was spotted are corrected on replay. The public
  reports layer uses ArcGIS editor-tracking fields, which are already UTC.
- **"Leak" is Thames Water's label for the whole pending-pins layer**, not a per-report
  classification: `ProblemType` is `1` on every record. Checked empirically — of 268 pins that
  could be matched to a work order raised soon after at the same address, 254 (94.8%) were
  `Visible Leak Investigation`, 7 flooding, 6 a blockage and 1 pollution. So the label is
  broadly right but not exclusive, and it rests on the same inferred match as the caveat below.
- **The reference shown on their map is the case number, not the work order number.** Both
  are searchable here.
- **Report-to-fault links are inferred, not confirmed.** Where a report and a work order
  share a street and postcode within a week, the site links them. Measured at 268 of 2,300
  reports with a usable address; 29 match more than one work order, and all candidates are shown
  rather than one being picked. A null model — same addresses, dates shifted by 30 to 90 days —
  produces 1.8 matches against those 268 real ones, so the links themselves are sound in the
  sense that they are not coincidence. That still does not give a conversion
  *rate*, because reports without an address cannot match at all and a report can be acted on
  without generating its own work order.
- **A public report leaving the map does not mean it became a work order.** The two feeds
  share no key, so the conversion rate cannot be read off directly — only inferred from
  location and timing. Treat any such figure as an estimate.
- **A low fault rate for a local authority is not proof of good performance.** Thames Water
  supplies only part of some authorities, so faults are counted for their area while
  households are counted for the whole of it. Normalising by households corrects for size;
  it does not adjust for network age, pipe material or ground conditions.
- **External sewer flooding counts are work orders, not compensation cases.** They record
  that Thames Water raised an investigation, not that a qualifying flooding incident
  occurred or that any payment is due.

## Licence and attribution

Licensed under the [GNU Affero General Public License v3.0 or later](LICENSE). The AGPL's
network clause is the point: anyone running a modified version of this as a public website
has to publish their changes, so a fork cannot quietly alter how the numbers are counted.

Not affiliated with or endorsed by Thames Water Utilities Ltd. The underlying records are
Thames Water's own published data, reproduced unchanged. Map tiles © OpenStreetMap
contributors.

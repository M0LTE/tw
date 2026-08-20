# Thames Water fault tracker

Thames Water publishes a [map of current problems](https://www.thameswater.co.uk/help/report-a-problem#/view-problems-map)
— leaks, blockages, flooding, pollution — but it only ever shows *now*. A fault open for
two years looks exactly like one raised this morning, and once a pin disappears there is no
record it was ever there.

This project snapshots that map every hour and keeps the history, so you can ask the
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

An hourly GitHub Action polls the feeds, appends a change log entry, rebuilds the database
and republishes the site. A poll that finds nothing changed commits nothing and deploys nothing.

### Why a change log rather than snapshot database commits

Each poll returns ~20,000 records, about 22 MB of JSON. Committing that every hour would add
terabytes to git within a year. Instead each run commits only what *changed*: new faults in
full, changed faults as a patch of the changed fields, and a tombstone for faults that
dropped out of the feed. Delta size tracks observed change rather than poll count, so hourly
collection costs roughly **30 MB a year**, not twelve times the daily figure.

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
show it was ever made. Regular collection is the only way to see how many reports actually
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
observations: `first_seen_at`, `last_changed_at`, `resolved_at`, `is_open`, `reappearances`.

`fault_events` — every observed change: `appeared`, `changed` (with field, old and new
value), `resolved`, `reappeared`. This is what drives the per-fault timeline.

`reports` — one row per public report ever seen, with `reported_at` (Thames Water's own
timestamp), `first_seen_at`, `last_changed_at` and `disappeared_at`. Because the source keeps
only ~7 days, `disappeared_at` is the record that a report existed at all.

There is deliberately no "last seen" column. An unchanged record emits no change log entry —
that is the point of the delta design — so such a column could only ever record the last
*change*, which is what `last_changed_at` honestly says. Presence is already implied: a record
is live in every snapshot from `first_seen_at` until it resolves or disappears.

`closed_faults` — work orders Thames Water publishes as finished, carrying a
`WorkOrderStatus` of `Completed` or `Canceled`. Kept apart from `faults` so that table stays
exactly what the open feed said; they join on `id` at build time. This is the only way to
tell a repair from a cancellation.

`snapshots` — one row per collection run, with per-source counts.

**Stage occupancy is reported censored, not averaged.** `build_site.stage_occupancy` counts open
faults by lifecycle stage and how long since each last moved, as an "at least". A mean or median
time-in-stage is computable and would be wrong: a dwell can only be measured when both entry and
exit are observed, so a window of *n* days can only ever contain visits shorter than *n* days. On
the data that first produced it, the observed Investigation median was 0.17 days while 2,194 of the
2,991 faults sitting in Investigation had not moved for over five days. Bucket thresholds appear
only once the observation window can support them, so a "held over 30 days" column never prints a
misleading zero.

`data/notes.json` is the site's narrative log — hand-written, dated entries explaining anything
that would otherwise leave a figure unexplained. It is the only content on the site not derived
from the change log, because it is the only part that needs judgement: the data can show that
13,600 work orders stopped being published, but not what that meant. `build_site` validates it
(a mistyped block key would otherwise render as a silently missing paragraph) and emits it to
`web/data/notes.json`. Figures quoted in an entry must still be reproducible from `data/deltas/`.

`build_site` emits `web/data/cleared.json` alongside the open faults: every departure in the
last 90 days as a browsable record, carrying the closure date and the closed-feed verdict where
there is one. Open faults are a bounded set and cleared ones are not, hence the window; older
departures stay in the database and in the change log.

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
python -m collector.collect --force          # apply changes even if a layer could not be read fully
```

Run the tests with:

```bash
python -m unittest discover -s tests -v   # the collector
node --test 'tests/*.test.mjs'            # the browser's share of the logic
```

Both run in CI. The second is there because the page takes decisions of its own once the
JSON crosses to the browser — whether a status contradicts its own line-item count, whether
to admit that collection has stopped — and several of those encode findings this site argues
in public. It needs no dependencies: `node --test` is in the standard library, and the tests
import the pure helpers out of `web/app.js` directly.

Most figures on the site are counts `build_site` produces anyway. A few are one-off
measurements quoted as prose in the caveats below, and those are the ones that quietly go
stale, so they have a command of their own:

```bash
python -m collector.checks   # leak-label proportion, link rate, address-less records
```

## Setting up the scheduled run

1. **Settings → Pages → Source: GitHub Actions.**
2. Merge to the default branch. GitHub only fires `schedule` triggers there; until then use
   **Actions → Collect faults → Run workflow** to trigger it by hand.
3. The workflow needs no secrets — it uses the built-in `GITHUB_TOKEN`.

The workflow runs at quarter past every hour. It runs the tests first, and refuses to write a delta
if a layer could not be read completely (see below), so a failed read cannot corrupt history.

Hourly rather than daily because resolution not collected is gone for good, while bytes are cheap:
a run is 23 requests and under two minutes. Twice-daily collection could not tell whether the
2026-08-05 departure of 4,272 faults was a single purge or a bleed across the day.

## Caveats

These matter if you are going to quote the numbers at anyone.

- **"Cleared" means "stopped appearing"**, not "confirmed fixed", unless the fault also
  turns up in the closed feed. Where it does, Thames Water's own `Completed`/`Canceled`
  verdict is shown. Where it does not, we genuinely do not know what happened, and the site
  says so rather than assuming a repair. The Faults tab lists cleared faults with that verdict
  per record, filterable by clearance date — which is how to tell a working week's repairs from
  a bulk disappearance. **The corroborated share is not stable**: it was about 60% before
  2026-08-05, and 2.5% for the 4,272 faults that left in a single collection that evening.
- **Time-to-fix is measured from Thames Water's own raised date** to the day the fault left
  the map. Faults already open when tracking began are included, so if anything the figure
  flatters them.
- **Only faults Thames Water chooses to publish are here.** Records carry a
  `ShowOnMapIndicator` flag; anything they suppress is invisible to this and to the public.
- **Faults occasionally reappear** after dropping out for a day. They are counted once and
  flagged with `reappearances`, not double-counted as a new fault.
- **A poll is checked for completeness, not plausibility.** Every layer is bracketed with
  `returnCountOnly` queries: if the count before, the rows retrieved, and the count after all
  agree, the read is complete and whatever it says is recorded — however implausible. If they
  disagree, paging skipped rows or the layer changed underneath us, so the run records the
  counts and applies no record change. Override with `--force`.

  It deliberately does **not** refuse a drop for being too large. "Too big to be true" is not a
  data-integrity property, and a guard that discards observations on those grounds would put our
  editorial judgement ahead of the measurement. A drop that large instead flags the snapshot as
  `anomalous`.
- **Departures in a flagged collection are excluded from the duration figures** unless Thames
  Water's own closed feed corroborates them, and the excluded count is published alongside. They
  are also not drawn as "clearing" on the flow chart. A collection counts as flagged either from
  the live guard or, after the fact, when its departures exceed 20× the median collection *and*
  under 10% are corroborated — both tests, since a large confirmed batch is real work and a small
  unconfirmed one is noise. This is a test of what counts as *evidence*, not of what gets
  recorded: everything is stored and browsable regardless (see #24, #30).

  It matters more than it sounds. On 2026-08-05 their feed shed ~13,600 work orders, mostly work
  that had been open for months; counting their disappearance as repair put the site's headline
  time-to-clear at **22 days** when departures either side of the event ran at 1.5 and 3.0 days.
  The corrected figure is 3 days.
- **History starts the day collection starts.** Ages come from Thames Water's raised dates
  and so reach back to 2023, but backlog and flow trends only begin from the first snapshot.
- **Work order timestamps are published as UK local time with a UTC label.** We convert them
  on ingest; deltas written before this was spotted are corrected on replay. The public
  reports layer uses ArcGIS editor-tracking fields, which are already UTC.
- **"Leak" is Thames Water's label for the whole pending-pins layer**, not a per-report
  classification: `ProblemType` is `1` on every record. Checked empirically — of 1,029 pins that
  could be matched to a work order raised soon after on the same street, 950 (92.3%) were
  `Visible Leak Investigation`, 57 flooding, 11 a blockage, 8 pollution and 3 other. So the label
  is broadly right but not exclusive, and it rests on the same inferred match as the caveat below.
- **The reference shown on their map is the case number, not the work order number.** Both
  are searchable here.
- **Report-to-fault links are inferred, not confirmed.** Where a report and a work order
  share a street and postcode within a week, the site links them. Measured at 1,029 of 3,161
  reports with a usable address; 416 match more than one work order, and all candidates are shown
  rather than one being picked.

  Matching is at **street level, not house number**. Thames Water's `Street` field is a single
  free-text address line — about 70% of records begin with a house number, formatted
  inconsistently (`5,MANDEVILLE CLOSE`, `TILEHURST ROAD 47A`) — so keying on it raw required an
  exact house-number and punctuation match and missed roughly half the real links (#28). A leak
  outside one house is routinely worked on under a neighbour's address.

  **A third of these matches would happen anyway.** The null model permutes which address belongs
  to which report while holding the dates fixed, so every report faces the same pool of work orders
  the real one did and only the address-to-time pairing is destroyed. It reproduces 631 of the 1,850
  matches — a signal of 2.9x, and an attributable excess of ~1,219. Run it with
  `python -m collector.checks --null-model`.

  The earlier date-shifting null claimed 99-100% genuine. It was measuring a different and much
  easier thing: work order raise density runs 112/day in May against 218/day in August, so shifting
  a report backwards puts it against a thinner pool, and shifting it forwards pushes it past the end
  of the data and returns zero.

  The match window is three days, chosen by that measurement rather than taste — widening it to a
  week added 114 real matches and 394 coincidental ones, so the attributable count actually fell.

  None of this gives a conversion
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

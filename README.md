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

A daily GitHub Action polls the feed, appends a change log entry, rebuilds the database and
republishes the site.

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

| Layer | Service | Rows |
|---|---|---|
| `CleanWaterOpenWorkOrder` | `CWOPWOPRD/FeatureServer` | ~9,500 |
| `WasteWaterOpenWorkOrder` | `WWOPWOPRD/FeatureServer` | ~10,700 |

Both are read unauthenticated, with `outSR=4326` for WGS84 coordinates. Layer *ids* inside
each service have changed before, so `collector/arcgis.py` resolves them by name at run time.

Each record carries a stable Salesforce work-order id, the raised date, a status along the
lifecycle `Reported → Investigation → Repair Planning → Repair Underway → Repair Complete`,
a priority flag, street/postcode/town, and a point geometry. Every one of the ~20,000 live
records has a non-null, unique `WorkOrderID`, which is what we key history on.

The map draws several other layers we deliberately ignore: planned improvement works,
supply-interruption bulletins, and a "pending pins" layer whose records carry no stable
identifier and so cannot be tracked over time.

## Data model

`faults` — one row per work order ever seen, holding Thames Water's own fields plus our
observations: `first_seen_at`, `last_seen_at`, `resolved_at`, `is_open`, `reappearances`.

`fault_events` — every observed change: `appeared`, `changed` (with field, old and new
value), `resolved`, `reappeared`. This is what drives the per-fault timeline.

`snapshots` — one row per collection run, with per-source counts.

See [`collector/schema.sql`](collector/schema.sql).

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

The workflow runs daily at 06:15 UTC. It runs the tests first, and refuses to write a delta
if the poll looks truncated (see below), so a bad day at the source cannot corrupt history.

## Caveats

These matter if you are going to quote the numbers at anyone.

- **"Cleared" means "stopped appearing"**, not "confirmed fixed". The feed does not say why
  a record left. Usually it is a completed repair, but a work order can also be cancelled,
  merged or reclassified.
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

## Licence and attribution

Not affiliated with or endorsed by Thames Water Utilities Ltd. The underlying records are
Thames Water's own published data, reproduced unchanged. Map tiles © OpenStreetMap
contributors.

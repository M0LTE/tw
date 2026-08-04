-- Thames Water fault tracker.
--
-- The database is *derived*: it is rebuilt by replaying the append-only delta
-- log in data/deltas/. That keeps git history small (we commit only what
-- changed each day) while making every figure on the site reproducible from
-- the raw record.

PRAGMA journal_mode = WAL;

-- One row per work order Thames Water has ever shown on the public map.
CREATE TABLE IF NOT EXISTS faults (
    id                      TEXT PRIMARY KEY,   -- WorkOrderID (Salesforce id)
    source                  TEXT NOT NULL,      -- 'clean' | 'waste'
    work_order_number       TEXT,
    case_number             TEXT,
    case_id                 TEXT,
    case_record_type        TEXT,
    journey_type            TEXT,
    high_level_journey_type TEXT,
    mid_level_work_type     TEXT,
    priority_flag           TEXT,
    status                  TEXT,
    street                  TEXT,
    thoroughfare            TEXT,
    postcode                TEXT,
    outcode                 TEXT,
    city                    TEXT,
    lon                     REAL,
    lat                     REAL,
    easting                 REAL,
    northing                REAL,

    -- Thames Water's own timestamps.
    raised_at               TEXT,
    closure_at              TEXT,
    repair_complete_at      TEXT,
    last_modified_at        TEXT,
    open_line_items         INTEGER,
    closed_line_items       INTEGER,
    remain_on_map_hrs       INTEGER,
    show_on_map             TEXT,

    -- Our observations.
    first_seen_at           TEXT NOT NULL,  -- first snapshot containing it
    last_seen_at            TEXT NOT NULL,  -- most recent snapshot containing it
    resolved_at             TEXT,           -- snapshot at which it vanished from the feed
    is_open                 INTEGER NOT NULL DEFAULT 1,
    reappearances           INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS faults_open        ON faults (is_open, raised_at);
CREATE INDEX IF NOT EXISTS faults_outcode     ON faults (outcode);
CREATE INDEX IF NOT EXISTS faults_city        ON faults (city);
CREATE INDEX IF NOT EXISTS faults_status      ON faults (status);
CREATE INDEX IF NOT EXISTS faults_resolved_at ON faults (resolved_at);

-- Every observed change, so a fault's progress can be replayed.
CREATE TABLE IF NOT EXISTS fault_events (
    id          INTEGER PRIMARY KEY,
    fault_id    TEXT NOT NULL REFERENCES faults (id),
    observed_at TEXT NOT NULL,   -- snapshot timestamp, not Thames Water's clock
    kind        TEXT NOT NULL,   -- appeared | changed | resolved | reappeared
    field       TEXT,
    old_value   TEXT,
    new_value   TEXT
);

CREATE INDEX IF NOT EXISTS fault_events_fault ON fault_events (fault_id, observed_at);
CREATE INDEX IF NOT EXISTS fault_events_kind  ON fault_events (kind, observed_at);
CREATE INDEX IF NOT EXISTS fault_events_field ON fault_events (field, observed_at);

-- Problems reported by the public that have not yet become work orders --
-- the map calls them "Leak". Thames Water keeps only a rolling seven days of
-- these, so a report that is never converted into a work order vanishes from
-- the public record entirely. Collecting them daily is the only way to see
-- how many reports actually turn into work.
--
-- Deliberately a separate table from `faults`: a report has no status, no work
-- order number and no repair lifecycle, and folding ~2,000 week-old reports
-- into the backlog would quietly wreck the headline age figures.
CREATE TABLE IF NOT EXISTS reports (
    id             TEXT PRIMARY KEY,   -- the layer's GlobalID
    source         TEXT NOT NULL,
    problem_type   INTEGER,
    street         TEXT,
    postcode       TEXT,
    outcode        TEXT,
    town           TEXT,
    lon            REAL,
    lat            REAL,

    reported_at    TEXT,               -- Thames Water's CreationDate
    edited_at      TEXT,

    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    disappeared_at TEXT,               -- snapshot at which it left the feed
    is_current     INTEGER NOT NULL DEFAULT 1,
    reappearances  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS reports_current  ON reports (is_current, reported_at);
CREATE INDEX IF NOT EXISTS reports_outcode  ON reports (outcode);
CREATE INDEX IF NOT EXISTS reports_postcode ON reports (postcode);

-- One row per collection run.
CREATE TABLE IF NOT EXISTS snapshots (
    observed_at   TEXT PRIMARY KEY,
    total         INTEGER NOT NULL,
    appeared      INTEGER NOT NULL,
    changed       INTEGER NOT NULL,
    resolved      INTEGER NOT NULL,
    reappeared    INTEGER NOT NULL,
    source_counts TEXT NOT NULL   -- JSON object
);

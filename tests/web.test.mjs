// Tests for the pure helpers in web/app.js (#39).
//
// The Python suite covers everything up to the JSON boundary; these cover the
// decisions taken on the far side of it. Several of them encode findings this
// site argues in public — that "Repair Complete" is not a verdict, that a
// status contradicting its own line-item count should say so, that the page
// must admit when it has stopped collecting. A regression in any of those would
// quietly restore the claim it was built to refute.
//
// Deliberately no framework and no jsdom: these are functions from data to
// strings, and `node --test` is in the standard library.
//
//   node --test tests/

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  freshnessState, statusCell, verdictCell, cohorts, expand, expandCleared,
  eventVisible, titleCase, escape, ageClass, formatAge,
} from '../web/app.js';

// ── Freshness (#38) ─────────────────────────────────────────────

const COLLECTED = '2026-08-19T14:00:00Z';
const at = (iso) => freshnessState(COLLECTED, new Date(iso).getTime()).level;

test('freshness stays quiet through ordinary lateness', () => {
  assert.equal(at('2026-08-19T14:30:00Z'), 'fresh');
  // Two missed polls is late, not broken. Crying wolf here would train a
  // reader to ignore the banner that matters.
  assert.equal(at('2026-08-19T16:59:00Z'), 'fresh');
});

test('freshness escalates at three hours and again at a day', () => {
  assert.equal(at('2026-08-19T17:00:00Z'), 'behind');
  assert.equal(at('2026-08-20T13:59:00Z'), 'behind');
  assert.equal(at('2026-08-20T14:00:00Z'), 'loud');
});

test('a reader clock behind the data does not accuse the collector', () => {
  // Skew, not a stalled collector: the page must not announce a failure
  // because someone's laptop is set wrong.
  const state = freshnessState(COLLECTED, new Date('2026-08-19T09:00:00Z').getTime());
  assert.equal(state.level, 'fresh');
  assert.equal(state.hours, 0, 'negative ages are floored, never reported');
});

test('freshness reports hours up to two days and days beyond', () => {
  assert.equal(freshnessState(COLLECTED, new Date('2026-08-20T06:00:00Z').getTime()).ago, '16 hours');
  assert.equal(freshnessState(COLLECTED, new Date('2026-08-21T15:00:00Z').getTime()).ago, '2 days');
});

test('freshness handles never having collected, and unparseable input', () => {
  assert.equal(freshnessState(null, Date.now()).level, 'never');
  assert.equal(freshnessState('not a date', Date.now()).level, 'never');
});

// ── Status and verdict (#32) ────────────────────────────────────

test('a finished-sounding status with open line items is marked', () => {
  const cell = statusCell({ status: 'Repair Complete', openItems: 4 });
  assert.match(cell, /Repair Complete/);
  assert.match(cell, /4 open/, 'the contradiction must be visible, not implied');
});

test('a status is left alone when nothing contradicts it', () => {
  assert.equal(statusCell({ status: 'Repair Complete', openItems: 0 }), 'Repair Complete');
  assert.equal(statusCell({ status: 'Completed', openItems: 0 }), 'Completed');
  // Work in progress with open items is ordinary, not a contradiction.
  assert.equal(statusCell({ status: 'Repair Underway', openItems: 3 }), 'Repair Underway');
  assert.equal(statusCell({ status: null, openItems: 2 }), '');
});

test('an older payload without line-item counts still renders', () => {
  // `ol` was added later; open.json files predating it have no column, and
  // expand() sets openItems to null rather than dropping the field.
  assert.equal(statusCell({ status: 'Repair Complete', openItems: null }), 'Repair Complete');
});

test('"Repair Complete" is not rendered as a verdict', () => {
  const cell = verdictCell({ verdict: 'Repair Complete' });
  assert.match(cell, /Not confirmed/,
    'rendering it as an outcome would restate the claim #32 refutes');
  assert.doesNotMatch(cell, /class="verdict done"/);
});

test('the two real verdicts are distinguished, and absence is admitted', () => {
  assert.match(verdictCell({ verdict: 'Completed' }), /verdict done/);
  assert.match(verdictCell({ verdict: 'Canceled' }), /verdict cancelled/);
  assert.match(verdictCell({ verdict: null }), /Not confirmed/);
});

// ── Observation-window cohorts (#34) ────────────────────────────

const COHORTS = [
  { month: '2026-01', n: 7053, pct: 28.1, short_n: 3080, short_pct: 17.9, gap: 10.2, long_pct: 5.3 },
  { month: '2026-07', n: 7102, pct: 20.7, short_n: 3456, short_pct: 17.3, gap: 3.4, long_pct: 1.6 },
];

test('the cohort table quotes the largest gap, not the last row', () => {
  const html = cohorts(COHORTS);
  assert.match(html, /10\.2 percentage points/);
  assert.match(html, /January 2026/, 'months are named, not printed as keys');
});

test('a single cohort renders nothing rather than a table of one', () => {
  // With one row there is no comparison to draw, and a lone row invites being
  // read as a trend.
  assert.equal(cohorts([COHORTS[0]]), '');
  assert.equal(cohorts([]), '');
  assert.equal(cohorts(null), '');
});

// ── Payload decoding ────────────────────────────────────────────

const OPEN = {
  epoch: '2020-01-01',
  today: 2422,
  dict: {
    status: ['Investigation'], journey: ['Blockage'], work_type: [null],
    priority: ['3'], city: ['READING'], source: ['clean'],
  },
  cols: {
    id: ['a'], wo: ['1'], cn: ['2'], s: [0], j: [0], w: [0], p: [0], c: [0], n: [0],
    pc: ['RG30 4JT'], st: ['MANDEVILLE CLOSE'], r: [2400], f: [2410],
    lon: [-1.02], lat: [51.44], ol: [3],
  },
  history: {},
  reports: {},
};

test('open faults decode with their line-item count and age', () => {
  const [row] = expand(OPEN);
  assert.equal(row.status, 'Investigation');
  assert.equal(row.openItems, 3);
  assert.equal(row.age, 22, 'age is today minus raised, in whole days');
  assert.match(row.haystack, /mandeville/, 'search text is lowercased');
});

test('a payload predating the line-item column decodes without it', () => {
  const legacy = { ...OPEN, cols: { ...OPEN.cols } };
  delete legacy.cols.ol;
  const [row] = expand(legacy);
  assert.equal(row.openItems, null, 'missing column must not become undefined-indexed');
  assert.equal(row.status, 'Investigation', 'and everything else still decodes');
});

test('cleared faults carry the closed-feed verdict', () => {
  const cleared = {
    epoch: '2020-01-01', today: 2422, latest: 1755612345, window_days: 90,
    dict: { ...OPEN.dict, verdict: ['Completed'] },
    cols: {
      ...OPEN.cols, t: [1755600000], v: [0],
    },
  };
  delete cleared.cols.f;
  const [row] = expandCleared(cleared);
  assert.equal(row.verdict, 'Completed');
  assert.equal(row.openItems, 3);
  assert.equal(row.kind, 'cleared');
});

// ── Annotations that must not outlive their event (#25) ─────────

test('an annotation is hidden once its event leaves the window', () => {
  const newest = 1_800_000_000;
  const day = 86400;
  const recent = { observed_at: new Date((newest - 2 * day) * 1000).toISOString() };
  const old = { observed_at: new Date((newest - 40 * day) * 1000).toISOString() };
  assert.equal(eventVisible(recent, newest, 7), true);
  assert.equal(eventVisible(old, newest, 7), false,
    'this went wrong three times before the check existed');
  assert.equal(eventVisible(old, newest, null), true, 'no window means all time');
  assert.equal(eventVisible(null, newest, 7), false);
});

// ── Text handling ───────────────────────────────────────────────

test('addresses are title-cased and comma-packed source text is spaced', () => {
  assert.equal(titleCase('5,MANDEVILLE CLOSE'), '5, Mandeville Close');
});

test('escape neutralises markup from the source feed', () => {
  assert.match(escape('<script>x</script>'), /&lt;script&gt;/);
  assert.equal(escape(null), '');
});

test('age classes bucket by the published thresholds', () => {
  assert.equal(ageClass(0), 0);
  assert.equal(ageClass(6), 0);
  assert.equal(ageClass(7), 1, 'thresholds are exclusive at the lower bound');
  assert.equal(ageClass(400), 4);
});

test('an unknown age is styled as the worst bucket, not the best', () => {
  // A fault Thames Water published with no raised date should not be coloured
  // as though it arrived this morning. Erring towards alarming is the right
  // direction for an absence: the alternative flatters a missing field.
  assert.equal(ageClass(null), 4);
  assert.equal(ageClass(undefined), 4);
  assert.equal(formatAge(null), '—', 'and it is not given a number it does not have');
});

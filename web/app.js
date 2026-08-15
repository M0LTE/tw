// Thames Water fault tracker — UI.
//
// Reads the compact JSON produced by collector/build_site.py. `open.json` is
// dictionary-encoded columnar data (~20k faults), which we expand once into
// plain objects; everything after that is ordinary array work.

import { areaChart, barChart, flowChart, formatNumber } from './charts.js';

const AGE_CLASSES = [7, 30, 90, 365];
// Statuses that sound finished. Thames Water applies "Repair Complete" to
// records that mostly still carry outstanding line items, and the pin then ages
// off the map over 72 hours — so somebody looking up their own street sees
// "Repair Complete" over an open hole. Where the record contradicts itself, say
// so next to the status rather than passing it on unqualified. See #32.
const INCONCLUSIVE_VERDICT = 'Repair Complete';
const FINISHED_SOUNDING = new Set(['Repair Complete', 'Completed']);
// Rows per page. The page itself scrolls — no inner scroll container — so this
// is the only thing deciding how much you can scroll through at once. Measured
// build+layout cost: 100 rows 31ms, 250 86ms, 500 185ms, 1000 406ms. 250 keeps
// a page change imperceptible while giving a decent run of scrolling.
const PAGE_SIZE = 250;

const state = {
  summary: null,
  faults: [],
  cleared: [],
  clearedWindow: null,
  latest: null,
  history: {},
  epoch: null,
  today: 0,
  view: 'overview',
  mode: 'open',
  // Overview chart window, in days. null = everything.
  range: 7,
  filters: { search: '', source: '', status: '', journey: '', minAge: '', clearedWithin: '', verdict: '' },
  sort: { key: 'age', dir: -1 },
  page: 0,
  filtered: [],
  map: null,
  mapLayer: null,
};

// The Faults view browses one of two record sets through the same filters,
// sorting and paging. They are separate arrays because a fault's row index is
// the key into `history` and `faultReports`, which only exist for open faults.
const rowsForMode = () => (state.mode === 'cleared' ? state.cleared : state.faults);

// An annotation about a past event is only true while that event is inside the
// window the figure covers. This has now gone wrong three times — the banner
// tense (#25), the backlog chart's step note, and the backlog KPI reading "+275
// over seven days" beneath "mostly the 9,620 work orders that stopped being
// published". Each time the caveat outlived the thing it described and said
// something false. One helper, used everywhere, so there is no fourth.
function eventVisible(info, newestSeconds, windowDays) {
  if (!info || !info.observed_at) return false;
  if (!windowDays) return true;   // no window means everything is in view
  return Date.parse(info.observed_at) / 1000 > newestSeconds - windowDays * 86400;
}

const $ = (sel) => document.querySelector(sel);

// ── Helpers ─────────────────────────────────────────────────────

const dayToDate = (day) => new Date(Date.parse(state.epoch + 'T00:00:00Z') + day * 86400000);

function formatDate(day) {
  if (day === null || day === undefined) return '—';
  return dayToDate(day).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
}

function ageClass(days) {
  if (days === null || days === undefined) return 4;
  for (let i = 0; i < AGE_CLASSES.length; i++) if (days < AGE_CLASSES[i]) return i;
  return 4;
}

function formatAge(days) {
  if (days === null || days === undefined) return '—';
  if (days < 1) return 'today';
  if (days < 21) return `${days}d`;
  if (days < 365) return `${Math.round(days / 7)}w`;
  const years = days / 365;
  return `${years.toFixed(years < 10 ? 1 : 0)}y`;
}

function titleCase(text) {
  if (!text) return '';
  // Thames Water's address lines are inconsistently punctuated — "5,MANDEVILLE
  // CLOSE" is theirs verbatim — so give a comma its space back before casing.
  return text.replace(/,(?=\S)/g, ', ')
    .replace(/\b[A-Z]{2,}\b/g, (word) => word[0] + word.slice(1).toLowerCase());
}

function place(fault) {
  return [titleCase(fault.street), titleCase(fault.city)].filter(Boolean).join(', ');
}

// A small number of records carry no address at all — Thames Water publishes
// Street, Town and Postcode as null, which happens when someone drops a pin on
// the map instead of typing an address. Every one of them still has
// coordinates, so show the position rather than an empty cell.
function hasAddress(rec, town) {
  return Boolean(rec.street || town || rec.postcode);
}

function locationCell(rec, town) {
  if (hasAddress(rec, town)) {
    const text = [titleCase(rec.street), titleCase(town)].filter(Boolean).join(', ');
    return `${escape(text)} <span class="mono">${escape(rec.postcode || '')}</span>`;
  }
  if (rec.lat !== null && rec.lat !== undefined) {
    return `<span class="pinned">Pinned on map</span> ` +
           `<span class="mono">${rec.lat.toFixed(4)}, ${rec.lon.toFixed(4)}</span>`;
  }
  return '<span class="pinned">No location recorded</span>';
}

// Reports and work orders share no reference number, so a link is an inference
// from address and timing. Say what was observed, never that one became the other.
// Phrase from the calendar-day difference, not elapsed hours: a report at 16:33
// and a work order at 12:21 the following morning is under 24 hours apart but
// is plainly "the next day", and saying "the same day" reads as wrong.
function lagPhrase(fromDay, toDay) {
  if (fromDay === null || toDay === null || fromDay === undefined || toDay === undefined) return '';
  const days = toDay - fromDay;
  if (days === 0) return 'the same day';
  if (days === 1) return 'the next day';
  if (days > 1) return `${days} days later`;
  return days === -1 ? 'the day before' : `${Math.abs(days)} days earlier`;
}

function renderReportLinks(matches, reportedDay) {
  if (!matches || !matches.length) return '';
  const items = matches.map((m) => `
    <li>
      ${m.open
        ? `<a href="#" data-fault="${escape(m.id)}">Work order ${escape(m.wo)}</a>`
        : `<span class="mono">Work order ${escape(m.wo)}</span> <span class="pinned">(closed)</span>`}
      — ${escape(m.journey || '')}${m.status ? `, ${escape(m.status)}` : ''}
      <div class="when">raised ${escape(formatDate(m.raised))}, ${escape(lagPhrase(reportedDay, m.raised))}</div>
    </li>`).join('');
  return `
    <h2 style="font-size:14px;margin:18px 0 10px">Work on this street</h2>
    <ul class="linklist">${items}</ul>
    <p class="footnote" style="margin-top:8px">
      ${matches.length > 1
        ? `<strong>${matches.length} work orders</strong> were raised on this street in the window, so which (if any) followed from this report is ambiguous. All are shown.`
        : 'Matched on street and postcode within three days of the report — the same street, not necessarily the same building.'}
      The two feeds share no reference number, so this is an association rather than a confirmed link.
      Busy streets get work anyway: shuffling which address belongs to which report still produces
      about a third of these matches, so treat any single link as suggestive rather than established.
    </p>`;
}

function renderFaultLinks(matches, raisedDay) {
  if (!matches || !matches.length) return '';
  const items = matches.map((m) => `
    <li><a href="#" data-report="${escape(m.id)}">Public report</a>
      <div class="when">reported ${escape(formatDate(m.reported))} — ${escape(lagPhrase(m.reported, raisedDay))} this work order was raised</div>
    </li>`).join('');
  return `
    <h2 style="font-size:14px;margin:18px 0 10px">Reported by the public</h2>
    <ul class="linklist">${items}</ul>
    <p class="footnote" style="margin-top:8px">
      Matched on street and postcode — the same street, not necessarily the same building.
      The feeds share no reference number, so this is an association rather than a confirmed link,
      and about a third of matches like this one would arise by chance on a street that had work
      anyway.</p>`;
}

const NO_ADDRESS_NOTE =
  'Thames Water published this one with no street, town or postcode, which happens when a ' +
  'problem is pinned on the map rather than given an address. The coordinates are theirs.';

// ── Data loading ────────────────────────────────────────────────

async function loadJSON(path) {
  const res = await fetch(path, { cache: 'no-cache' });
  if (!res.ok) throw new Error(`${path}: ${res.status} ${res.statusText}`);
  return res.json();
}

function expand(open) {
  const { dict, cols } = open;
  const out = new Array(cols.id.length);
  for (let i = 0; i < cols.id.length; i++) {
    const raised = cols.r[i];
    out[i] = {
      i,
      id: cols.id[i],
      kind: 'open',
      workOrder: cols.wo[i],
      caseNumber: cols.cn ? cols.cn[i] : null,
      status: dict.status[cols.s[i]],
      journey: dict.journey[cols.j[i]],
      workType: dict.work_type[cols.w[i]],
      priority: dict.priority[cols.p[i]],
      city: dict.city[cols.c[i]],
      source: dict.source[cols.n[i]],
      postcode: cols.pc[i],
      street: cols.st[i],
      raised,
      firstSeen: cols.f[i],
      age: raised === null ? null : open.today - raised,
      lon: cols.lon[i],
      lat: cols.lat[i],
      openItems: cols.ol ? cols.ol[i] : null,
    };
    // Case number included because that is the reference Thames Water's own
    // map shows, so it is what someone will paste in here.
    out[i].haystack = [out[i].street, out[i].postcode, out[i].city, out[i].workOrder,
                       out[i].caseNumber, out[i].journey]
      .filter(Boolean).join(' ').toLowerCase();
  }
  // Statuses come from the dictionary in first-seen order; sort by the real
  // lifecycle so history timelines read in the right sequence.
  state.statusDict = dict.status;
  state.faultReports = open.reports || {};
  return out;
}

// Faults that have left the open feed. Same columns as `expand`, plus the day
// they went and Thames Water's own verdict where the closed feed carries one.
// `age` is deliberately time-to-clear rather than age-now, so sorting by it
// answers "what sat there longest before it went".
// Days since the payload's epoch, from an absolute moment. Used to keep the
// "Took" figure in whole days while `cleared` itself stays precise.
function dayOf(epochSeconds) {
  if (epochSeconds === null || epochSeconds === undefined) return null;
  const epochDay = Math.floor(Date.parse(state.epoch + 'T00:00:00Z') / 86400000);
  return Math.floor(epochSeconds / 86400) - epochDay;
}

function expandCleared(data) {
  const { dict, cols } = data;
  return cols.id.map((id, i) => {
    const raised = cols.r[i];
    const clearedAt = cols.t[i];
    const cleared = dayOf(clearedAt);
    const row = {
      i,
      id,
      kind: 'cleared',
      workOrder: cols.wo[i],
      caseNumber: cols.cn[i],
      status: dict.status[cols.s[i]],
      journey: dict.journey[cols.j[i]],
      workType: dict.work_type[cols.w[i]],
      priority: dict.priority[cols.p[i]],
      city: dict.city[cols.c[i]],
      source: dict.source[cols.n[i]],
      postcode: cols.pc[i],
      street: cols.st[i],
      raised,
      cleared,
      clearedAt,
      verdict: dict.verdict[cols.v[i]] || null,
      age: raised === null || cleared === null ? null : cleared - raised,
      lon: cols.lon[i],
      lat: cols.lat[i],
      openItems: cols.ol ? cols.ol[i] : null,
    };
    row.haystack = [row.street, row.postcode, row.city, row.workOrder, row.caseNumber, row.journey]
      .filter(Boolean).join(' ').toLowerCase();
    return row;
  });
}

// ── Overview ────────────────────────────────────────────────────

function sourceColour(key) {
  return key === 'clean' ? 'var(--c-clean)' : 'var(--c-waste)';
}

function renderKPIs() {
  const s = state.summary;
  const buckets = s.age.buckets || {};
  const overYear = (buckets['<=730'] || 0) + (buckets['>730'] || 0);

  // Summed over a real time window. This was `flow.slice(-7)`, which meant seven
  // days while rows were daily and seven snapshots once they were per-collection.
  const WINDOW_DAYS = 7;
  const newest = s.flow.length ? s.flow[s.flow.length - 1].t : 0;
  const window = s.flow.filter((f) => f.t > newest - WINDOW_DAYS * 86400);
  const raised = window.reduce((a, f) => a + f.raised, 0);
  const cleared = window.reduce((a, f) => a + f.resolved, 0);
  const net = raised - cleared;
  const haveFlow = window.length >= 1;
  const spanHours = s.backlog.length ? Math.max(1, Math.round((newest - s.backlog[0].t) / 3600)) : 0;
  const shortSpan = spanHours < 24 * WINDOW_DAYS;
  const eventInWindow = eventVisible(s.truncated, newest, WINDOW_DAYS);

  const cards = [
    {
      label: 'Open faults now',
      value: formatNumber(s.totals.open),
      note: Object.entries(s.by_source)
        .map(([k, v]) => `${formatNumber(v)} ${(s.sources.find((x) => x.key === k) || {}).label || k}`)
        .join(' · '),
    },
    {
      label: 'Median age',
      value: s.age.p50 === null ? '–' : `${formatNumber(s.age.p50)}d`,
      note: `Half have been open longer. Worst 10%: over ${formatNumber(s.age.p90)} days.`,
    },
    {
      label: 'Open over a year',
      value: formatNumber(overYear),
      tone: overYear > 0 ? 'bad' : 'good',
      note: s.totals.open ? `${((overYear / s.totals.open) * 100).toFixed(1)}% of the backlog` : '',
    },
    {
      label: 'Oldest open fault',
      value: s.age.max === null ? '–' : formatAge(s.age.max),
      note: s.age.max === null ? '' : `raised ${formatNumber(s.age.max)} days ago`,
    },
    haveFlow
      ? {
          label: shortSpan ? 'Backlog, since tracking began' : 'Backlog, last 7 days',
          value: `${net > 0 ? '+' : ''}${formatNumber(net)}`,
          // A falling backlog is normally good news, and is coloured that way.
          // It is not good news when the fall is a source-side event: rendering
          // a 12,613 collapse as a green improvement would be the site telling
          // the exact lie it exists to catch. Stay neutral and say why.
          tone: eventInWindow ? '' : (net > 0 ? 'bad' : 'good'),
          note: eventInWindow
            ? `mostly the ${formatNumber(s.truncated.departed)} work orders that stopped being published — not work completed`
            : `${formatNumber(raised)} arrived, ${formatNumber(cleared)} cleared` +
              (shortSpan ? ` over ${formatNumber(spanHours)} hours` : ''),
        }
      : {
          label: 'Backlog trend',
          value: '-',
          note: 'Needs a second collection',
        },
    s.reports && s.reports.current
      ? {
          label: 'Reported, not yet work',
          value: formatNumber(s.reports.current),
          note: s.reports.retention_days
            ? `public reports on the map; kept ~${s.reports.retention_days} days`
            : 'public reports awaiting a work order',
        }
      : null,
    s.resolution.n
      ? {
          label: 'Typical time to clear',
          value: `${formatNumber(s.resolution.since_raised.p50)}d`,
          note: `median of ${formatNumber(s.resolution.n)} faults cleared recently`
            + (s.resolution.quarantined
              ? `; excludes ${formatNumber(s.resolution.quarantined)} uncorroborated departures`
              : ''),
        }
      : {
          label: 'Typical time to clear',
          value: '–',
          note: 'No faults have cleared since tracking began',
        },
  ];

  $('#kpis').replaceChildren(
    ...cards.filter(Boolean).map((c) => {
      const div = document.createElement('div');
      div.className = 'kpi';
      div.innerHTML = `<div class="kpi-label"></div><div class="kpi-value ${c.tone || ''}"></div><div class="kpi-note"></div>`;
      div.querySelector('.kpi-label').textContent = c.label;
      div.querySelector('.kpi-value').textContent = c.value;
      div.querySelector('.kpi-note').textContent = c.note || '';
      return div;
    }),
  );
}

// A 63% cliff cannot share a vertical axis with ordinary movement: since
// 5 August the backlog has lived in a ~4% band that the drop renders invisible.
// Windowing lets the charts answer "what is happening now" while the whole
// history stays a click away, where the cliff is the story rather than noise.
function inRange(rows) {
  if (!state.range || !rows.length) return rows;
  const newest = rows[rows.length - 1].t;
  return rows.filter((r) => r.t > newest - state.range * 86400);
}

function renderOverview() {
  const s = state.summary;
  renderKPIs();

  const backlog = inRange(s.backlog);
  areaChart($('#chart-backlog'), [{
    key: 'total',
    label: 'Open faults',
    colour: 'var(--accent)',
    values: backlog.map((row) => ({ x: row.t, y: row.total })),
  }], { stacked: false, zeroBased: false });

  const latest = backlog[backlog.length - 1] || {};
  const first = backlog[0] || {};
  const net = (latest.total || 0) - (first.total || 0);
  $('#legend-backlog').innerHTML =
    `<span>Now <strong>${formatNumber(latest.total)}</strong> open — ` +
    // Only the open work-order networks contribute to the backlog; the closed
    // and report feeds are in `sources` too and would show as zeros.
    s.sources.filter((src) => src.key in latest)
      .map((src) => `${formatNumber(latest[src.key])} ${escape(src.label.toLowerCase())}`).join(', ') +
    `</span><span>${state.range ? `Over ${state.range} days` : 'Since tracking began'} ` +
    `<strong>${net > 0 ? '+' : ''}${formatNumber(net)}</strong></span>` +
    `<span class="pinned">Vertical axis does not start at zero</span>`;

  flowChart($('#chart-flow'), inRange(s.flow));

  // Buckets are exclusive ranges, so label them as ranges rather than "≤ n".
  const bucketLabels = [
    ['<=1', 'today', 0], ['<=7', '1–7d', 0], ['<=30', '1w–1m', 1], ['<=90', '1–3m', 2],
    ['<=180', '3–6m', 2], ['<=365', '6m–1y', 3], ['<=730', '1–2y', 4], ['>730', '2y+', 4],
  ];
  barChart(
    $('#chart-age'),
    bucketLabels.map(([key, label, tone]) => ({
      label,
      value: s.age.buckets[key] || 0,
      colour: `var(--age-${tone})`,
    })),
  );

  barChart(
    $('#chart-status'),
    s.status_order.filter((x) => s.by_status[x]).map((x) => ({ label: x, value: s.by_status[x] })),
    { horizontal: true },
  );

  barChart(
    $('#chart-journey'),
    Object.entries(s.by_journey).slice(0, 9).map(([label, value]) => ({ label, value })),
    { horizontal: true },
  );
}

// `state.faults` arrives sorted oldest-first with unknown dates last, so the
// head of the list is exactly the longest-running faults.
function renderOldest(limit = 25) {
  const table = $('#table-oldest');
  table.innerHTML =
    '<thead><tr><th>Raised</th><th>Age</th><th>Problem</th><th>Where</th><th>Status</th><th>Network</th></tr></thead>';
  const body = document.createElement('tbody');

  for (const fault of state.faults.filter((f) => f.raised !== null).slice(0, limit)) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${formatDate(fault.raised)}</td>
      <td class="age age-${ageClass(fault.age)}">${formatAge(fault.age)}</td>
      <td>${escape(fault.journey || '')}</td>
      <td class="wrap">${locationCell(fault, fault.city)}</td>
      <td>${escape(fault.status || '')}</td>
      <td><span class="pill ${fault.source}">${fault.source === 'clean' ? 'Clean' : 'Waste'}</span></td>`;
    tr.addEventListener('click', () => openDetail(fault));
    body.append(tr);
  }
  table.append(body);
}

function escape(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ── Faults table ────────────────────────────────────────────────

// Only the filters that mean something in the current mode are shown: an age
// filter over cleared faults would read as "age now", which they no longer have.
function syncModeUI() {
  const cleared = state.mode === 'cleared';
  $('#f-cleared').hidden = !cleared;
  $('#f-verdict').hidden = !cleared;
  $('#f-age').hidden = cleared;
  $('#f-mode').value = state.mode;

  const note = $('#f-cleared-note');
  note.hidden = !cleared;
  if (cleared) {
    note.innerHTML = 'A fault is <strong>cleared</strong> when it stops appearing in the open feed. '
      + 'Where Thames Water\u2019s closed feed also carries it, their own Completed or Cancelled verdict is shown; '
      + 'where it does not, <strong>nothing confirms what happened</strong> \u2014 a bulk disappearance is not evidence of bulk repair. '
      + `Covers the last ${state.clearedWindow || 90} days of departures. `
      + 'Times are the collection that first found the fault missing, so a departure is placed '
      + 'within about an hour \u2014 not to the minute it actually went.';
  }
}

function applyFilters() {
  const f = state.filters;
  const needle = f.search.trim().toLowerCase();
  const minAge = f.minAge ? Number(f.minAge) : null;

  // "Cleared within N hours" counts back from the latest collection, not from
  // the browser's clock: the site is only ever as current as its last poll, and
  // counting from local time would silently drop the newest departures for
  // anyone loading the page between runs.
  const withinHours = f.clearedWithin === '' ? null : Number(f.clearedWithin);
  const floor = withinHours === null || state.latest === null
    ? null : state.latest - withinHours * 3600;

  state.filtered = rowsForMode().filter((x) => {
    if (f.source && x.source !== f.source) return false;
    if (f.status && x.status !== f.status) return false;
    if (f.journey && x.journey !== f.journey) return false;
    if (minAge !== null && !(x.age > minAge)) return false;
    if (needle && !x.haystack.includes(needle)) return false;
    if (state.mode === 'cleared') {
      if (floor !== null && !(x.clearedAt !== null && x.clearedAt >= floor)) return false;
      if (f.verdict === '_none' && x.verdict) return false;
      if (f.verdict && f.verdict !== '_none' && x.verdict !== f.verdict) return false;
    }
    return true;
  });

  const { key, dir } = state.sort;
  state.filtered.sort((a, b) => {
    const av = a[key];
    const bv = b[key];
    if (av === bv) return 0;
    if (av === null || av === undefined) return 1;
    if (bv === null || bv === undefined) return -1;
    return (av > bv ? 1 : -1) * dir;
  });

  state.page = 0;
  renderFaults();
}

// Date on top, time beneath. `resolved_at` is the moment of the first
// collection the fault was *missing* from, not the moment it left — hourly
// polling makes that good to about an hour, not exact, so the time is shown
// quietly rather than as a headline.
function clearedCell(x) {
  if (x.clearedAt === null || x.clearedAt === undefined) return '—';
  const at = new Date(x.clearedAt * 1000);
  return `${escape(formatDate(x.cleared))}`
    + `<div class="when">${escape(at.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }))}</div>`;
}

// Thames Water's own verdict, or the honest absence of one. A cleared fault
// with no closed-feed record has not been confirmed as anything — saying so is
// the point of the column.
function verdictCell(x) {
  if (x.verdict === 'Completed') return '<span class="verdict done">Completed</span>';
  if (x.verdict === 'Canceled') return '<span class="verdict cancelled">Cancelled</span>';
  // "Repair Complete" reads like an outcome and is not one — four in five
  // records carrying it still have outstanding line items. Rendering it as a
  // neutral verdict would repeat the source's own overstatement. See #32.
  if (x.verdict === INCONCLUSIVE_VERDICT) {
    return '<span class="verdict unknown" title="Thames Water lists this as &quot;Repair Complete&quot;, '
      + 'a status that usually still carries outstanding line items. It does not confirm a repair.">'
      + 'Not confirmed</span>';
  }
  if (x.verdict) return `<span class="verdict">${escape(x.verdict)}</span>`;
  return '<span class="verdict unknown" title="This fault stopped being published, but Thames Water\'s closed feed carries no record of it">Not confirmed</span>';
}

// A status plus, where it disagrees with the record, the disagreement. The
// marker is deliberately not styled as an error: outstanding line items are
// Thames Water's own count, and what they mean is undocumented. It is shown so
// a reader can see the contradiction, not told what to conclude from it.
function statusCell(x) {
  const label = escape(x.status || '');
  if (!FINISHED_SOUNDING.has(x.status) || !x.openItems) return label;
  const n = x.openItems;
  return `${label} <span class="pinned" title="Thames Water's own record still shows `
    + `${n} open line item${n === 1 ? '' : 's'} on this work order, despite the status.">`
    + `&#9888; ${n} open</span>`;
}

const BASE_COLUMNS = [
  { key: 'journey', label: 'Problem', render: (x) => escape(x.journey || '') },
  { key: 'street', label: 'Where', cls: 'wrap', render: (x) => locationCell(x, x.city) },
  { key: 'status', label: 'Status', render: (x) => statusCell(x) },
  { key: 'source', label: 'Network', render: (x) => `<span class="pill ${x.source}">${x.source === 'clean' ? 'Clean' : 'Waste'}</span>` },
  { key: 'workOrder', label: 'Work order', cls: 'mono', render: (x) => escape(x.workOrder || '') },
  { key: 'caseNumber', label: 'Case', cls: 'mono', render: (x) => escape(x.caseNumber || '') },
];

const OPEN_COLUMNS = [
  { key: 'raised', label: 'Raised', render: (x) => formatDate(x.raised) },
  { key: 'age', label: 'Age', cls: 'num', render: (x) => `<span class="age age-${ageClass(x.age)}">${formatAge(x.age)}</span>` },
  ...BASE_COLUMNS,
];

const CLEARED_COLUMNS = [
  { key: 'clearedAt', label: 'Cleared', render: (x) => clearedCell(x) },
  { key: 'verdict', label: 'Outcome', render: verdictCell },
  { key: 'raised', label: 'Raised', render: (x) => formatDate(x.raised) },
  { key: 'age', label: 'Took', cls: 'num', render: (x) => `<span class="age age-${ageClass(x.age)}">${formatAge(x.age)}</span>` },
  ...BASE_COLUMNS,
];

const columns = () => (state.mode === 'cleared' ? CLEARED_COLUMNS : OPEN_COLUMNS);

function renderFaults() {
  const table = $('#table-faults');
  const head = document.createElement('thead');
  const hr = document.createElement('tr');
  for (const col of columns()) {
    const th = document.createElement('th');
    th.dataset.sort = col.key;
    th.textContent = col.label;
    if (state.sort.key === col.key) {
      const arrow = document.createElement('span');
      arrow.className = 'arrow';
      arrow.textContent = state.sort.dir === 1 ? ' ▲' : ' ▼';
      th.append(arrow);
    }
    th.addEventListener('click', () => {
      if (state.sort.key === col.key) state.sort.dir *= -1;
      else state.sort = { key: col.key, dir: col.key === 'age' ? -1 : 1 };
      applyFilters();
    });
    hr.append(th);
  }
  head.append(hr);

  const body = document.createElement('tbody');
  const start = state.page * PAGE_SIZE;
  for (const fault of state.filtered.slice(start, start + PAGE_SIZE)) {
    const tr = document.createElement('tr');
    tr.innerHTML = columns().map((c) => `<td class="${c.cls || ''}">${c.render(fault)}</td>`).join('');
    tr.addEventListener('click', () => openDetail(fault));
    body.append(tr);
  }

  table.replaceChildren(head, body);
  const total = rowsForMode().length;
  $('#f-count').textContent = state.mode === 'cleared'
    ? `${formatNumber(state.filtered.length)} of ${formatNumber(total)} cleared`
    : `${formatNumber(state.filtered.length)} of ${formatNumber(total)} faults`;

  const pages = Math.ceil(state.filtered.length / PAGE_SIZE) || 1;
  const pager = $('#pager');
  pager.replaceChildren();
  if (pages > 1) {
    const prev = document.createElement('button');
    prev.textContent = '‹ Previous';
    prev.disabled = state.page === 0;
    prev.addEventListener('click', () => { state.page--; renderFaults(); window.scrollTo({ top: 0 }); });

    const label = document.createElement('span');
    label.textContent = `Page ${state.page + 1} of ${formatNumber(pages)}`;

    const next = document.createElement('button');
    next.textContent = 'Next ›';
    next.disabled = state.page >= pages - 1;
    next.addEventListener('click', () => { state.page++; renderFaults(); window.scrollTo({ top: 0 }); });

    pager.append(prev, label, next);
  }
}

function downloadCSV(rows, filename) {
  const csv = rows
    .map((r) => r.map((v) => {
      const s = String(v ?? '');
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    }).join(','))
    .join('\n');

  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

const isoDay = (day) => (day === null || day === undefined ? '' : dayToDate(day).toISOString().slice(0, 10));

function exportCSV() {
  const cleared = state.mode === 'cleared';
  // `outcome` is empty rather than a guess when the closed feed has no record:
  // a blank is honest, "unknown" in a data column invites being read as a value.
  const header = cleared
    ? ['work_order', 'case_number', 'raised', 'cleared_utc', 'days_to_clear', 'outcome',
       'last_status', 'problem', 'work_type', 'street', 'postcode', 'town', 'network', 'lat', 'lon']
    : ['work_order', 'case_number', 'raised', 'age_days', 'status', 'problem', 'work_type',
       'street', 'postcode', 'town', 'network', 'lat', 'lon'];

  const rows = state.filtered.map((x) => (cleared
    ? [x.workOrder, x.caseNumber, isoDay(x.raised),
       x.clearedAt ? new Date(x.clearedAt * 1000).toISOString().replace('.000', '') : '', x.age ?? '', x.verdict || '',
       x.status, x.journey, x.workType, x.street, x.postcode, x.city, x.source, x.lat, x.lon]
    : [x.workOrder, x.caseNumber, isoDay(x.raised), x.age ?? '',
       x.status, x.journey, x.workType, x.street, x.postcode, x.city, x.source, x.lat, x.lon]));

  const name = cleared ? 'thames-water-cleared' : 'thames-water-faults';
  downloadCSV([header, ...rows], `${name}-${new Date().toISOString().slice(0, 10)}.csv`);
}

// ── Detail dialog ───────────────────────────────────────────────

function openDetail(fault) {
  // `history` and `faultReports` are keyed by row index into the open-fault
  // array, so they must not be read for a cleared fault — the indexes collide.
  const isCleared = fault.kind === 'cleared';
  const history = isCleared ? [] : state.history[fault.i] || [];
  const body = $('#detail-body');

  const rows = [
    ['Work order', fault.workOrder],
    ['Case number', fault.caseNumber],
    ['Network', fault.source === 'clean' ? 'Clean water' : 'Waste water'],
    ['Problem', fault.journey],
    ['Work type', fault.workType],
    ['Priority', fault.priority && fault.priority !== 'N/A' ? fault.priority : null],
    ['Raised', formatDate(fault.raised)],
    ...(isCleared
      ? [
        ['Left the map', fault.clearedAt === null ? formatDate(fault.cleared)
          : `${formatDate(fault.cleared)}, ${new Date(fault.clearedAt * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}`],
        ['Took', fault.age === null ? null : `${formatNumber(fault.age)} days`],
        ['Outcome', fault.verdict === 'Canceled' ? 'Cancelled (Thames Water’s own record)'
          : fault.verdict ? `${fault.verdict} (Thames Water’s own record)`
            : 'Not confirmed — it stopped being published, and their closed feed has no record of it'],
        ['Last published status', fault.status],
      ]
      : [
        ['Open for', fault.age === null ? null : `${formatNumber(fault.age)} days`],
        ['Current status', fault.status],
      ]),
    ['Location', place(fault)],
    ['Postcode', fault.postcode],
    ['Coordinates', hasAddress(fault, fault.city) || fault.lat === null || fault.lat === undefined
      ? null : `${fault.lat.toFixed(5)}, ${fault.lon.toFixed(5)}`],
  ].filter(([, v]) => v);

  const timeline = history.length
    ? history.map(([day, statusIndex]) => `
        <li><strong>${escape(state.statusDict[statusIndex] || 'Seen on map')}</strong>
        <div class="when">observed ${formatDate(day)}</div></li>`).join('')
    : isCleared
      ? `<li><strong>${escape(fault.status || 'Last seen')}</strong>
         <div class="when">last published status, ${escape(formatDate(fault.cleared))}</div></li>
         <li class="pending"><strong>Gone from the map</strong>
         <div class="when">first collection it was missing from: ${escape(fault.clearedAt === null ? formatDate(fault.cleared)
           : formatDate(fault.cleared) + ', ' + new Date(fault.clearedAt * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }))}
           — it left at some point in the hour before that</div></li>`
      : `<li class="pending"><strong>${escape(fault.status || 'Open')}</strong>
         <div class="when">status history begins once this fault changes</div></li>`;

  body.innerHTML = `
    <h3>${escape(fault.journey || 'Fault')}${fault.street ? ' — ' + escape(titleCase(fault.street)) : ''}</h3>
    <p class="sub">${hasAddress(fault, fault.city)
      ? escape(place(fault)) + ' ' + escape(fault.postcode || '')
      : 'No address published'}</p>
    <dl class="kv">${rows.map(([k, v]) => `<dt>${escape(k)}</dt><dd>${escape(v)}</dd>`).join('')}</dl>
    ${hasAddress(fault, fault.city) ? '' : `<p class="footnote" style="margin-top:0">${NO_ADDRESS_NOTE}</p>`}
    <h2 style="font-size:14px;margin:0 0 10px">What we have seen</h2>
    <ul class="timeline">${timeline}</ul>
    ${isCleared ? '' : renderFaultLinks((state.faultReports || {})[fault.i], fault.raised)}
    ${fault.lat ? `<p style="margin-top:16px"><a href="https://www.openstreetmap.org/?mlat=${fault.lat}&mlon=${fault.lon}#map=17/${fault.lat}/${fault.lon}" target="_blank" rel="noopener">View location on OpenStreetMap ↗</a></p>` : ''}
  `;
  $('#detail').showModal();
}

// ── Public reports ──────────────────────────────────────────────
//
// Problems the public has reported that have not become work orders. Thames
// Water keeps only a rolling window of these, so a report can vanish from
// their map without ever becoming work — which is the point of keeping them.

const reportState = { all: [], filtered: [], page: 0, search: '', state: '' };

function expandReports(data) {
  const { dict, cols } = data;
  return cols.id.map((id, i) => {
    const row = {
      i, id,
      town: dict.town[cols.t[i]],
      postcode: cols.pc[i],
      street: cols.st[i],
      reported: cols.r[i],
      firstSeen: cols.f[i],
      gone: cols.g[i],
      lon: cols.lon[i],
      lat: cols.lat[i],
    };
    row.age = row.reported === null ? null : data.today - row.reported;
    row.haystack = [row.street, row.postcode, row.town].filter(Boolean).join(' ').toLowerCase();
    row.faults = (data.faults || {})[i] || [];
    return row;
  });
}

function applyReportFilters() {
  const needle = reportState.search.trim().toLowerCase();
  reportState.filtered = reportState.all.filter((r) => {
    if (reportState.state === 'current' && r.gone !== null) return false;
    if (reportState.state === 'gone' && r.gone === null) return false;
    if (needle && !r.haystack.includes(needle)) return false;
    return true;
  });
  reportState.page = 0;
  renderReports();
}

function renderReports() {
  const table = $('#table-reports');
  table.innerHTML =
    '<thead><tr><th>Reported</th><th>Problem</th><th>Age</th><th>Where</th><th>Town</th>' +
    '<th>On the map now?</th></tr></thead>';
  const body = document.createElement('tbody');
  const start = reportState.page * PAGE_SIZE;

  for (const r of reportState.filtered.slice(start, start + PAGE_SIZE)) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${formatDate(r.reported)}</td>
      <td title="Thames Water's label for this layer. Where a pin can be matched to a work order raised soon after on the same street, 92% are leak investigations — but the label is theirs, not a per-report classification">Leak</td>
      <td class="num age age-${ageClass(r.age)}">${formatAge(r.age)}</td>
      <td class="wrap">${locationCell(r, r.town)}</td>
      <td>${escape(titleCase(r.town || ''))}</td>
      <td>${r.gone === null
        ? '<span class="pill clean">Showing</span>'
        : `<span class="pill">Gone ${escape(formatDate(r.gone))}</span>`}</td>`;
    tr.addEventListener('click', () => openReportDetail(r));
    body.append(tr);
  }
  table.replaceChildren(table.querySelector('thead'), body);

  $('#r-count').textContent =
    `${formatNumber(reportState.filtered.length)} of ${formatNumber(reportState.all.length)} reports`;

  const pages = Math.ceil(reportState.filtered.length / PAGE_SIZE) || 1;
  const pager = $('#r-pager');
  pager.replaceChildren();
  if (pages > 1) {
    const prev = document.createElement('button');
    prev.textContent = '‹ Previous';
    prev.disabled = reportState.page === 0;
    prev.addEventListener('click', () => { reportState.page--; renderReports(); window.scrollTo({ top: 0 }); });
    const label = document.createElement('span');
    label.textContent = `Page ${reportState.page + 1} of ${formatNumber(pages)}`;
    const next = document.createElement('button');
    next.textContent = 'Next ›';
    next.disabled = reportState.page >= pages - 1;
    next.addEventListener('click', () => { reportState.page++; renderReports(); window.scrollTo({ top: 0 }); });
    pager.append(prev, label, next);
  }
}

function openReportDetail(r) {
  const rows = [
    ['Reported', formatDate(r.reported)],
    ['Age', r.age === null ? null : `${formatNumber(r.age)} days`],
    ['Location', titleCase(r.street || '')],
    ['Town', titleCase(r.town || '')],
    ['Postcode', r.postcode],
    ['Coordinates', hasAddress(r, r.town) || r.lat === null || r.lat === undefined
      ? null : `${r.lat.toFixed(5)}, ${r.lon.toFixed(5)}`],
    ['First recorded here', formatDate(r.firstSeen)],
  ].filter(([, v]) => v);

  const fate = r.gone === null
    ? `<li><strong>Still showing on Thames Water's map</strong>
         <div class="when">as of the last collection</div></li>`
    : `<li><strong>Dropped off Thames Water's map</strong>
         <div class="when">${escape(formatDate(r.gone))}</div></li>`;

  $('#detail-body').innerHTML = `
    <h3>Reported problem${r.street ? ' — ' + escape(titleCase(r.street)) : ''}</h3>
    <p class="sub">${hasAddress(r, r.town)
      ? escape(titleCase(r.town || '')) + ' ' + escape(r.postcode || '')
      : 'No address published'}</p>
    <dl class="kv">${rows.map(([k, v]) => `<dt>${escape(k)}</dt><dd>${escape(v)}</dd>`).join('')}</dl>
    <p class="footnote" style="margin:0 0 18px">&ldquo;Leak&rdquo; is Thames Water's label for this
    whole layer, not a classification of this report: the feed carries no per-report problem type
    (<code>ProblemType</code> is <code>1</code> on every pin). It is broadly accurate — where a pin
    can be matched to a work order raised soon after on the same street, 92% are leak
    investigations — but a small number become flooding or blockage work instead.</p>
    <h2 style="font-size:14px;margin:0 0 10px">What we have seen</h2>
    <ul class="timeline">
      <li><strong>Reported to Thames Water</strong><div class="when">${escape(formatDate(r.reported))}</div></li>
      ${fate}
    </ul>
    ${renderReportLinks(r.faults, r.reported)}
    ${hasAddress(r, r.town) ? '' : `<p class="footnote">${NO_ADDRESS_NOTE}</p>`}
    <p style="margin-top:16px;color:var(--text-dim);font-size:13px">
      ${r.faults && r.faults.length
        ? 'A public report carries no reference number or repair status of its own. Any status above belongs to the work order, not to this report.'
        : 'This is a public report, not yet a work order. It carries no reference number or repair status until Thames Water raises one.'}
    </p>
    ${r.lat ? `<p style="margin-top:10px"><a href="https://www.openstreetmap.org/?mlat=${r.lat}&mlon=${r.lon}#map=17/${r.lat}/${r.lon}" target="_blank" rel="noopener">View location on OpenStreetMap ↗</a></p>` : ''}
  `;
  $('#detail').showModal();
}

function exportReportsCSV() {
  const header = ['reported', 'problem', 'age_days', 'street', 'postcode', 'town', 'still_on_map', 'left_map', 'lat', 'lon'];
  const rows = reportState.filtered.map((r) => [
    r.reported === null ? '' : dayToDate(r.reported).toISOString().slice(0, 10),
    'Leak', r.age ?? '', r.street, r.postcode, r.town,
    r.gone === null ? 'yes' : 'no',
    r.gone === null ? '' : dayToDate(r.gone).toISOString().slice(0, 10),
    r.lat, r.lon,
  ]);
  downloadCSV([header, ...rows], `thames-water-reports-${new Date().toISOString().slice(0, 10)}.csv`);
}

function renderReportsBlurb() {
  const r = state.summary.reports;
  if (!r || !r.current) {
    $('#reports-blurb').textContent =
      'No public reports have been collected yet.';
    return;
  }
  const retention = r.retention_days ? `about ${r.retention_days} days` : 'a short rolling window';
  $('#reports-blurb').innerHTML =
    `When you report a problem, it appears on Thames Water's map straight away as a "Leak" pin — ` +
    `before any work order exists. Those pins carry no reference number and no repair status, and ` +
    `Thames Water only keeps <strong>${escape(retention)}</strong> of them. A report that is never ` +
    `turned into work simply disappears. There are <strong>${formatNumber(r.current)}</strong> ` +
    `showing right now; we keep them after they go.`;
}

// ── Map ─────────────────────────────────────────────────────────

function ageColour(days) {
  return getComputedStyle(document.documentElement).getPropertyValue(`--age-${ageClass(days)}`).trim();
}

function renderMap() {
  // Leaflet comes from a CDN; the rest of the site must still work without it.
  if (typeof L === 'undefined') {
    $('#map').innerHTML =
      '<div class="chart-empty" style="margin:0">The map library could not be loaded. ' +
      'Everything else on this page still works — try the Faults tab.</div>';
    return;
  }
  if (!state.map) {
    state.map = L.map('map', { preferCanvas: true }).setView([51.5, -0.5], 9);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 18,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(state.map);
  }
  if (state.mapLayer) state.mapLayer.remove();

  const points = state.filtered.filter((x) => x.lat !== null && x.lon !== null);
  const markers = points.map((fault) =>
    L.circleMarker([fault.lat, fault.lon], {
      radius: 4,
      weight: 1,
      color: ageColour(fault.age),
      fillColor: ageColour(fault.age),
      fillOpacity: 0.65,
    }).on('click', () => openDetail(fault)),
  );

  // Public reports, drawn hollow so they read as "reported, not yet work".
  const reports = reportState.all.filter((r) => r.gone === null && r.lat !== null && r.lon !== null);
  for (const r of reports) {
    markers.push(
      L.circleMarker([r.lat, r.lon], {
        radius: 3.5,
        weight: 1.5,
        color: 'var(--c-report)',
        fillOpacity: 0,
      }).on('click', () => openReportDetail(r)),
    );
  }

  state.mapLayer = L.layerGroup(markers).addTo(state.map);
  // The map plots whatever the Faults view is filtered to, so it has to say
  // which set that is — otherwise cleared faults read as the live backlog.
  $('#map-note').textContent = state.mode === 'cleared'
    ? `${formatNumber(points.length)} cleared faults (coloured by how long they took) and ${formatNumber(reports.length)} reports plotted`
    : `${formatNumber(points.length)} faults and ${formatNumber(reports.length)} reports plotted`;
  setTimeout(() => state.map.invalidateSize(), 0);
}

// ── Places ──────────────────────────────────────────────────────

// Ranked by faults per 10,000 households rather than raw count: a raw count
// mostly ranks population, which tells you nothing about how Thames Water is
// performing in one place versus another.
function renderPlaces() {
  const areas = state.summary.areas;
  const table = $('#table-places');

  if (!areas || !areas.available) {
    $('#places-blurb').textContent =
      'Reference data for normalising by households is missing, so this table is unavailable.';
    table.replaceChildren();
    return;
  }

  $('#places-blurb').innerHTML =
    `Ranked by open faults per 10,000 households, so the table is not simply a map of where ` +
    `people live. Authorities with fewer than ${formatNumber(areas.min_faults)} open faults ` +
    // One decimal deliberately: this is the page's own statement about its data
    // quality, and formatNumber would round 98.7 up to 99.
    `are excluded. ${areas.coverage.toFixed(1)}% of open faults have a postcode we could ` +
    `place in a local authority.`;

  table.innerHTML =
    '<thead><tr><th>Local authority</th><th>Per 10,000 homes</th><th>Open faults</th>' +
    '<th>Households</th><th>Median age</th><th>Open over a year</th></tr></thead>';
  const body = document.createElement('tbody');
  const worst = Math.max(...areas.rows.map((r) => r.per_10k || 0), 1);

  for (const row of areas.rows) {
    const tr = document.createElement('tr');
    // A bar in the cell makes the spread legible without a separate chart.
    const pct = ((row.per_10k || 0) / worst) * 100;
    tr.innerHTML = `
      <td>${escape(row.name)}</td>
      <td class="num rate"><span class="rate-bar" style="width:${pct.toFixed(1)}%"></span>
        <span class="rate-value">${row.per_10k === null ? '—' : row.per_10k.toFixed(1)}</span></td>
      <td class="num">${formatNumber(row.n)}</td>
      <td class="num">${formatNumber(row.households)}</td>
      <td class="num age age-${ageClass(row.median_age)}">${row.median_age === null ? '—' : formatNumber(row.median_age) + 'd'}</td>
      <td class="num">${formatNumber(row.over_year)}</td>`;
    tr.addEventListener('click', () => {
      state.filters.search = row.name;
      $('#f-search').value = row.name;
      applyFilters();
      show('faults');
    });
    body.append(tr);
  }
  table.append(body);

  $('#places-footnote').innerHTML =
    `Households from ${escape(areas.source)}. ` +
    `<strong>A low rate does not necessarily mean good performance:</strong> Thames Water supplies ` +
    `only part of some authorities, so faults are counted for their area while households are ` +
    `counted for the whole of it. Normalising by households also does not adjust for network age, ` +
    `pipe material or ground conditions, all of which plausibly drive real differences in fault rates.`;
}

// Deliberately states its own n and how much is unaccounted for: with a day of
// overlap this is a small sample, and reporting the percentage alone would
// imply more certainty than there is.
function renderClosure() {
  const c = state.summary.closure;
  if (!c || !c.listed_total) {
    $('#card-closure').hidden = true;
    return;
  }
  const completed = c.matched_by_status.Completed || 0;
  const cancelled = c.matched_by_status.Canceled || 0;
  const pct = c.matched ? ((cancelled / c.matched) * 100).toFixed(1) : null;

  // "Repair Complete" is excluded from the verdicts upstream, so say why here
  // rather than leaving a hole in the arithmetic.
  const inconclusive = c.inconclusive
    ? ` A further ${formatNumber(c.inconclusive)} are listed only as
        &ldquo;${escape(c.inconclusive_status)}&rdquo;, which is not an outcome: four in five
        records carrying it still have outstanding line items on them, and the count more often
        rises than falls at the moment it is applied. Those are counted here as unexplained.`
    : '';

  $('#closure-body').innerHTML = `
    <div class="stat-row">
      <div><span class="stat-value">${formatNumber(c.departed)}</span><span class="stat-label">left the map since tracking began</span></div>
      <div><span class="stat-value">${formatNumber(completed)}</span><span class="stat-label">confirmed completed</span></div>
      <div><span class="stat-value ${cancelled ? 'bad' : ''}">${formatNumber(cancelled)}</span><span class="stat-label">cancelled, not repaired</span></div>
      <div><span class="stat-value">${formatNumber(c.unexplained)}</span><span class="stat-label">no outcome published</span></div>
    </div>
    <p class="footnote" style="margin-top:0">
      ${c.matched
        ? `Of the ${formatNumber(c.matched)} carrying a verdict that says what happened,
           <strong>${pct}%</strong> were cancelled rather than repaired. `
        : ''}${formatNumber(c.unexplained - (c.inconclusive || 0))} never appeared in the closed
      feed at all, so we cannot say what happened to them — that feed is also a rolling window, and
      a fault can pass through it between collections.${inconclusive} Thames Water lists
      <strong>${formatNumber(c.listed_total)}</strong> closed work orders right now, of which only
      ${formatNumber(c.with_closure_date)} carry a closure date. Small sample so far: tracking
      began on ${escape(new Date(state.summary.totals.first_snapshot).toLocaleDateString('en-GB'))}.
    </p>`;
}

// The GSS panel. Careful wording matters here: these are work orders about
// external sewer flooding investigations, not adjudicated compensation cases.
function renderFlooding() {
  const f = state.summary.external_flooding;
  if (!f || !f.open) {
    $('#card-flooding').hidden = true;
    return;
  }
  const statuses = Object.entries(f.by_status)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `<li><strong>${formatNumber(v)}</strong> ${escape(k)}</li>`)
    .join('');

  $('#flooding-body').innerHTML = `
    <div class="stat-row">
      <div><span class="stat-value">${formatNumber(f.open)}</span><span class="stat-label">open now</span></div>
      <div><span class="stat-value">${formatNumber(f.age.p50)}d</span><span class="stat-label">median age</span></div>
      <div><span class="stat-value">${formatNumber(f.over_90d)}</span><span class="stat-label">open over 3 months</span></div>
      <div><span class="stat-value">${formatNumber(f.over_year)}</span><span class="stat-label">open over a year</span></div>
      <div><span class="stat-value">${formatAge(f.age.max)}</span><span class="stat-label">oldest</span></div>
    </div>

    <ul class="inline-list">${statuses}</ul>

    <blockquote>
      <p>In the Consultation, Ofwat states, “the company must make an automatic GSS payment” when
      referring to an external sewer flooding event. We believe that this is a mistake, that a
      payment should not be made automatically, and that eligibility for this payment still
      requires the customer to claim and demonstrate how they were affected.</p>
      <cite>Thames Water, response to Ofwat's consultation on the Guaranteed Standards Scheme,
      September 2025, page 3</cite>
    </blockquote>

    <p class="footnote">
      These are work orders Thames Water has raised and coded
      <code>${escape(f.work_type)}</code>. They are <strong>not</strong> confirmed
      compensation-eligible flooding incidents, and the two are not interchangeable — an
      investigation may find nothing, and eligibility is exactly what is in dispute. Nothing here
      states or implies that any sum is owed.
    </p>`;
}

// ── Routing ─────────────────────────────────────────────────────

function show(view) {
  state.view = view;
  for (const button of document.querySelectorAll('.tabs button')) {
    button.classList.toggle('active', button.dataset.view === view);
  }
  for (const section of document.querySelectorAll('.view')) {
    section.hidden = section.id !== `view-${view}`;
  }
  location.hash = view;
  if (view === 'map') renderMap();
}

function fillSelect(id, values, allLabel) {
  const select = $(id);
  select.replaceChildren(new Option(allLabel, ''));
  for (const value of values) select.append(new Option(value, value));
}

function wireUp() {
  for (const button of document.querySelectorAll('.tabs button')) {
    button.addEventListener('click', () => show(button.dataset.view));
  }

  const search = $('#f-search');
  let timer;
  search.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => { state.filters.search = search.value; applyFilters(); }, 160);
  });

  for (const [id, key] of [['#f-source', 'source'], ['#f-status', 'status'], ['#f-journey', 'journey'],
                          ['#f-age', 'minAge'], ['#f-cleared', 'clearedWithin'], ['#f-verdict', 'verdict']]) {
    $(id).addEventListener('change', (e) => { state.filters[key] = e.target.value; applyFilters(); });
  }

  for (const button of document.querySelectorAll('.range-bar button')) {
    button.addEventListener('click', () => {
      state.range = button.dataset.range ? Number(button.dataset.range) : null;
      for (const other of document.querySelectorAll('.range-bar button')) {
        other.classList.toggle('active', other === button);
      }
      renderOverview();
      renderBacklogNote(state.summary.truncated);
    });
  }

  $('#f-mode').addEventListener('change', (e) => {
    state.mode = e.target.value;
    // Sorting by clearance date is the whole reason for this mode; age is the
    // right default for open faults. Reset rather than carry over, since
    // `cleared` is not a key the open rows even have.
    state.sort = state.mode === 'cleared' ? { key: 'clearedAt', dir: -1 } : { key: 'age', dir: -1 };
    syncModeUI();
    applyFilters();
  });

  $('#f-reset').addEventListener('click', () => {
    state.filters = { search: '', source: '', status: '', journey: '', minAge: '', clearedWithin: '', verdict: '' };
    search.value = '';
    for (const id of ['#f-source', '#f-status', '#f-journey', '#f-age', '#f-cleared', '#f-verdict']) $(id).value = '';
    applyFilters();
  });

  $('#f-export').addEventListener('click', exportCSV);

  const reportSearch = $('#r-search');
  let reportTimer;
  reportSearch.addEventListener('input', () => {
    clearTimeout(reportTimer);
    reportTimer = setTimeout(() => { reportState.search = reportSearch.value; applyReportFilters(); }, 160);
  });
  $('#r-state').addEventListener('change', (e) => { reportState.state = e.target.value; applyReportFilters(); });
  $('#r-reset').addEventListener('click', () => {
    reportState.search = '';
    reportState.state = '';
    reportSearch.value = '';
    $('#r-state').value = '';
    applyReportFilters();
  });
  $('#r-export').addEventListener('click', exportReportsCSV);

  // Cross-links are rendered into the dialog, so delegate rather than rebind.
  $('#detail-body').addEventListener('click', (e) => {
    const toFault = e.target.closest('[data-fault]');
    const toReport = e.target.closest('[data-report]');
    if (!toFault && !toReport) return;
    e.preventDefault();
    if (toFault) {
      const fault = state.faults.find((f) => f.id === toFault.dataset.fault);
      if (fault) openDetail(fault);
    } else {
      const report = reportState.all.find((r) => r.id === toReport.dataset.report);
      if (report) openReportDetail(report);
    }
  });

  // The backlog pointer is a same-page link into the notes view; switch view
  // first, then scroll, since the target is hidden until the view is shown.
  document.addEventListener('click', (e) => {
    const link = e.target.closest('a[data-note]');
    if (!link) return;
    e.preventDefault();
    show('notes');
    const target = document.getElementById(`note-${link.dataset.note}`);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  $('#detail .dialog-close').addEventListener('click', () => $('#detail').close());
  $('#detail').addEventListener('click', (e) => { if (e.target.id === 'detail') $('#detail').close(); });
}

// The caveat belongs next to the number, not in a box at the top of the page.
// When a collection is flagged, the backlog chart is the figure it distorts, so
// the pointer sits under that chart and links to the note explaining it.
function renderBacklogNote(info) {
  const host = $('#backlog-note');
  if (!host) return;
  // Only when the collection it describes is actually on screen: pointing at
  // "the step on 5 August" under a chart that starts on the 9th is worse than
  // saying nothing.
  const backlog = state.summary.backlog;
  if (!eventVisible(info, backlog[backlog.length - 1].t, state.range)) {
    host.hidden = true;
    return;
  }
  const when = new Date(info.observed_at)
    .toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric' });
  const iso = info.observed_at.slice(0, 10);
  host.hidden = false;
  host.innerHTML = info.kind === 'truncated'
    ? `The most recent collection could not be read completely, so this chart ends at the last one `
      + `that could. <a href="#notes" data-note="${escape(iso)}">Read the note</a>.`
    : `The step on ${escape(when)} is <strong>${formatNumber(info.departed)}</strong> work orders `
      + 'that stopped being published in a single collection. It is not a record of work completed — '
      + `<a href="#notes" data-note="${escape(iso)}">read the note</a>.`;
}

// Where the open backlog is sitting. Deliberately a censored view rather than
// median time-in-stage: see stage_occupancy in build_site.py for why the median
// is computable and wrong.
function renderStages(d) {
  const host = $('#stages-body');
  if (!host) return;
  if (!d || !d.stages.length) { host.innerHTML = '<p class="footnote">Not enough history yet.</p>'; return; }

  const total = d.stages.reduce((a, s) => a + s.n, 0);
  const head = d.bucket_days.map((b) =>
    `<th>Not moved for ${b === 1 ? '24 hours' : `${b} days`}</th>`).join('');
  const rows = d.stages.map((s) => `
    <tr>
      <td>${escape(s.stage)}</td>
      <td class="num">${formatNumber(s.n)}</td>
      <td class="num">${(100 * s.n / total).toFixed(1)}%</td>
      ${s.buckets.map((n) => `<td class="num">${formatNumber(n)}` +
        `<span class="pinned"> (${Math.round(100 * n / s.n)}%)</span></td>`).join('')}
    </tr>`).join('');

  const back = d.backwards.length ? `
    <h3 style="font-size:13.5px;margin:20px 0 6px">Work that went backwards</h3>
    <p class="footnote" style="margin:0 0 8px">Thames Water moved these back to an earlier stage —
      a repair that did not hold, or a job reopened. ${formatNumber(d.backwards_total)} observed so far.</p>
    <ul class="linklist">${d.backwards.map(([label, n]) =>
      `<li><strong>${formatNumber(n)}</strong> — ${escape(label)}</li>`).join('')}</ul>` : '';

  host.innerHTML = `
    <div class="table-wrap"><table>
      <thead><tr><th>Stage</th><th>Open here</th><th>Share</th>${head}</tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    <p class="footnote">A fault counted as "not moved" may still be being worked on — this is
      the published status changing, not the work.</p>
    ${back}`;
}

// ── Permits ─────────────────────────────────────────────────────
//
// A second source on a different clock — DfT's Street Manager archive, monthly
// in arrears, where the fault map is polled hourly. The card hides itself when
// no extract is committed rather than rendering zeroes.
//
// The two breakdowns are load-bearing, not decoration. Works that finish inside
// a single month's archive are disproportionately short works, and the late
// rate climbs steeply with both duration and category — so the headline is a
// floor, and these tables are why we are entitled to say so.
// How much the headline is missing, shown rather than asserted.
//
// A work still running when the last archive closes cannot be counted, so
// recent months under-count long jobs — and long jobs overrun most. The control
// column is what makes this an argument rather than a hunch: works finishing
// inside two days are fully observed in every month, so their rate holds steady
// while the all-works rate climbs with the observation window. The gap between
// them is the censoring, in percentage points.
function cohorts(rows) {
  if (!rows || rows.length < 2) return '';
  const month = (m) => new Date(`${m}-01T00:00:00Z`)
    .toLocaleDateString('en-GB', { month: 'long', year: 'numeric', timeZone: 'UTC' });
  const best = rows.reduce((a, b) => (b.gap > a.gap ? b : a));
  return `
    <div class="table-wrap"><table>
      <thead><tr>
        <th>Works starting in</th><th>Works</th><th>Share late</th>
        <th>Of those, finishing in under 2 days</th><th>Share late</th><th>Gap</th>
      </tr></thead>
      <tbody>${rows.map((r) => `
        <tr><td>${escape(month(r.month))}</td>
            <td class="num">${formatNumber(r.n)}</td>
            <td class="num">${r.pct.toFixed(1)}%</td>
            <td class="num pinned">${formatNumber(r.short_n)}</td>
            <td class="num pinned">${r.short_pct.toFixed(1)}%</td>
            <td class="num"><strong>${r.gap > 0 ? '+' : ''}${r.gap.toFixed(1)}pp</strong></td></tr>`).join('')}
      </tbody>
    </table></div>
    <p class="footnote"><strong>Read the headline as a floor, and this is roughly how far
      short.</strong> A job still running when the last month of records closes cannot be
      counted at all, so the most recent months are missing their longest jobs &mdash; and
      long jobs are the ones that overrun. The short-job column is the control: those finish
      well within any month, so they are counted the same way throughout, and their rate
      barely moves. The earliest months, which have had the longest to be observed, run up to
      ${best.gap.toFixed(1)} percentage points above their own short-job rate. Later months
      have not had that time yet.</p>`;
}

function renderPermits(d) {
  const host = $('#permits-body');
  if (!host) return;
  if (!d || !d.finished) return;
  $('#card-permits').hidden = false;

  const pct = (n) => `${(100 * n / d.finished).toFixed(1)}%`;
  // Duration bands have a natural order and must keep it — the point of the
  // table is that the rate climbs monotonically down the rows. Categories have
  // no inherent order, so they sort by the rate itself.
  const DURATIONS = ['under a day', '1-2 days', '2-5 days', '5-10 days', '10-30 days', 'over 30 days'];
  const band = (rows, label, order) => {
    const keys = Object.keys(rows).sort((a, b) => (order
      ? order.indexOf(a) - order.indexOf(b)
      : rows[b].pct - rows[a].pct));
    return `
      <div class="table-wrap"><table>
        <thead><tr><th>${label}</th><th>Works</th><th>Late</th><th>Share late</th></tr></thead>
        <tbody>${keys.map((k) => `
          <tr><td>${escape(k)}</td>
              <td class="num">${formatNumber(rows[k].n)}</td>
              <td class="num">${formatNumber(rows[k].late)}</td>
              <td class="num">${rows[k].pct.toFixed(1)}%</td></tr>`).join('')}
        </tbody>
      </table></div>`;
  };

  host.innerHTML = `
    <div class="table-wrap"><table>
      <thead><tr><th>Against the end date they applied for</th><th>Works</th><th>Share</th></tr></thead>
      <tbody>
        <tr><td>Finished early</td><td class="num">${formatNumber(d.early)}</td>
            <td class="num">${pct(d.early)}</td></tr>
        <tr><td>Finished on the last permitted day</td><td class="num">${formatNumber(d.on_last_day)}</td>
            <td class="num">${pct(d.on_last_day)}</td></tr>
        <tr><td>Finished late</td><td class="num">${formatNumber(d.late)}</td>
            <td class="num">${d.late_pct.toFixed(1)}%</td></tr>
        <tr><td class="pinned" style="padding-left:18px">&hellip;of which, by exactly one day</td>
            <td class="num pinned">${formatNumber(d.late_by_one_day)}</td>
            <td class="num pinned">${pct(d.late_by_one_day)}</td></tr>
        <tr><td class="pinned" style="padding-left:18px">&hellip;more than a day late</td>
            <td class="num pinned">${formatNumber(d.over_a_day_late)}</td>
            <td class="num pinned">${pct(d.over_a_day_late)}</td></tr>
      </tbody>
    </table></div>
    <p class="footnote">${formatNumber(d.permits)} permits published for
      ${d.months.map(escape).join(', ')}; ${formatNumber(d.finished)} of them record both a
      proposed and an actual end date. The rest are planned, cancelled, or still running.
      Where an extension was applied for and granted, the comparison uses the revised date &mdash;
      an approved extension is not an overrun. Worst overrun observed: ${d.max_days_late} days.</p>
    ${band(d.by_duration, 'How long the work actually took', DURATIONS)}
    ${band(d.by_category, 'Work category')}
    ${cohorts(d.by_start_month)}`;
}

// ── Notes ───────────────────────────────────────────────────────
//
// Dated narrative entries from data/notes.json. This replaced a banner across
// the top of the overview: an alarming yellow box is the wrong shape for
// something that stays true for weeks, and it left no room to show the working.
// A note can carry the evidence and the reasoning, and it stays readable long
// after the event stops being news.

// A deliberately tiny inline vocabulary — bold, italic, code, links — rather than raw
// HTML. Entries are trusted repo content, so this is not about safety; it is so
// the JSON stays legible as prose to whoever writes the next one.
function inlineMarkup(text) {
  return escape(text)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

function renderBlock(block) {
  if (block.p) return `<p>${inlineMarkup(block.p)}</p>`;
  if (block.list) return `<ul>${block.list.map((i) => `<li>${inlineMarkup(i)}</li>`).join('')}</ul>`;
  if (block.table) {
    const head = block.table.head.map((h) => `<th>${inlineMarkup(h)}</th>`).join('');
    const rows = block.table.rows.map((r) =>
      `<tr>${r.map((cell, i) => `<td class="${i ? 'num' : ''}">${inlineMarkup(cell)}</td>`).join('')}</tr>`).join('');
    return `<div class="table-wrap"><table class="note-table"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>`;
  }
  return '';
}

// More than one thing can happen on a day, so the date alone is not a unique
// id. Keep the bare date for the first entry of a day — that is what the
// backlog pointer links to — and suffix any others.
function noteId(entry, i, entries) {
  const earlier = entries.slice(0, i).filter((e) => e.date === entry.date).length;
  return `note-${entry.date}${earlier ? `-${earlier + 1}` : ''}`;
}

function renderNotes(data) {
  const host = $('#notes-body');
  const entries = (data && data.entries) || [];
  if (!entries.length) {
    host.innerHTML = '<section class="card"><p class="footnote">Nothing noted yet.</p></section>';
    return;
  }

  host.innerHTML = entries.map((entry, i) => {
    const when = new Date(entry.date + 'T00:00:00Z')
      .toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric' });
    const refs = (entry.refs || []).length
      ? `<p class="footnote note-refs">${entry.refs.map((r) =>
          `<a href="${escape(r.url)}" target="_blank" rel="noopener">${escape(r.label)}</a>`).join(' · ')}</p>`
      : '';
    return `
      <section class="card note" id="${noteId(entry, i, entries)}">
        <header>
          <p class="note-date"><time datetime="${escape(entry.date)}">${escape(when)}</time></p>
          <h2>${escape(entry.title)}</h2>
          ${entry.summary ? `<p class="note-summary">${inlineMarkup(entry.summary)}</p>` : ''}
        </header>
        <div class="prose note-body">${entry.body.map(renderBlock).join('')}</div>
        ${refs}
      </section>`;
  }).join('');
}

// ── Boot ────────────────────────────────────────────────────────

async function main() {
  wireUp();
  try {
    const [summary, open, reports, cleared, noteData, permitData] = await Promise.all([
      loadJSON('data/summary.json'),
      loadJSON('data/open.json'),
      loadJSON('data/reports.json').catch(() => null),
      // Tolerated missing so the site still loads against an older data build.
      loadJSON('data/cleared.json').catch(() => null),
      loadJSON('data/notes.json').catch(() => null),
      loadJSON('data/permits.json').catch(() => null),
    ]);

    state.summary = summary;
    reportState.all = reports ? expandReports(reports) : [];
    reportState.filtered = reportState.all;
    state.epoch = open.epoch;
    state.today = open.today;
    state.faults = expand(open);
    state.history = open.history;
    state.filtered = state.faults;
    state.cleared = cleared ? expandCleared(cleared) : [];
    state.clearedWindow = cleared ? cleared.window_days : null;
    state.latest = cleared ? cleared.latest : null;
    if (!state.cleared.length) $('#f-mode').hidden = true;
    syncModeUI();

    const collected = summary.totals.latest_snapshot;
    $('#freshness-value').textContent = collected
      ? new Date(collected).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
      : 'never';

    fillSelect('#f-source', summary.sources.map((s) => s.key), 'Both networks');
    for (const option of $('#f-source').options) {
      const match = summary.sources.find((s) => s.key === option.value);
      if (match) option.textContent = match.label;
    }
    fillSelect('#f-status', summary.status_order.filter((s) => summary.by_status[s]), 'Any status');
    fillSelect('#f-journey', Object.keys(summary.by_journey), 'Any problem');

    renderNotes(noteData);
    renderBacklogNote(summary.truncated);
    renderOverview();
    renderOldest();
    renderFlooding();
    renderClosure();
    renderStages(state.summary.stages);
    renderPermits(permitData);
    renderPlaces();
    renderReportsBlurb();
    applyFilters();
    applyReportFilters();

    $('#loading').hidden = true;
    show(location.hash.slice(1) || 'overview');
  } catch (error) {
    $('#loading').hidden = true;
    const box = $('#error');
    box.hidden = false;
    box.textContent = `Could not load the fault data: ${error.message}`;
    console.error(error);
  }
}

main();

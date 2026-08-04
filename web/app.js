// Thames Water fault tracker — UI.
//
// Reads the compact JSON produced by collector/build_site.py. `open.json` is
// dictionary-encoded columnar data (~20k faults), which we expand once into
// plain objects; everything after that is ordinary array work.

import { areaChart, barChart, flowChart, formatNumber } from './charts.js';
import { VirtualTable } from './table.js';

const AGE_CLASSES = [7, 30, 90, 365];
const ROW_HEIGHT = 33; // keep in step with `.virtual tbody tr` in style.css

const state = {
  summary: null,
  faults: [],
  history: {},
  epoch: null,
  today: 0,
  view: 'overview',
  filters: { search: '', source: '', status: '', journey: '', minAge: '' },
  sort: { key: 'age', dir: -1 },
  filtered: [],
  table: null,
  map: null,
  mapLayer: null,
};

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
  return text.replace(/\b[A-Z]{2,}\b/g, (word) => word[0] + word.slice(1).toLowerCase());
}

function place(fault) {
  return [titleCase(fault.street), titleCase(fault.city)].filter(Boolean).join(', ');
}

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
      workOrder: cols.wo[i],
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
    };
    out[i].haystack = [out[i].street, out[i].postcode, out[i].city, out[i].workOrder, out[i].journey]
      .filter(Boolean).join(' ').toLowerCase();
  }
  // Statuses come from the dictionary in first-seen order; sort by the real
  // lifecycle so history timelines read in the right sequence.
  state.statusDict = dict.status;
  return out;
}

// ── Overview ────────────────────────────────────────────────────

function sourceColour(key) {
  return key === 'clean' ? 'var(--c-clean)' : 'var(--c-waste)';
}

function renderKPIs() {
  const s = state.summary;
  const buckets = s.age.buckets || {};
  const overYear = (buckets['<=730'] || 0) + (buckets['>730'] || 0);

  // Net movement over the last week of snapshots: the single most telling number.
  const flow = s.flow.slice(-7);
  const raised = flow.reduce((a, f) => a + f.raised, 0);
  const cleared = flow.reduce((a, f) => a + f.resolved, 0);
  const net = raised - cleared;
  const haveFlow = s.flow.length >= 2;

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
          label: 'Backlog, last 7 days',
          value: `${net > 0 ? '+' : ''}${formatNumber(net)}`,
          tone: net > 0 ? 'bad' : 'good',
          note: `${formatNumber(raised)} arrived, ${formatNumber(cleared)} cleared`,
        }
      : {
          label: 'Backlog trend',
          value: '–',
          note: 'Needs a second daily snapshot',
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
          note: `median of ${formatNumber(s.resolution.n)} faults cleared recently`,
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

function renderOverview() {
  const s = state.summary;
  renderKPIs();

  const series = s.sources.map((src) => ({
    key: src.key,
    label: src.label,
    colour: sourceColour(src.key),
    values: s.backlog.map((row) => ({ x: row.d, y: row[src.key] || 0 })),
  }));
  areaChart($('#chart-backlog'), series, { epoch: s.epoch, stacked: true });
  $('#legend-backlog').innerHTML = series
    .map((x) => `<span><span class="swatch" style="background:${x.colour}"></span>${x.label}</span>`)
    .join('');

  flowChart($('#chart-flow'), s.flow, { epoch: s.epoch });

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
      <td class="wrap">${escape(place(fault))} <span class="mono">${escape(fault.postcode || '')}</span></td>
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

function applyFilters() {
  const f = state.filters;
  const needle = f.search.trim().toLowerCase();
  const minAge = f.minAge ? Number(f.minAge) : null;

  state.filtered = state.faults.filter((x) => {
    if (f.source && x.source !== f.source) return false;
    if (f.status && x.status !== f.status) return false;
    if (f.journey && x.journey !== f.journey) return false;
    if (minAge !== null && !(x.age > minAge)) return false;
    if (needle && !x.haystack.includes(needle)) return false;
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

  renderFaults();
}

const COLUMNS = [
  { key: 'raised', label: 'Raised', render: (x) => formatDate(x.raised) },
  { key: 'age', label: 'Age', cls: 'num', render: (x) => `<span class="age age-${ageClass(x.age)}">${formatAge(x.age)}</span>` },
  { key: 'journey', label: 'Problem', render: (x) => escape(x.journey || '') },
  { key: 'street', label: 'Where', cls: 'wrap', render: (x) => `${escape(place(x))} <span class="mono">${escape(x.postcode || '')}</span>` },
  { key: 'status', label: 'Status', render: (x) => escape(x.status || '') },
  { key: 'source', label: 'Network', render: (x) => `<span class="pill ${x.source}">${x.source === 'clean' ? 'Clean' : 'Waste'}</span>` },
  { key: 'workOrder', label: 'Work order', cls: 'mono', render: (x) => escape(x.workOrder || '') },
];

function renderFaults() {
  if (!state.table) {
    state.table = new VirtualTable($('#table-faults'), {
      columns: COLUMNS,
      rowHeight: ROW_HEIGHT,
      onRowClick: openDetail,
      onSort: (key) => {
        if (state.sort.key === key) state.sort.dir *= -1;
        else state.sort = { key, dir: key === 'age' ? -1 : 1 };
        applyFilters();
      },
      emptyMessage: 'No faults match those filters.',
    });
  }
  state.table.setRows(state.filtered, state.sort);
  $('#f-count').textContent =
    `${formatNumber(state.filtered.length)} of ${formatNumber(state.faults.length)} faults`;
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

function exportCSV() {
  const header = ['work_order', 'raised', 'age_days', 'status', 'problem', 'work_type', 'street', 'postcode', 'town', 'network', 'lat', 'lon'];
  const rows = state.filtered.map((x) => [
    x.workOrder, x.raised === null ? '' : dayToDate(x.raised).toISOString().slice(0, 10), x.age ?? '',
    x.status, x.journey, x.workType, x.street, x.postcode, x.city, x.source, x.lat, x.lon,
  ]);
  downloadCSV([header, ...rows], `thames-water-faults-${new Date().toISOString().slice(0, 10)}.csv`);
}

// ── Detail dialog ───────────────────────────────────────────────

function openDetail(fault) {
  const history = state.history[fault.i] || [];
  const body = $('#detail-body');

  const rows = [
    ['Work order', fault.workOrder],
    ['Network', fault.source === 'clean' ? 'Clean water' : 'Waste water'],
    ['Problem', fault.journey],
    ['Work type', fault.workType],
    ['Priority', fault.priority && fault.priority !== 'N/A' ? fault.priority : null],
    ['Raised', formatDate(fault.raised)],
    ['Open for', fault.age === null ? null : `${formatNumber(fault.age)} days`],
    ['Current status', fault.status],
    ['Location', place(fault)],
    ['Postcode', fault.postcode],
  ].filter(([, v]) => v);

  const timeline = history.length
    ? history.map(([day, statusIndex]) => `
        <li><strong>${escape(state.statusDict[statusIndex] || 'Seen on map')}</strong>
        <div class="when">observed ${formatDate(day)}</div></li>`).join('')
    : `<li class="pending"><strong>${escape(fault.status || 'Open')}</strong>
       <div class="when">status history begins once this fault changes</div></li>`;

  body.innerHTML = `
    <h3>${escape(fault.journey || 'Fault')}${fault.street ? ' — ' + escape(titleCase(fault.street)) : ''}</h3>
    <p class="sub">${escape(place(fault))} ${escape(fault.postcode || '')}</p>
    <dl class="kv">${rows.map(([k, v]) => `<dt>${escape(k)}</dt><dd>${escape(v)}</dd>`).join('')}</dl>
    <h2 style="font-size:14px;margin:0 0 10px">What we have seen</h2>
    <ul class="timeline">${timeline}</ul>
    ${fault.lat ? `<p style="margin-top:16px"><a href="https://www.openstreetmap.org/?mlat=${fault.lat}&mlon=${fault.lon}#map=17/${fault.lat}/${fault.lon}" target="_blank" rel="noopener">View location on OpenStreetMap ↗</a></p>` : ''}
  `;
  $('#detail').showModal();
}

// ── Public reports ──────────────────────────────────────────────
//
// Problems the public has reported that have not become work orders. Thames
// Water keeps only a rolling window of these, so a report can vanish from
// their map without ever becoming work — which is the point of keeping them.

const reportState = { all: [], filtered: [], search: '', state: '', sort: { key: 'reported', dir: -1 }, table: null };

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
    return row;
  });
}

const REPORT_COLUMNS = [
  { key: 'reported', label: 'Reported', render: (r) => formatDate(r.reported) },
  { key: 'age', label: 'Age', cls: 'num', render: (r) => `<span class="age age-${ageClass(r.age)}">${formatAge(r.age)}</span>` },
  { key: 'street', label: 'Where', render: (r) => `${escape(titleCase(r.street || ''))} <span class="mono">${escape(r.postcode || '')}</span>` },
  { key: 'town', label: 'Town', render: (r) => escape(titleCase(r.town || '')) },
  {
    key: 'gone',
    label: 'On the map now?',
    render: (r) => (r.gone === null
      ? '<span class="pill clean">Showing</span>'
      : `<span class="pill">Gone ${escape(formatDate(r.gone))}</span>`),
  },
];

function applyReportFilters() {
  const needle = reportState.search.trim().toLowerCase();
  reportState.filtered = reportState.all.filter((r) => {
    if (reportState.state === 'current' && r.gone !== null) return false;
    if (reportState.state === 'gone' && r.gone === null) return false;
    if (needle && !r.haystack.includes(needle)) return false;
    return true;
  });

  const { key, dir } = reportState.sort;
  reportState.filtered.sort((a, b) => {
    const av = a[key];
    const bv = b[key];
    if (av === bv) return 0;
    if (av === null || av === undefined) return 1;
    if (bv === null || bv === undefined) return -1;
    return (av > bv ? 1 : -1) * dir;
  });

  renderReports();
}

function renderReports() {
  if (!reportState.table) {
    reportState.table = new VirtualTable($('#table-reports'), {
      columns: REPORT_COLUMNS,
      rowHeight: ROW_HEIGHT,
      onRowClick: openReportDetail,
      onSort: (key) => {
        if (reportState.sort.key === key) reportState.sort.dir *= -1;
        else reportState.sort = { key, dir: key === 'reported' ? -1 : 1 };
        applyReportFilters();
      },
      emptyMessage: 'No reports match those filters.',
    });
  }
  reportState.table.setRows(reportState.filtered, reportState.sort);
  $('#r-count').textContent =
    `${formatNumber(reportState.filtered.length)} of ${formatNumber(reportState.all.length)} reports`;
}

function openReportDetail(r) {
  const rows = [
    ['Reported', formatDate(r.reported)],
    ['Age', r.age === null ? null : `${formatNumber(r.age)} days`],
    ['Location', titleCase(r.street || '')],
    ['Town', titleCase(r.town || '')],
    ['Postcode', r.postcode],
    ['First recorded here', formatDate(r.firstSeen)],
  ].filter(([, v]) => v);

  const fate = r.gone === null
    ? `<li><strong>Still showing on Thames Water's map</strong>
         <div class="when">as of the last collection</div></li>`
    : `<li><strong>Dropped off Thames Water's map</strong>
         <div class="when">${escape(formatDate(r.gone))}</div></li>`;

  $('#detail-body').innerHTML = `
    <h3>Reported problem${r.street ? ' — ' + escape(titleCase(r.street)) : ''}</h3>
    <p class="sub">${escape(titleCase(r.town || ''))} ${escape(r.postcode || '')}</p>
    <dl class="kv">${rows.map(([k, v]) => `<dt>${escape(k)}</dt><dd>${escape(v)}</dd>`).join('')}</dl>
    <h2 style="font-size:14px;margin:0 0 10px">What we have seen</h2>
    <ul class="timeline">
      <li><strong>Reported to Thames Water</strong><div class="when">${escape(formatDate(r.reported))}</div></li>
      ${fate}
    </ul>
    <p style="margin-top:16px;color:var(--text-dim);font-size:13px">
      This is a public report, not yet a work order. It carries no reference number or repair
      status until Thames Water raises one.
    </p>
    ${r.lat ? `<p style="margin-top:10px"><a href="https://www.openstreetmap.org/?mlat=${r.lat}&mlon=${r.lon}#map=17/${r.lat}/${r.lon}" target="_blank" rel="noopener">View location on OpenStreetMap ↗</a></p>` : ''}
  `;
  $('#detail').showModal();
}

function exportReportsCSV() {
  const header = ['reported', 'age_days', 'street', 'postcode', 'town', 'still_on_map', 'left_map', 'lat', 'lon'];
  const rows = reportState.filtered.map((r) => [
    r.reported === null ? '' : dayToDate(r.reported).toISOString().slice(0, 10),
    r.age ?? '', r.street, r.postcode, r.town,
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
  $('#map-note').textContent =
    `${formatNumber(points.length)} faults and ${formatNumber(reports.length)} reports plotted`;
  setTimeout(() => state.map.invalidateSize(), 0);
}

// ── Places ──────────────────────────────────────────────────────

function renderPlaces() {
  const table = $('#table-places');
  table.innerHTML =
    '<thead><tr><th>Town</th><th>Open faults</th><th>Median age</th><th>Open over a year</th></tr></thead>';
  const body = document.createElement('tbody');
  for (const row of state.summary.places) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${escape(titleCase(row.place))}</td>
      <td class="num">${formatNumber(row.n)}</td>
      <td class="num age age-${ageClass(row.median_age)}">${row.median_age === null ? '—' : formatNumber(row.median_age) + 'd'}</td>
      <td class="num">${formatNumber(row.over_year)}</td>`;
    tr.addEventListener('click', () => {
      state.filters.search = row.place;
      $('#f-search').value = row.place;
      applyFilters();
      show('faults');
    });
    body.append(tr);
  }
  table.append(body);
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

  for (const [id, key] of [['#f-source', 'source'], ['#f-status', 'status'], ['#f-journey', 'journey'], ['#f-age', 'minAge']]) {
    $(id).addEventListener('change', (e) => { state.filters[key] = e.target.value; applyFilters(); });
  }

  $('#f-reset').addEventListener('click', () => {
    state.filters = { search: '', source: '', status: '', journey: '', minAge: '' };
    search.value = '';
    for (const id of ['#f-source', '#f-status', '#f-journey', '#f-age']) $(id).value = '';
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

  $('#detail .dialog-close').addEventListener('click', () => $('#detail').close());
  $('#detail').addEventListener('click', (e) => { if (e.target.id === 'detail') $('#detail').close(); });
}

// ── Boot ────────────────────────────────────────────────────────

async function main() {
  wireUp();
  try {
    const [summary, open, reports] = await Promise.all([
      loadJSON('data/summary.json'),
      loadJSON('data/open.json'),
      loadJSON('data/reports.json').catch(() => null),
    ]);

    state.summary = summary;
    reportState.all = reports ? expandReports(reports) : [];
    reportState.filtered = reportState.all;
    state.epoch = open.epoch;
    state.today = open.today;
    state.faults = expand(open);
    state.history = open.history;
    state.filtered = state.faults;

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

    renderOverview();
    renderOldest();
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

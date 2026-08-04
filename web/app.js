// Thames Water fault tracker — UI.
//
// Reads the compact JSON produced by collector/build_site.py. `open.json` is
// dictionary-encoded columnar data (~20k faults), which we expand once into
// plain objects; everything after that is ordinary array work.

import { areaChart, barChart, flowChart, formatNumber } from './charts.js';

const AGE_CLASSES = [7, 30, 90, 365];
const PAGE_SIZE = 100;

const state = {
  summary: null,
  faults: [],
  history: {},
  epoch: null,
  today: 0,
  view: 'overview',
  filters: { search: '', source: '', status: '', journey: '', minAge: '' },
  sort: { key: 'age', dir: -1 },
  page: 0,
  filtered: [],
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
    ...cards.map((c) => {
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

  state.page = 0;
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
  const table = $('#table-faults');
  const head = document.createElement('thead');
  const hr = document.createElement('tr');
  for (const col of COLUMNS) {
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
    tr.innerHTML = COLUMNS.map((c) => `<td class="${c.cls || ''}">${c.render(fault)}</td>`).join('');
    tr.addEventListener('click', () => openDetail(fault));
    body.append(tr);
  }

  table.replaceChildren(head, body);
  $('#f-count').textContent = `${formatNumber(state.filtered.length)} of ${formatNumber(state.faults.length)} faults`;

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

function exportCSV() {
  const header = ['work_order', 'raised', 'age_days', 'status', 'problem', 'work_type', 'street', 'postcode', 'town', 'network', 'lat', 'lon'];
  const rows = state.filtered.map((x) => [
    x.workOrder, x.raised === null ? '' : dayToDate(x.raised).toISOString().slice(0, 10), x.age ?? '',
    x.status, x.journey, x.workType, x.street, x.postcode, x.city, x.source, x.lat, x.lon,
  ]);
  const csv = [header, ...rows]
    .map((r) => r.map((v) => {
      const s = String(v ?? '');
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    }).join(','))
    .join('\n');

  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = `thames-water-faults-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
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

  state.mapLayer = L.layerGroup(markers).addTo(state.map);
  $('#map-note').textContent = `${formatNumber(points.length)} faults plotted`;
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
  $('#detail .dialog-close').addEventListener('click', () => $('#detail').close());
  $('#detail').addEventListener('click', (e) => { if (e.target.id === 'detail') $('#detail').close(); });
}

// ── Boot ────────────────────────────────────────────────────────

async function main() {
  wireUp();
  try {
    const [summary, open] = await Promise.all([
      loadJSON('data/summary.json'),
      loadJSON('data/open.json'),
    ]);

    state.summary = summary;
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
    applyFilters();

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

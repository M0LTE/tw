// Small hand-rolled SVG charts. No dependencies, no build step, theme-aware
// (all colours come from CSS custom properties so dark mode just works).

const NS = 'http://www.w3.org/2000/svg';

function el(name, attrs = {}, text) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  }
  if (text !== undefined) node.textContent = text;
  return node;
}

function niceTicks(min, max, count = 5) {
  if (min === max) return [min];
  const raw = (max - min) / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm >= 7.5 ? 10 : norm >= 3.5 ? 5 : norm >= 1.5 ? 2 : 1) * mag;
  const ticks = [];
  for (let t = Math.ceil(min / step) * step; t <= max + 1e-9; t += step) ticks.push(t);
  return ticks;
}

// Keeps the first and last date labels from being clipped at the plot edges.
function anchorFor(index, count) {
  if (index === 0) return 'start';
  if (index === count - 1) return 'end';
  return 'middle';
}

// Observations are minutes to months apart, so the label granularity follows
// the span being shown rather than being fixed.
export function formatMoment(epochSeconds, spanSeconds) {
  const d = new Date(epochSeconds * 1000);
  if (spanSeconds > 5 * 86400) return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
  return d.toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

export function formatNumber(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '–';
  return n.toLocaleString('en-GB', { maximumFractionDigits: Math.abs(n) < 10 ? 1 : 0 });
}

// `emptyState` keeps early days honest: with one snapshot there is no trend to draw.
function emptyState(container, message) {
  container.replaceChildren();
  const div = document.createElement('div');
  div.className = 'chart-empty';
  div.textContent = message;
  container.append(div);
}

/**
 * Stacked area / line chart over a day axis.
 * series: [{ key, label, colour, values: [{x, y}] }]
 */
export function areaChart(container, series, { stacked = true, yLabel = '', minPoints = 2, zeroBased = true } = {}) {
  const points = Math.max(...series.map((s) => s.values.length), 0);
  if (points < minPoints) {
    emptyState(container, 'Not enough history yet — this chart fills in as daily snapshots accumulate.');
    return;
  }

  const W = 900;
  const H = 260;
  const pad = { top: 12, right: 12, bottom: 28, left: 52 };

  const xs = series[0].values.map((v) => v.x);
  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);

  // Pre-compute the stack so both the axis range and the paths agree.
  const stackTops = series.map(() => new Array(xs.length).fill(0));
  const running = new Array(xs.length).fill(0);
  series.forEach((s, si) => {
    s.values.forEach((v, i) => {
      running[i] = stacked ? running[i] + (v.y || 0) : v.y || 0;
      stackTops[si][i] = running[i];
    });
  });
  // A backlog moving 1% is invisible against a zero baseline, so a level chart
  // fits the axis to its data. Only safe because these are lines: a truncated
  // baseline under stacked fills would misrepresent the areas.
  const flat = stackTops.flat();
  const dataMax = Math.max(...flat);
  const dataMin = Math.min(...flat);
  const padding = Math.max(1, (dataMax - dataMin) * 0.15);
  const yMax = zeroBased ? Math.max(1, dataMax) : dataMax + padding;
  const yMin = zeroBased ? 0 : Math.max(0, dataMin - padding);

  const x = (v) => pad.left + ((v - xMin) / Math.max(1, xMax - xMin)) * (W - pad.left - pad.right);
  const y = (v) => H - pad.bottom - ((v - yMin) / Math.max(1, yMax - yMin)) * (H - pad.top - pad.bottom);

  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart' });

  for (const tick of niceTicks(yMin, yMax)) {
    svg.append(el('line', { x1: pad.left, x2: W - pad.right, y1: y(tick), y2: y(tick), class: 'grid' }));
    svg.append(el('text', { x: pad.left - 8, y: y(tick) + 4, class: 'axis', 'text-anchor': 'end' }, formatNumber(tick)));
  }

  const xTickCount = Math.min(6, xs.length);
  for (let i = 0; i < xTickCount; i++) {
    const v = xMin + Math.round((i * (xMax - xMin)) / Math.max(1, xTickCount - 1));
    const label = formatMoment(v, xMax - xMin);
    svg.append(el('text', { x: x(v), y: H - 8, class: 'axis', 'text-anchor': anchorFor(i, xTickCount) }, label));
  }

  series.forEach((s, si) => {
    const below = si === 0 || !stacked ? xs.map(() => 0) : stackTops[si - 1];
    const top = stackTops[si];
    if (stacked) {
      const d = [
        ...xs.map((xv, i) => `${i ? 'L' : 'M'}${x(xv)},${y(top[i])}`),
        ...xs.map((xv, i) => `L${x(xs[xs.length - 1 - i])},${y(below[xs.length - 1 - i])}`),
        'Z',
      ].join(' ');
      svg.append(el('path', { d, fill: s.colour, 'fill-opacity': 0.75, stroke: 'none' }));
    }
    const line = xs.map((xv, i) => `${i ? 'L' : 'M'}${x(xv)},${y(top[i])}`).join(' ');
    svg.append(el('path', { d: line, fill: 'none', stroke: s.colour, 'stroke-width': stacked ? 1.5 : 2.5 }));
  });

  container.replaceChildren(svg);
  if (yLabel) container.setAttribute('aria-label', yLabel);
}

/** Vertical or horizontal bars. items: [{ label, value, colour }] */
export function barChart(container, items, { horizontal = false, valueFormat = formatNumber, max } = {}) {
  if (!items.length) {
    emptyState(container, 'No data.');
    return;
  }
  const ceiling = max ?? Math.max(...items.map((i) => i.value), 1);
  const wrap = document.createElement('div');
  wrap.className = horizontal ? 'bars bars-h' : 'bars bars-v';

  for (const item of items) {
    const row = document.createElement('div');
    row.className = 'bar-row';

    const label = document.createElement('span');
    label.className = 'bar-label';
    label.textContent = item.label;

    const track = document.createElement('span');
    track.className = 'bar-track';
    const fill = document.createElement('span');
    fill.className = 'bar-fill';
    const pct = (item.value / ceiling) * 100;
    if (horizontal) fill.style.width = `${pct}%`;
    else fill.style.height = `${Math.max(pct, item.value > 0 ? 1.5 : 0)}%`;
    if (item.colour) fill.style.background = item.colour;
    track.append(fill);

    const value = document.createElement('span');
    value.className = 'bar-value';
    value.textContent = valueFormat(item.value);

    row.append(horizontal ? label : track, horizontal ? track : label, value);
    row.title = `${item.label}: ${valueFormat(item.value)}`;
    wrap.append(row);
  }
  container.replaceChildren(wrap);
}

/** Two-series overlaid bars, for "raised vs resolved" per day. */
export function flowChart(container, rows) {
  if (rows.length < 2) {
    emptyState(container, 'Needs at least two snapshots — come back tomorrow.');
    return;
  }
  const W = 900;
  const H = 220;
  const pad = { top: 12, right: 12, bottom: 28, left: 52 };
  const yMax = Math.max(1, ...rows.map((r) => Math.max(r.raised, r.resolved)));
  const bw = (W - pad.left - pad.right) / rows.length;
  const y = (v) => H - pad.bottom - (v / yMax) * (H - pad.top - pad.bottom);

  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart' });
  for (const tick of niceTicks(0, yMax)) {
    svg.append(el('line', { x1: pad.left, x2: W - pad.right, y1: y(tick), y2: y(tick), class: 'grid' }));
    svg.append(el('text', { x: pad.left - 8, y: y(tick) + 4, class: 'axis', 'text-anchor': 'end' }, formatNumber(tick)));
  }

  rows.forEach((r, i) => {
    const x0 = pad.left + i * bw;
    const w = Math.max(1, bw * 0.38);
    svg.append(el('rect', { x: x0 + bw * 0.06, y: y(r.raised), width: w, height: y(0) - y(r.raised), fill: 'var(--c-raised)' }));
    svg.append(el('rect', { x: x0 + bw * 0.52, y: y(r.resolved), width: w, height: y(0) - y(r.resolved), fill: 'var(--c-resolved)' }));
    // Invisible full-height hit area so the whole column shows a tooltip.
    const hit = el('rect', { x: x0, y: pad.top, width: bw, height: H - pad.top - pad.bottom, fill: 'transparent' });
    hit.append(el('title', {}, `${new Date(r.t * 1000).toLocaleString('en-GB')}: ${r.raised} new, ${r.resolved} cleared`));
    svg.append(hit);
  });

  const ticks = Math.min(6, rows.length);
  for (let i = 0; i < ticks; i++) {
    const idx = Math.round((i * (rows.length - 1)) / Math.max(1, ticks - 1));
    const span = rows[rows.length - 1].t - rows[0].t;
    svg.append(
      el('text', { x: pad.left + idx * bw + bw / 2, y: H - 8, class: 'axis', 'text-anchor': anchorFor(i, ticks) },
        formatMoment(rows[idx].t, span)),
    );
  }
  container.replaceChildren(svg);
}

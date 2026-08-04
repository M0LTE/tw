// Virtualised table: one continuous scroll over any number of rows, with only
// the visible slice in the DOM.
//
// Putting all ~20,000 faults in the document costs about nine seconds and
// 165,000 DOM nodes, which is why the tables used to be paged. Windowing keeps
// the row count in the DOM to a few dozen no matter how long the list is, so
// the whole dataset scrolls as one list and stays responsive.

const OVERSCAN = 8;

export class VirtualTable {
  /**
   * @param {HTMLElement} host      scroll container (must have a bounded height)
   * @param {object}   options
   * @param {Array}    options.columns    [{ key, label, cls, render, sortable }]
   * @param {number}   options.rowHeight  must match the CSS row height
   * @param {Function} options.onRowClick
   * @param {Function} options.onSort     called with a column key
   * @param {string}   options.emptyMessage
   */
  constructor(host, options) {
    this.host = host;
    this.columns = options.columns;
    this.rowHeight = options.rowHeight ?? 33;
    this.onRowClick = options.onRowClick;
    this.onSort = options.onSort;
    this.emptyMessage = options.emptyMessage ?? 'Nothing matches those filters.';

    this.rows = [];
    this.sort = null;
    this.painted = null;

    this.table = document.createElement('table');
    this.thead = document.createElement('thead');
    this.tbody = document.createElement('tbody');
    this.table.append(this.thead, this.tbody);
    host.classList.add('virtual');
    host.replaceChildren(this.table);

    host.addEventListener('scroll', () => this.paint(), { passive: true });
    // The viewport height decides how many rows we need, so re-window when it
    // changes — including when the tab this lives in is first shown.
    new ResizeObserver(() => this.paint()).observe(host);

    this.renderHeader();
  }

  renderHeader() {
    const tr = document.createElement('tr');
    for (const col of this.columns) {
      const th = document.createElement('th');
      th.textContent = col.label;
      if (col.cls) th.className = col.cls;
      if (col.sortable !== false && this.onSort) {
        th.dataset.sort = col.key;
        if (this.sort && this.sort.key === col.key) {
          const arrow = document.createElement('span');
          arrow.className = 'arrow';
          arrow.textContent = this.sort.dir === 1 ? ' ▲' : ' ▼';
          th.append(arrow);
        }
        th.addEventListener('click', () => this.onSort(col.key));
      }
      tr.append(th);
    }
    this.thead.replaceChildren(tr);
  }

  /** Replace the data and jump back to the top. */
  setRows(rows, sort) {
    this.rows = rows;
    this.sort = sort ?? this.sort;
    this.painted = null;
    this.renderHeader();
    this.host.scrollTop = 0;
    this.paint();
  }

  spacer(height) {
    const tr = document.createElement('tr');
    tr.className = 'spacer';
    const td = document.createElement('td');
    td.colSpan = this.columns.length;
    td.style.height = `${height}px`;
    tr.append(td);
    return tr;
  }

  paint() {
    if (!this.rows.length) {
      if (this.painted === 'empty') return;
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = this.columns.length;
      td.className = 'table-empty';
      td.textContent = this.emptyMessage;
      tr.append(td);
      this.tbody.replaceChildren(tr);
      this.painted = 'empty';
      return;
    }

    const visible = Math.ceil(this.host.clientHeight / this.rowHeight) || 20;
    const start = Math.max(0, Math.floor(this.host.scrollTop / this.rowHeight) - OVERSCAN);
    const end = Math.min(this.rows.length, start + visible + OVERSCAN * 2);

    const key = `${start}:${end}:${this.rows.length}`;
    if (this.painted === key) return;
    this.painted = key;

    const frag = document.createDocumentFragment();
    frag.append(this.spacer(start * this.rowHeight));
    for (let i = start; i < end; i++) {
      const row = this.rows[i];
      const tr = document.createElement('tr');
      tr.innerHTML = this.columns
        .map((c) => `<td class="${c.cls || ''}">${c.render(row)}</td>`)
        .join('');
      // Long values are clipped to keep every row exactly one line tall; the
      // tooltip and the detail dialog still carry the full text.
      for (const [i2, cell] of [...tr.cells].entries()) {
        const text = cell.textContent.trim();
        if (text) cell.title = `${this.columns[i2].label}: ${text}`;
      }
      if (this.onRowClick) tr.addEventListener('click', () => this.onRowClick(row));
      frag.append(tr);
    }
    frag.append(this.spacer((this.rows.length - end) * this.rowHeight));
    this.tbody.replaceChildren(frag);
  }
}

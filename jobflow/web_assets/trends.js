'use strict';

// Discovery history is a cumulative catalog series, never an open-role census.
// UI selections stay local; this module never changes the supplied workspace.
(function (global) {
  const DAY = 86400000;
  const palette = ['#6bdac6', '#8baeff', '#e7ba73', '#c49afa', '#f08fa5', '#76c8ec', '#a4ce77', '#eca078', '#81b8a5', '#d6a2cd', '#a6b8d9', '#dbcd78', '#84d5dc', '#b5a3f3', '#d8a98d', '#8dccb3', '#eca9be', '#b4c875', '#94b6eb', '#e5c9a9'];
  const preferences = {limit: 10, range: '30', selected: null, hidden: new Set(), query: ''};
  let dataset = {rows: [], companies: [], end: 0};
  let surface = null;
  let removeListeners = [];
  let observer = null;
  let model = null;
  let activeDate = null;
  let renderWidth = 760;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const fmt = value => Number(value || 0).toLocaleString('en-US');
  const dateNumber = day => /^\d{4}-\d{2}-\d{2}$/.test(String(day)) ? Date.parse(day + 'T00:00:00Z') : NaN;
  const dateString = value => new Date(value).toISOString().slice(0, 10);
  const dateLabel = (value, detailed = false) => new Intl.DateTimeFormat('en-US', {timeZone: 'UTC', month: 'short', day: 'numeric', ...(detailed ? {year: 'numeric'} : {})}).format(new Date(value));
  const count = value => Number.isFinite(Number(value)) ? Math.max(0, Math.floor(Number(value))) : 0;
  const icon = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M3 5h14M6 10h8M8 15h4"/></svg>';

  function normalize(data) {
    const dates = new Map();
    for (const row of data.history || []) {
      const at = dateNumber(row.date);
      if (!Number.isFinite(at)) continue;
      const companies = Object.fromEntries(Object.entries(row.companies || {}).map(([name, value]) => [name, count(value)]));
      dates.set(at, {at, companies});
    }
    const rows = [...dates.values()].sort((a, b) => a.at - b.at);
    const names = [...new Set(rows.flatMap(row => Object.keys(row.companies)))];
    // The backend sends full cumulative rows. Carry-forward also permits sparse
    // snapshots without turning an absent property into an invented decline.
    let previous = {};
    rows.forEach(row => { row.companies = {...previous, ...row.companies}; previous = row.companies; });
    const latest = rows.at(-1)?.companies || {};
    const companies = names.sort((a, b) => (latest[b] || 0) - (latest[a] || 0) || a.localeCompare(b));
    const today = dateNumber(data.today);
    return {rows, companies, colorIndexes: new Map(companies.map((name, index) => [name, index])), end: rows.length ? Math.max(rows.at(-1).at, Number.isFinite(today) ? today : 0) : 0};
  }

  function chosenCompanies() {
    return preferences.selected === null
      ? dataset.companies.slice(0, preferences.limit === 'all' ? undefined : preferences.limit)
      : dataset.companies.filter(name => preferences.selected.has(name));
  }

  function color(name) {
    // Rank assigns the ten default series distinct curated colors. The mapping
    // stays stable while filtering, searching, and toggling this dataset.
    const index = dataset.colorIndexes?.get(name) ?? 0;
    if (index < palette.length) return `var(--trend-series-${index}, ${palette[Math.max(0, index)]})`;
    return `hsl(${Math.round((index * 137.508 + 160) % 360)} 63% var(--trend-series-lightness, 70%))`;
  }

  function valuesAt(at) {
    let left = 0, right = dataset.rows.length - 1, match = -1;
    while (left <= right) {
      const middle = (left + right) >> 1;
      if (dataset.rows[middle].at <= at) { match = middle; left = middle + 1; }
      else right = middle - 1;
    }
    return match < 0 ? {} : dataset.rows[match].companies;
  }

  function niceStep(max) {
    if (max <= 4) return 1;
    const raw = max / 5;
    const scale = 10 ** Math.floor(Math.log10(raw));
    const candidates = [...new Set([1, 2, 2.5, 5, 10].map(value => Math.max(1, Math.ceil(value * scale))))];
    // Prefer about five intervals, with a small tie-break toward less empty
    // headroom. Counts always use integer ticks, including very small series.
    const cost = step => Math.abs(Math.ceil(max / step) - 5) + (Math.ceil(max / step) * step / max - 1) * .1;
    return candidates.reduce((best, step) => cost(step) < cost(best) ? step : best);
  }

  function chartModel(width = renderWidth) {
    if (!dataset.rows.length) return null;
    const selected = chosenCompanies();
    const companies = selected.filter(name => !preferences.hidden.has(name));
    const last = dataset.end;
    const first = preferences.range === 'all' ? dataset.rows[0].at : Math.max(dataset.rows[0].at, last - (Number(preferences.range) - 1) * DAY);
    const rows = [{at: first, companies: valuesAt(first)}, ...dataset.rows.filter(row => row.at > first && row.at <= last)];
    if (rows.at(-1).at !== last) rows.push({at: last, companies: valuesAt(last)});
    const latest = valuesAt(last), baseline = valuesAt(first - DAY);
    const total = companies.reduce((sum, name) => sum + (latest[name] || 0), 0);
    const increase = companies.reduce((sum, name) => sum + (latest[name] || 0) - (baseline[name] || 0), 0);
    const maximum = rows.reduce((max, row) => companies.reduce((m, name) => Math.max(m, row.companies[name] || 0), max), 0);
    const step = niceStep(maximum), topValue = Math.max(step, Math.ceil(maximum / step) * step);
    const w = Math.max(280, width), h = 280, left = topValue >= 10000 ? 60 : 49, right = 17, top = 24, bottom = 34;
    const x = at => first === last ? left + (w - left - right) / 2 : left + (at - first) / (last - first) * (w - left - right);
    const y = value => h - bottom - value / topValue * (h - top - bottom);
    return {selected, companies, first, last, rows, total, increase, latest, baseline, step, topValue, w, h, left, right, top, bottom, x, y};
  }

  function renderPickerList() {
    const selected = new Set(chosenCompanies());
    const matching = dataset.companies.filter(name => name.toLocaleLowerCase().includes(preferences.query.trim().toLocaleLowerCase()));
    const latest = dataset.rows.at(-1)?.companies || {};
    return matching.length ? matching.map(name => `<label class="jf-trend-option"><input type="checkbox" data-trend-company="${esc(name)}" ${selected.has(name) ? 'checked' : ''}><span class="jf-trend-dot" style="--series:${color(name)}"></span><span>${esc(name)}</span><small>${fmt(latest[name])}</small></label>`).join('') : '<p class="jf-trend-no-results">No companies match your search.</p>';
  }

  function plotMarkup(m) {
    if (!m) return '<div class="jf-trend-empty"><strong>Your discovery history starts here</strong><span>Companies appear after roles with discovery dates are recorded.</span></div>';
    if (!m.selected.length) return '<div class="jf-trend-empty"><strong>Choose companies to compare</strong><span>Use Company filter or a Top 10, Top 20, or All preset.</span></div>';
    if (!m.companies.length) return '<div class="jf-trend-empty"><strong>All selected series are hidden</strong><span>Click a company in the legend to show its history.</span></div>';
    const ticks = Array.from({length: Math.round(m.topValue / m.step) + 1}, (_, i) => i * m.step);
    const tickCount = m.first === m.last ? 1 : Math.min(5, Math.floor((m.w - m.left - m.right) / 90) + 1, Math.round((m.last - m.first) / DAY) + 1);
    const dates = [...new Set(Array.from({length: tickCount}, (_, i) => m.first + Math.round((m.last - m.first) / DAY * (tickCount === 1 ? 0 : i / (tickCount - 1))) * DAY))];
    const paths = m.companies.map(name => {
      const path = m.rows.map((row, index) => `${index ? 'H' + m.x(row.at).toFixed(2) + 'V' : 'M' + m.x(row.at).toFixed(2) + ','}${m.y(row.companies[name] || 0).toFixed(2)}`).join('');
      return `<g class="jf-trend-series" data-trend-series="${esc(name)}"><path d="${path}" fill="none" stroke="${color(name)}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/><circle cx="${m.x(m.last)}" cy="${m.y(m.latest[name] || 0)}" r="3" fill="${color(name)}" stroke="var(--trend-bg, #101a21)" stroke-width="1.5"/><title>${esc(name)}: ${fmt(m.latest[name])} cumulative discoveries</title></g>`;
    }).join('');
    return `<svg class="jf-trend-svg" viewBox="0 0 ${m.w} ${m.h}" preserveAspectRatio="none" role="img" aria-label="Cumulative discoveries by company from ${dateLabel(m.first, true)} to ${dateLabel(m.last, true)}. Values are also available in the company legend.">
      ${ticks.map(value => `<line class="jf-trend-grid" x1="${m.left}" x2="${m.w - m.right}" y1="${m.y(value)}" y2="${m.y(value)}"/><text class="jf-trend-axis" x="${m.left - 10}" y="${m.y(value) + 4}" text-anchor="end">${fmt(value)}</text>`).join('')}
      ${dates.map(at => `<line class="jf-trend-grid jf-trend-grid-vertical" x1="${m.x(at)}" x2="${m.x(at)}" y1="${m.top}" y2="${m.h - m.bottom}"/><text class="jf-trend-axis" x="${m.x(at)}" y="${m.h - 10}" text-anchor="${at === m.first && dates.length > 1 ? 'start' : at === m.last && dates.length > 1 ? 'end' : 'middle'}">${dateLabel(at)}</text>`).join('')}
      ${paths}<g class="jf-trend-cursor" hidden><line class="jf-trend-crosshair" y1="${m.top}" y2="${m.h - m.bottom}"/><g class="jf-trend-cursor-dots"></g></g>
    </svg><div class="jf-trend-interaction" tabindex="0" role="slider" aria-label="Inspect discovery date; use left and right arrow keys" aria-valuemin="0" aria-valuemax="${Math.round((m.last - m.first) / DAY)}" aria-valuenow="${Math.round((m.last - m.first) / DAY)}" aria-valuetext="${dateLabel(m.last, true)}" style="left:${m.left / m.w * 100}%;right:${m.right / m.w * 100}%;top:${m.top}px;bottom:${m.bottom}px"></div><div class="jf-trend-tooltip" role="status" hidden></div>`;
  }

  function legendMarkup(m) {
    if (!m?.selected.length) return '';
    return m.selected.map(name => `<button type="button" class="jf-trend-legend-item ${preferences.hidden.has(name) ? 'is-hidden' : ''}" data-trend-toggle="${esc(name)}" aria-pressed="${!preferences.hidden.has(name)}" title="${preferences.hidden.has(name) ? 'Show' : 'Hide'} ${esc(name)}"><span class="jf-trend-dot" style="--series:${color(name)}"></span><span class="jf-trend-company">${esc(name)}</span><span class="jf-trend-value">${fmt(m.latest[name])}</span></button>`).join('');
  }

  function summaryMarkup(m) {
    if (!m) return '<span class="jf-trend-summary-label">Cumulative discoveries</span><strong>—</strong>';
    return `<div><span class="jf-trend-summary-label">Cumulative discoveries</span><strong>${fmt(m.total)}<small>roles</small></strong></div><div class="jf-trend-summary-meta"><span>${fmt(m.companies.length)} of ${fmt(dataset.companies.length)} companies shown</span><span><b>${m.increase > 0 ? '+' : ''}${fmt(m.increase)}</b> discovered in range</span></div>`;
  }

  function render(data) {
    dataset = normalize(data || {});
    model = chartModel();
    return `<section class="jf-trend" aria-label="Company discovery trends">
      <div class="jf-trend-summary">${summaryMarkup(model)}</div>
      <div class="jf-trend-toolbar"><div class="jf-trend-presets" role="group" aria-label="Company count">${[10, 20, 'all'].map(limit => `<button type="button" data-trend-limit="${limit}" aria-pressed="${preferences.selected === null && preferences.limit === limit}">${limit === 'all' ? 'All' : 'Top ' + limit}</button>`).join('')}</div>
        <details class="jf-trend-picker"><summary>${icon}<span>Company filter</span><span class="jf-trend-selection-count">${fmt(chosenCompanies().length)}</span><span class="jf-trend-chevron" aria-hidden="true">⌄</span></summary><div class="jf-trend-picker-panel"><input class="jf-trend-search" type="search" placeholder="Search all companies…" aria-label="Search all companies" value="${esc(preferences.query)}"><div class="jf-trend-picker-actions"><button type="button" data-trend-action="add-matches">Select matches</button><button type="button" data-trend-action="clear">Clear</button></div><div class="jf-trend-picker-list">${renderPickerList()}</div><div class="jf-trend-picker-footer"><span>Counts are cumulative discoveries</span><button type="button" data-trend-action="done">Done</button></div></div></details>
        <label class="jf-trend-range"><span class="jf-trend-sr-only">Discovery date range</span><select aria-label="Discovery date range" data-trend-range>${[['7','7 days'],['30','30 days'],['90','90 days'],['all','All time']].map(([value, label]) => `<option value="${value}" ${preferences.range === value ? 'selected' : ''}>${label}</option>`).join('')}</select></label>
      </div>
      <div class="jf-trend-plot">${plotMarkup(model)}</div>
      <div class="jf-trend-legend" aria-label="Toggle company series">${legendMarkup(model)}</div>
      <div class="jf-trend-footer"><span>Cumulative discoveries · not historical open positions</span><span class="jf-trend-period">${model ? dateLabel(model.first) + ' – ' + dateLabel(model.last, true) : ''}</span></div>
    </section>`;
  }

  function repaint({picker = false} = {}) {
    if (!surface) return;
    model = chartModel();
    surface.querySelector('.jf-trend-summary').innerHTML = summaryMarkup(model);
    surface.querySelector('.jf-trend-plot').innerHTML = plotMarkup(model);
    surface.querySelector('.jf-trend-legend').innerHTML = legendMarkup(model);
    surface.querySelector('.jf-trend-selection-count').textContent = fmt(chosenCompanies().length);
    surface.querySelector('.jf-trend-period').textContent = model ? dateLabel(model.first) + ' – ' + dateLabel(model.last, true) : '';
    surface.querySelectorAll('[data-trend-limit]').forEach(button => button.setAttribute('aria-pressed', String(preferences.selected === null && String(preferences.limit) === button.dataset.trendLimit)));
    if (picker) surface.querySelector('.jf-trend-picker-list').innerHTML = renderPickerList();
    else {
      const chosen = new Set(chosenCompanies());
      surface.querySelectorAll('[data-trend-company]').forEach(input => { input.checked = chosen.has(input.dataset.trendCompany); });
    }
    activeDate = null;
  }

  function listen(target, type, handler, options) {
    target.addEventListener(type, handler, options);
    removeListeners.push(() => target.removeEventListener(type, handler, options));
  }

  function inspectDate(at) {
    if (!model || !surface || !model.companies.length) return;
    activeDate = Math.max(model.first, Math.min(model.last, at));
    const cursor = surface.querySelector('.jf-trend-cursor');
    const tooltip = surface.querySelector('.jf-trend-tooltip');
    if (!cursor || !tooltip) return;
    const x = model.x(activeDate), values = valuesAt(activeDate);
    cursor.removeAttribute('hidden');
    cursor.querySelector('line').setAttribute('x1', x);
    cursor.querySelector('line').setAttribute('x2', x);
    cursor.querySelector('.jf-trend-cursor-dots').innerHTML = model.companies.map(name => `<circle cx="${x}" cy="${model.y(values[name] || 0)}" r="3.5" fill="${color(name)}" stroke="var(--trend-bg, #101a21)" stroke-width="1.5"/>`).join('');
    const ordered = [...model.companies].sort((a, b) => (values[b] || 0) - (values[a] || 0) || a.localeCompare(b));
    tooltip.hidden = false;
    tooltip.innerHTML = `<div class="jf-trend-tooltip-date">${dateLabel(activeDate, true)}<small>Cumulative discoveries</small></div><div class="jf-trend-tooltip-values">${ordered.map(name => `<div><span class="jf-trend-dot" style="--series:${color(name)}"></span><span>${esc(name)}</span><b>${fmt(values[name])}</b></div>`).join('')}</div>`;
    // Place the inspection panel on the opposite side of the crosshair.
    tooltip.style.left = x > model.w * .52 ? '8px' : 'auto';
    tooltip.style.right = x > model.w * .52 ? 'auto' : '8px';
    const interaction = surface.querySelector('.jf-trend-interaction');
    interaction.setAttribute('aria-valuenow', String(Math.round((activeDate - model.first) / DAY)));
    interaction.setAttribute('aria-valuetext', dateLabel(activeDate, true) + ': ' + ordered.map(name => `${name}, ${fmt(values[name])}`).join('; '));
  }

  function hideInspection() {
    surface?.querySelector('.jf-trend-cursor')?.setAttribute('hidden', '');
    const tooltip = surface?.querySelector('.jf-trend-tooltip');
    if (tooltip) tooltip.hidden = true;
  }

  function mount(root) {
    destroy();
    surface = root?.matches?.('.jf-trend') ? root : root?.querySelector?.('.jf-trend');
    if (!surface) return;
    const measure = () => {
      const width = Math.round(surface.querySelector('.jf-trend-plot').getBoundingClientRect().width);
      if (width > 0 && width !== renderWidth) {renderWidth = width; repaint();}
    };
    measure();
    if (typeof global.ResizeObserver === 'function') {
      observer = new global.ResizeObserver(measure);
      observer.observe(surface);
    } else listen(global, 'resize', measure);
    listen(surface, 'click', event => {
      const limit = event.target.closest('[data-trend-limit]');
      const toggle = event.target.closest('[data-trend-toggle]');
      const action = event.target.closest('[data-trend-action]');
      if (limit) {
        preferences.limit = limit.dataset.trendLimit === 'all' ? 'all' : Number(limit.dataset.trendLimit);
        preferences.selected = null; preferences.hidden.clear(); repaint({picker: true});
      } else if (toggle) {
        const name = toggle.dataset.trendToggle;
        if (preferences.hidden.has(name)) preferences.hidden.delete(name); else preferences.hidden.add(name);
        repaint();
        [...surface.querySelectorAll('[data-trend-toggle]')].find(button => button.dataset.trendToggle === name)?.focus({preventScroll: true});
      } else if (action) {
        const command = action.dataset.trendAction;
        if (command === 'done') {surface.querySelector('.jf-trend-picker').open = false; surface.querySelector('.jf-trend-picker summary').focus(); return;}
        if (command === 'clear') { preferences.selected = new Set(); preferences.hidden.clear(); }
        if (command === 'add-matches') {
          preferences.selected = new Set(chosenCompanies());
          dataset.companies.filter(name => name.toLocaleLowerCase().includes(preferences.query.trim().toLocaleLowerCase())).forEach(name => {preferences.selected.add(name); preferences.hidden.delete(name);});
        }
        repaint({picker: true});
      }
    });
    listen(surface, 'change', event => {
      if (event.target.matches('[data-trend-range]')) { preferences.range = event.target.value; repaint(); }
      if (event.target.matches('[data-trend-company]')) {
        const name = event.target.dataset.trendCompany;
        preferences.selected = new Set(chosenCompanies());
        if (event.target.checked) {preferences.selected.add(name); preferences.hidden.delete(name);} else preferences.selected.delete(name);
        repaint();
      }
    });
    listen(surface, 'input', event => {
      if (event.target.matches('.jf-trend-search')) {
        preferences.query = event.target.value;
        surface.querySelector('.jf-trend-picker-list').innerHTML = renderPickerList();
      }
    });
    listen(surface, 'pointermove', event => {
      const interaction = event.target.closest('.jf-trend-interaction');
      if (!interaction || !model) return;
      const rect = interaction.getBoundingClientRect();
      if (!rect.width) return;
      inspectDate(model.first + Math.round(Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) * (model.last - model.first) / DAY) * DAY);
    });
    listen(surface, 'pointerout', event => {
      if (event.target.closest('.jf-trend-plot') && !surface.querySelector('.jf-trend-plot')?.contains(event.relatedTarget)) hideInspection();
    });
    listen(surface, 'focusin', event => {
      if (event.target.matches('.jf-trend-interaction')) inspectDate(model.last);
    });
    listen(surface, 'focusout', event => {
      if (event.target.matches('.jf-trend-interaction')) hideInspection();
    });
    listen(surface, 'keydown', event => {
      if (event.key === 'Escape') {
        hideInspection();
        const picker = surface.querySelector('.jf-trend-picker');
        if (picker.open) {picker.open = false; picker.querySelector('summary').focus();}
      }
      if (!event.target.matches('.jf-trend-interaction') || !model) return;
      const days = event.shiftKey ? 7 : 1;
      const moves = {ArrowLeft: (activeDate ?? model.last) - DAY * days, ArrowRight: (activeDate ?? model.last) + DAY * days, Home: model.first, End: model.last};
      if (event.key in moves) { event.preventDefault(); inspectDate(moves[event.key]); }
    });
    listen(surface, 'pointerover', event => {
      const button = event.target.closest('[data-trend-toggle]');
      if (!button) return;
      surface.querySelectorAll('[data-trend-series]').forEach(series => series.classList.toggle('is-muted', series.dataset.trendSeries !== button.dataset.trendToggle));
    });
    listen(surface, 'pointerout', event => {
      if (event.target.closest('[data-trend-toggle]') && !event.relatedTarget?.closest?.('[data-trend-toggle]')) surface.querySelectorAll('[data-trend-series]').forEach(series => series.classList.remove('is-muted'));
    });
  }

  function destroy() {
    removeListeners.forEach(remove => remove()); removeListeners = [];
    observer?.disconnect(); observer = null; surface = null; activeDate = null;
  }

  global.JobflowTrends = {render, mount, destroy};
})(window);

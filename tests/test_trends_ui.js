/* Optional UI regressions: node tests/test_trends_ui.js.
 * Executes the shipped presentation module and its actual delegated controls.
 * No build tooling, browser, or third-party runtime package is required.
 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../jobflow/web_assets/trends.js'), 'utf8');

function fixture(size = 24) {
  const companies = Object.fromEntries(Array.from({length: size}, (_, i) => ['Company ' + String(i + 1).padStart(2, '0'), (size - i) * 5]));
  return {today: '2026-11-14', history: [
    {date: '2026-10-28', companies: Object.fromEntries(Object.entries(companies).map(([name, value]) => [name, Math.max(0, value - 2)]))},
    {date: '2026-11-14', companies},
  ]};
}

function harness(data = fixture()) {
  const context = vm.createContext({window: {addEventListener() {}, removeEventListener() {}}});
  vm.runInContext(source, context, {filename: 'trends.js'});
  const api = context.window.JobflowTrends;
  const nodes = new Map();
  const listeners = new Map();
  function node(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {
      innerHTML: '', textContent: '', open: false,
      getBoundingClientRect: () => ({width: 760}),
      focus() {}, querySelector: node,
    });
    return nodes.get(selector);
  }
  const root = {
    matches: selector => selector === '.jf-trend',
    querySelector: node,
    querySelectorAll: () => [],
    addEventListener(type, handler) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(handler);
    },
    removeEventListener(type, handler) {
      listeners.set(type, listeners.get(type).filter(value => value !== handler));
    },
  };
  api.render(data);
  api.mount(root);
  function fire(type, selector, fields = {}) {
    const target = {dataset: {}, closest: match => match === selector ? target : null, matches: match => match === selector, ...fields};
    for (const handler of listeners.get(type) || []) handler({target});
  }
  return {api, nodes, fire, html: () => api.render(data), listeners};
}
const legendCount = html => (html.match(/data-trend-toggle=/g) || []).length;
const seriesCount = html => (html.match(/data-trend-series=/g) || []).length;

{
  const h = harness();
  assert.equal(legendCount(h.html()), 10, 'Default chart must show at least the top ten companies.');
  assert.equal(seriesCount(h.html()), 10);
  assert.match(h.html(), /data-trend-limit="10" aria-pressed="true"/);
  h.fire('click', '[data-trend-limit]', {dataset: {trendLimit: '20'}});
  assert.equal(legendCount(h.html()), 20);
  h.fire('click', '[data-trend-limit]', {dataset: {trendLimit: 'all'}});
  assert.equal(legendCount(h.html()), 24, 'All companies must not retain an arbitrary chart cap.');
  assert.equal(seriesCount(h.html()), 24);
}
{
  const h = harness();
  h.fire('input', '.jf-trend-search', {value: 'company 24'});
  const matches = h.nodes.get('.jf-trend-picker-list').innerHTML;
  assert.match(matches, /Company 24/);
  assert.doesNotMatch(matches, /Company 01/);
  h.fire('change', '[data-trend-company]', {dataset: {trendCompany: 'Company 24'}, checked: true});
  assert.equal(legendCount(h.html()), 11, 'Search can add a company below the top-ten cutoff.');
  assert.match(h.html(), /data-trend-toggle="Company 24"/);
  h.fire('click', '[data-trend-action]', {dataset: {trendAction: 'clear'}});
  assert.equal(legendCount(h.html()), 0);
  assert.match(h.html(), /Choose companies to compare/);
  h.fire('click', '[data-trend-action]', {dataset: {trendAction: 'add-matches'}});
  assert.equal(legendCount(h.html()), 1);
  assert.match(h.html(), /data-trend-toggle="Company 24"/);
}
{
  const h = harness();
  h.fire('click', '[data-trend-toggle]', {dataset: {trendToggle: 'Company 01'}});
  assert.equal(legendCount(h.html()), 10);
  assert.equal(seriesCount(h.html()), 9, 'Legend visibility changes the plot while retaining the selection.');
  assert.match(h.html(), /data-trend-toggle="Company 01" aria-pressed="false"/);
  h.fire('click', '[data-trend-toggle]', {dataset: {trendToggle: 'Company 01'}});
  assert.equal(seriesCount(h.html()), 10);
}
{
  const data = {today: '2026-11-14', history: [
    {date: '2026-11-01', companies: {Alpha: 5}},
    {date: '2026-11-10', companies: {Alpha: 10}},
  ]};
  const original = JSON.stringify(data);
  const h = harness(data);
  h.fire('change', '[data-trend-range]', {value: '7'});
  const html = h.html();
  assert.match(html, /<b>\+5<\/b> discovered in range/, 'Window growth must subtract the carry-in baseline.');
  assert.match(html, /Nov 8 – Nov 14, 2026/);
  assert.match(html, /Nov 14, 2026/, 'The chart carries the final snapshot forward to workspace today.');
  assert.match(html, /<path d="M[^\"]+H[^\"]+V/, 'Discovery counts use steps, not interpolated hiring activity.');
  assert.equal(JSON.stringify(data), original, 'The chart must not alter workspace records.');
}
{
  const h = harness({today: '2026-11-14', history: [
    {date: '2026-11-14', companies: {Alpha: 7, Beta: 2}},
  ]});
  const html = h.html();
  assert.equal(seriesCount(html), 2);
  assert.doesNotMatch(html, /NaN|Infinity/);
  const labels = [...html.matchAll(/class="jf-trend-axis"[^>]*>([^<]+)<\/text>/g)].map(match => match[1]).filter(label => !label.includes('Nov'));
  assert.ok(labels.every(label => /^\d[\d,]*$/.test(label)), 'A count axis must never display fractional counts.');
  assert.match(html, /aria-valuemax="0"/, 'A single date is still keyboard-inspectable.');
}
{
  const h = harness({today: '2026-11-14', history: [{date: '2026-11-14', companies: {Alpha: 204}}]});
  const html = h.html();
  assert.match(html, /text-anchor="end">250<\/text>/, 'The axis uses a sensible ceiling rather than leaving half of the plot empty.');
  assert.doesNotMatch(html, /text-anchor="end">400<\/text>/);
}
{
  const h = harness({today: '2026-11-14', history: []});
  assert.match(h.html(), /Your discovery history starts here/);
  assert.equal(seriesCount(h.html()), 0);
}
{
  const hostile = '<img src=x onerror=alert(1)>';
  const h = harness({today: '2026-11-14', history: [
    {date: 'bad-date', companies: {Bad: 1000}},
    {date: '2026-11-14', companies: {[hostile]: 1, Valid: 2}},
  ]});
  const html = h.html();
  assert.equal(seriesCount(html), 2);
  assert.doesNotMatch(html, /<img src=x/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  h.api.destroy();
  assert.ok([...h.listeners.values()].every(list => list.length === 0), 'Destroy removes delegated listeners before the application rerenders.');
}
console.log('Trend dashboard UI regressions passed.');

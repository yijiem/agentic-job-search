/* Run with node tests/test_insights_ui.js. Exercises application integration
 * without a browser, external map tiles, or changes to workspace records. */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assets = path.join(__dirname, '../jobflow/web_assets');
const appSource = fs.readFileSync(path.join(assets, 'app.js'), 'utf8')
  .split('// Bootstrap browser session.')[0];
const mapSource = fs.readFileSync(path.join(assets, 'maps.js'), 'utf8');

function appContext(data) {
  const nodes = new Map();
  const listeners = new Map();
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {
      innerHTML: '', textContent: '', addEventListener() {}, close() {},
      querySelector() { return null; }, querySelectorAll() { return []; },
      classList: {add() {}, remove() {}, toggle() {}},
    });
    return nodes.get(selector);
  };
  const context = vm.createContext({
    URL, URLSearchParams, Intl, Date, setTimeout, clearTimeout,
    document: {querySelector: node, querySelectorAll: () => [],
      addEventListener(type, callback) {
        if (!listeners.has(type)) listeners.set(type, []);
        listeners.get(type).push(callback);
      }},
    window: {addEventListener() {}, scrollTo() {}},
    location: {hash: '#analytics'},
    fetch: () => { throw new Error('Unexpected network request'); },
  });
  vm.runInContext(mapSource, context, {filename: 'maps.js'});
  vm.runInContext(appSource, context, {filename: 'app.js'});
  context.fixture = data;
  vm.runInContext('setData(fixture); state.page = "analytics";', context);
  return {context, nodes, listeners, evaluate: code => vm.runInContext(code, context)};
}

const job = (id, fields = {}) => ({
  id, company: 'Alpha', title: 'Senior Engineer', location: 'San Francisco, CA',
  eligible: true, score: 90, availability: 'open', first_seen: '2026-11-14T18:00:00Z',
  ...fields,
});
const fixture = () => ({
  today: '2026-11-14', generated_at: '2026-11-14T12:00:00-08:00',
  timezone: 'America/Los_Angeles', settings: {search: {min_score: 40}},
  jobs: [
    job('sf'), job('recheck', {availability: 'missing', score: 70}),
    job('south', {company: 'Beta', location: 'South San Francisco, CA'}),
    job('region', {company: 'Beta', location: 'San Francisco Bay Area'}),
    job('remote', {company: 'Gamma', location: 'United States', remote: true}),
    job('closed', {availability: 'closed'}), job('nonmatch', {eligible: false}),
    job('below-threshold', {score: 10}),
  ],
  applications: [], interviews: [], tasks: [], research: [], history: [],
});
const ids = (app, expression) => JSON.parse(app.evaluate(`JSON.stringify(${expression}.map(job => job.id))`));

{
  const data = fixture(), before = JSON.stringify(data);
  const app = appContext(data);
  assert.deepEqual(ids(app, 'mapJobs()'), ['sf', 'recheck', 'south', 'region', 'remote']);
  app.evaluate('state.mapCompany = "Alpha"; state.mapScore = "80";');
  assert.deepEqual(ids(app, 'mapJobs()'), ['sf'], 'Company and score filters intersect');
  app.evaluate('state.filters = {...state.filters, company:"Gamma", location:"remote", q:"unrelated"};');
  assert.deepEqual(ids(app, 'mapJobs()'), ['sf'], 'Explore-role controls do not silently alter map filters');
  app.evaluate('state.mapScore = "";');
  assert.deepEqual(ids(app, 'mapJobs()'), ['sf', 'recheck'], 'Missing listings remain available for rechecking');
  app.evaluate('state.mapCompany = "No such company";');
  assert.deepEqual(ids(app, 'mapJobs()'), []);
  assert.equal(app.evaluate('jobs() === fixture.jobs'), true);
  assert.equal(JSON.stringify(data), before, 'Map filtering must preserve all recorded jobs and their order');
}

{
  const app = appContext(fixture());
  assert.equal(app.evaluate('locationMatches(jobById("sf"), " san FRANCISCO ")'), true);
  assert.equal(app.evaluate('locationMatches(jobById("south"), "San Francisco")'), false);
  assert.equal(app.evaluate('locationMatches(jobById("region"), "San Francisco")'), false);
  assert.equal(app.evaluate('locationMatches(jobById("south"), "South San Francisco")'), true);
  assert.equal(app.evaluate('locationMatches({location:"San Francisco; South San Francisco"}, "San Francisco")'), true);
  assert.equal(app.evaluate('locationMatches({location:"East Palo Alto"}, "Palo Alto")'), false);
  assert.equal(app.evaluate('locationMatches({location:"California, United States"}, "california")'), true);
  assert.equal(app.evaluate('locationMatches({location:"Remote, US"}, "remote")'), true);
  assert.equal(app.evaluate('locationMatches(jobById("remote"), "remote")'), true);
  assert.equal(app.evaluate('Boolean(locationMatches({location:"London"}, "remote"))'), false);
  assert.equal(app.evaluate('locationMatches({}, "")'), true);
  app.evaluate('state.page = "jobs"; state.filters.location = "San Francisco";');
  assert.deepEqual(ids(app, 'filteredJobs()'), ['sf', 'recheck'], 'City drilldown and map grouping agree');
}

{
  const app = appContext(fixture());
  app.evaluate('state.mapCompany = "Alpha"; state.mapScore = "80"; state.filters.q = "stale query";');
  const click = dataset => app.listeners.get('click').forEach(listener =>
    listener({target: {closest: () => ({dataset})}}));
  click({cityBrowse: 'San Francisco'});
  assert.equal(app.context.location.hash, '#jobs');
  assert.equal(app.evaluate('state.filters.location'), 'San Francisco');
  assert.equal(app.evaluate('state.filters.company'), 'Alpha');
  assert.equal(app.evaluate('state.filters.score'), '80');
  assert.equal(app.evaluate('state.filters.q'), '');
  app.evaluate('state.page = "jobs";');
  assert.deepEqual(ids(app, 'filteredJobs()'), ['sf'], 'Browse roles carries the map company and score filters');
  app.context.location.hash = '#analytics';
  click({companyCity: 'Beta', cityName: 'South San Francisco'});
  assert.deepEqual(ids(app, 'filteredJobs()'), ['south'], 'Company drilldown overrides the company while retaining city and score');
}

{
  const data = fixture();
  data.jobs.push(job('escaped', {company: '<img src=x onerror="bad()"> & Partners'}));
  const before = JSON.stringify(data), app = appContext(data);
  assert.match(app.evaluate('renderCityDetail(mapJobs())'), /Explore a city/);
  app.evaluate('state.city = "San Francisco";');
  const selected = app.evaluate('renderCityDetail(mapJobs())');
  assert.match(selected, /3 matching roles · 2 companies/);
  assert.match(selected, /data-city-browse="San Francisco"/);
  assert.match(selected, /&lt;img src=x onerror=&quot;bad\(\)&quot;&gt; &amp; Partners/);
  assert.ok(!selected.includes('<img'), 'Company data cannot inject markup into city results');
  assert.ok(!selected.includes('Beta'), 'Nearby and regional jobs do not inflate city results');
  app.evaluate('state.mapCompany = "Gamma";');
  const empty = app.evaluate('renderCityDetail(mapJobs())');
  assert.match(empty, /0 matching roles · 0 companies/);
  assert.match(empty, /No matching roles in this city/);
  app.evaluate('state.city = "<script>untrusted city</script>";');
  const invalid = app.evaluate('renderCityDetail(mapJobs())');
  assert.match(invalid, /Explore a city/);
  assert.ok(!invalid.includes('<script>'));
  assert.equal(JSON.stringify(data), before, 'Drilldown rendering must remain read-only');
}

{
  const app = appContext(fixture());
  const events = [], main = app.nodes.get('#main') || {querySelectorAll: () => []};
  app.nodes.set('#main', main);
  let html = '', onSelect;
  Object.defineProperty(main, 'innerHTML', {get: () => html, set(value) {events.push('replace DOM'); html = value;}});
  app.context.window.JobflowTrends = {
    destroy() {events.push('destroy chart');}, render() {return '<div>Chart fixture</div>';},
    mount(root) {assert.equal(root, main); events.push('mount chart');},
  };
  const maps = app.context.window.JobflowMaps;
  app.context.window.JobflowMaps = {...maps,
    destroy() {events.push('destroy map');}, render() {return '<div>Map fixture</div>';},
    mount(root, options) {assert.equal(root, main); onSelect = options.onSelect; events.push('mount map');},
  };
  app.evaluate('render();');
  assert.deepEqual(events, ['destroy chart', 'destroy map', 'replace DOM', 'mount chart', 'mount map']);
  events.length = 0;
  onSelect('San Francisco');
  assert.equal(app.evaluate('state.city'), 'San Francisco');
  assert.match(app.nodes.get('#map-selection').innerHTML, /2 matching roles · 1 company/);
  assert.deepEqual(events, [], 'Selecting a city updates details without resetting map zoom');
  onSelect('');
  assert.match(app.nodes.get('#map-selection').innerHTML, /Explore a city/);
  app.evaluate('state.page = "jobs"; render();');
  assert.deepEqual(events, ['destroy chart', 'destroy map', 'replace DOM', 'mount chart', 'mount map'],
    'Navigating away tears down mounted observers and handlers before removing their DOM');
  app.evaluate('state.page = "overview";');
  onSelect('Oakland');
  assert.equal(app.evaluate('state.city'), 'Oakland');
  assert.equal(app.context.location.hash, '#analytics', 'Overview map selection opens the full explorer');
}

console.log('Insights integration checks passed.');

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
      innerHTML: '', textContent: '', addEventListener() {}, close() {}, setAttribute() {},
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
  url: 'https://example.com/jobs/' + id,
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

// Model the delegated selector boundary, not just its eventual dataset branch.
// A missing [data-*] selector must make the real event handler ignore the click.
function dispatchClick(app, dataset) {
  const attributes = new Set(Object.keys(dataset).map(key =>
    '[data-' + key.replace(/[A-Z]/g, letter => '-' + letter.toLowerCase()) + ']'));
  const element = {dataset};
  const target = {closest(selector) {
    return selector.split(',').some(part => attributes.has(part.trim())) ? element : null;
  }};
  app.listeners.get('click').forEach(listener => listener({target}));
}


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
  const click = dataset => dispatchClick(app, dataset);
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
  assert.match(app.evaluate('renderCityDetail(mapJobs())'), /All locations/);
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
  assert.match(empty, /No matching roles/i);
  app.evaluate('state.city = "<script>untrusted city</script>";');
  const invalid = app.evaluate('renderCityDetail(mapJobs())');
  assert.match(invalid, /All locations/);
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
  app.evaluate('state.mapRoleQuery = "stale search";');
  onSelect('San Francisco');
  assert.equal(app.evaluate('state.city'), 'San Francisco');
  assert.equal(app.evaluate('state.mapRoleQuery'), '', 'A new city starts with its full company results');
  assert.match(app.nodes.get('#map-selection').innerHTML, /2 matching roles · 1 company/);
  assert.deepEqual(events, [], 'Selecting a city updates details without resetting map zoom');
  onSelect('');
  assert.match(app.nodes.get('#map-selection').innerHTML, /All locations/);
  app.evaluate('state.page = "jobs"; render();');
  assert.deepEqual(events, ['destroy chart', 'destroy map', 'replace DOM', 'mount chart', 'mount map'],
    'Navigating away tears down mounted observers and handlers before removing their DOM');
  app.evaluate('state.page = "overview";');
  onSelect('Oakland');
  assert.equal(app.evaluate('state.city'), 'Oakland');
  assert.equal(app.context.location.hash, '#analytics', 'Overview map selection opens the full explorer');
}


// Company drilldown preserves the employer's actual role/location text; a city
// grouping must never be turned into an invented office address or coordinate.
{
  const data = fixture();
  data.jobs = [
    job('multi', {company: 'Alpha', title: 'Staff Storage Engineer', score: 80,
      location: 'San Francisco, CA; Mountain View, CA; Remote, US'}),
    job('other-alpha', {company: 'Alpha', title: 'Senior Compute Engineer', score: 95,
      location: 'San Francisco, CA'}),
    job('beta', {company: 'Beta', title: 'Platform Engineer', score: 85,
      location: 'Remote-Friendly (Travel Required) | San Francisco, CA'}),
    job('south', {company: 'South Only', location: 'South San Francisco, CA'}),
  ];
  data.jobs.push({...data.jobs[0]});
  const before = JSON.stringify(data), app = appContext(data);
  const groups = () => JSON.parse(app.evaluate('JSON.stringify(mapRoleGroups(mapJobs()))'));
  let grouped = groups();
  assert.equal(grouped[0].company, 'Alpha', 'Groups rank by distinct role count');
  assert.deepEqual(grouped[0].roles.map(role => role.id), ['other-alpha', 'multi'],
    'Repeated IDs are counted once and roles rank by match score');
  assert.equal(grouped.reduce((sum, group) => sum + group.roles.length, 0), 4);
  const all = app.evaluate('renderCityDetail(mapJobs())');
  assert.match(all, /All locations/);
  assert.match(all, /Staff Storage Engineer/);
  assert.match(all, /San Francisco, CA; Mountain View, CA; Remote, US/);
  assert.match(all, /Remote-Friendly \(Travel Required\) \| San Francisco, CA/);
  assert.match(all, /data-job="multi"/);
  assert.match(all, /href="https:\/\/example\.com\/jobs\/multi"/);
  assert.ok(!/data-(?:lat|lng|latitude|longitude)=/.test(all),
    'Recorded city text does not establish exact map coordinates');
  app.evaluate('state.city = "San Francisco";');
  const selected = app.evaluate('renderCityDetail(mapJobs())');
  assert.ok(!selected.includes('South Only'));
  assert.match(selected, /San Francisco, CA; Mountain View, CA; Remote, US/,
    'Selecting one city must not discard other employer-stated locations');
  app.evaluate('state.mapRoleQuery = "storage";');
  grouped = groups();
  assert.deepEqual(grouped.map(group => group.company), ['Alpha']);
  assert.deepEqual(grouped[0].roles.map(role => role.id), ['multi']);
  app.evaluate('state.mapRoleQuery = "remote-friendly";');
  assert.deepEqual(groups().map(group => group.company), ['Beta'], 'Search includes full location text');
  app.evaluate('state.mapRoleQuery = "ALPHA";');
  assert.equal(groups()[0].roles.length, 2, 'Company search is case-insensitive');
  app.evaluate('state.mapRoleQuery = ""; state.mapCompany = "Alpha"; state.mapScore = "90";');
  assert.deepEqual(groups()[0].roles.map(role => role.id), ['other-alpha'],
    'Grouped results use the same map company/score filters');
  app.evaluate('state.mapRoleQuery = "no-such-role";');
  assert.deepEqual(groups(), []);
  assert.equal(JSON.stringify(data), before, 'Grouping/search must not modify source records');
}

{
  const data = fixture();
  data.jobs = Array.from({length: 16}, (_, index) => job('company-' + index, {
    company: 'Company ' + String(index).padStart(2, '0'),
    title: 'Role ' + index,
  }));
  const app = appContext(data);
  app.evaluate('state.city = "San Francisco";');
  const html = app.evaluate('renderCityDetail(mapJobs())');
  assert.equal((html.match(/class="map-company-group"/g) || []).length, 16,
    'All company groups remain reachable beyond the previous 12-company cutoff');
  assert.match(html, /Company 15/);
  assert.match(html, /data-job="company-15"/);
  const details = [...html.matchAll(/<details\b[^>]*class="map-company-group"[^>]*>/g)].map(match => match[0]);
  assert.equal(details.length, 16);
  assert.match(details[0], /\bopen(?:\s|>|=)/, 'The first company is expanded initially');
}

{
  const data = fixture();
  data.jobs = [job('unsafe', {
    company: '<script>company()</script>', title: '<img src=x onerror="title()"> & Engineer',
    location: 'San Francisco; <svg onload="location()">', url: 'javascript:alert(1)',
  })];
  const app = appContext(data), html = app.evaluate('renderCityDetail(mapJobs())');
  assert.match(html, /&lt;script&gt;company\(\)&lt;\/script&gt;/);
  assert.match(html, /&lt;img src=x onerror=&quot;title\(\)&quot;&gt; &amp; Engineer/);
  assert.match(html, /San Francisco; &lt;svg onload=&quot;location\(\)&quot;&gt;/);
  assert.ok(!/<(?:img|script)\b/.test(html));
  assert.ok(!html.includes('href="javascript:'), 'Employer links accept only safe protocols');
}


{
  const data = fixture();
  data.jobs = Array.from({length: 60}, (_, index) => job('alpha-' + index, {
    title: 'Engineer ' + String(index).padStart(2, '0'),
    score: 100 - index,
    location: 'San Francisco, CA; Mountain View, CA',
  }));
  data.jobs.push(job('beta', {company: 'Beta'}));
  const before = JSON.stringify(data), app = appContext(data);
  app.evaluate('state.city = "San Francisco";');
  const roleCount = html => (html.match(/data-job="alpha-/g) || []).length;
  const initial = app.evaluate('renderMapCompanyResults(mapJobs())');
  app.nodes.set('#map-company-results', {innerHTML: initial});
  assert.equal(roleCount(initial), 6);
  assert.match(initial, /data-map-more="Alpha"/);
  const click = dataset => dispatchClick(app, dataset);
  const toggle = (company, open) => app.listeners.get('toggle').forEach(listener =>
    listener({target: {matches: selector => selector === '.map-company-group',
      dataset: {mapGroup: company}, open}}));
  // Preserve both an explicitly collapsed leading group and an opened peer.
  toggle('Alpha', false);
  toggle('Beta', true);
  click({mapMore: 'Alpha'});
  let html = app.nodes.get('#map-company-results').innerHTML;
  assert.equal(roleCount(html), 31, 'Show more adds 25 roles to the initial six');
  assert.match(html, /<details[^>]*data-map-group="Alpha"[^>]*\bopen(?:\s|>|=)/);
  assert.match(html, /<details[^>]*data-map-group="Beta"[^>]*\bopen(?:\s|>|=)/,
    'Expanding one role list must retain other company disclosure state');
  click({mapMore: 'Alpha'});
  html = app.nodes.get('#map-company-results').innerHTML;
  assert.equal(roleCount(html), 56);
  click({mapMore: 'Alpha'});
  html = app.nodes.get('#map-company-results').innerHTML;
  assert.equal(roleCount(html), 60, 'All company positions remain reachable');
  assert.ok(!html.includes('data-map-more="Alpha"'));
  app.evaluate('state.city = "Mountain View";');
  assert.equal(roleCount(app.evaluate('renderMapCompanyResults(mapJobs())')), 6,
    'Expansion limits are scoped to the city/company pair');
  app.evaluate('state.city = "San Francisco";');
  assert.equal(roleCount(app.evaluate('renderMapCompanyResults(mapJobs())')), 60);
  app.listeners.get('input').forEach(listener => listener({target: {
    id: 'map-role-query', value: 'Beta', matches() { return false; },
  }}));
  html = app.nodes.get('#map-company-results').innerHTML;
  assert.equal(app.evaluate('state.mapRoleQuery'), 'Beta');
  assert.match(html, /data-job="beta"/);
  assert.equal(roleCount(html), 0, 'Search updates only matching company positions');
  assert.equal(JSON.stringify(data), before, 'Disclosure and paging only change UI state');
}

// Verified company addresses remain distinct from the location of a particular role.
{
  const data=fixture();
  const reference={id:'alpha-office',kind:'company_location',company:'Alpha',city:'San Francisco',
    address:'123 Example Street, San Francisco, CA',latitude:37.78,longitude:-122.41,precision:'address',
    relationship:'company_office',worksite_confirmed:false,job_ids:[],source_url:'https://example.com/offices',
    coordinate_source_url:'https://geocoding.geo.census.gov/',coordinate_method:'census_address_range',
    coordinate_note:'Address-range interpolation; approximate, not an entrance or surveyed building point.',observed_at:'2026-11-14'};
  const confirmed={...reference,id:'alpha-confirmed',relationship:'job_site',worksite_confirmed:true,job_ids:['sf'],source_url:'https://example.com/jobs/sf'};
  data.locations=[reference,confirmed];
  data.jobs.find(role=>role.id==='sf').office_locations=[reference,confirmed];
  data.jobs.find(role=>role.id==='recheck').office_locations=[reference];
  data.jobs.push(job('alpha-remote',{company:'Alpha',remote:true,location:'United States'}));
  const before=JSON.stringify(data),app=appContext(data);
  const html=app.evaluate('renderCityDetail(mapJobs())');
  assert.match(html,/Company office · role assignment unconfirmed/);
  assert.match(html,/Approximate address pin/);
  assert.match(html,/Checked Nov 14/);
  assert.match(html,/Address-range interpolation/);
  assert.match(html,/href="https:\/\/example\.com\/offices"/);
  assert.match(html,/href="https:\/\/geocoding\.geo\.census\.gov\//);
  assert.match(html,/https:\/\/www\.google\.com\/maps\/search\/\?api=1&amp;query=/);
  assert.equal((html.match(/class="map-position-worksite"/g)||[]).length,1,
    'Only the explicitly linked role receives a confirmed street address');
  assert.equal((html.match(/class="map-office-reference/g)||[]).length,1,
    'Co-located posting and company evidence share a single company office card');
  app.evaluate('state.mapOfficeId="alpha-office";state.city="San Francisco";');
  assert.deepEqual(ids(app,'mapRoleGroups(mapJobs()).flatMap(group=>group.roles)'),['sf','recheck'],
    'Reference selection scopes company/city roles without inventing a worksite or assigning remote-only roles');
  app.evaluate('state.mapOfficeId="alpha-confirmed";');
  assert.deepEqual(ids(app,'mapRoleGroups(mapJobs()).flatMap(group=>group.roles)'),['sf'],
    'Confirmed worksite selection shows only the explicitly attached role');
  assert.match(app.evaluate('renderCityDetail(mapJobs())'),/Only roles explicitly linked to this address/);
  app.evaluate('state.mapScore="95";');
  assert.equal(app.evaluate('mapOfficeLocations(mapJobs()).length'),0,'No role in the current view means no office assignment');
  assert.equal(JSON.stringify(data),before);
}
{
  const data=fixture();
  data.jobs=[job('foster',{company:'Foster Co',location:'Foster City, CA'}),job('elsewhere',{company:'Foster Co',location:'San Francisco, CA'})];
  data.locations=[{id:'foster-office',company:'Foster Co',city:'Foster City',address:'1 Example Road, Foster City, CA',
    precision:'address',relationship:'company_office',source_url:'https://example.com/offices',observed_at:'2026-11-14'}];
  const app=appContext(data);
  app.evaluate('state.mapOfficeId="foster-office";state.city="Foster City";');
  assert.deepEqual(ids(app,'mapRoleGroups(mapJobs()).flatMap(group=>group.roles)'),['foster'],
    'Office cities outside the legacy city-center list still filter correctly');
  const html=app.evaluate('renderCityDetail(mapJobs())');
  assert.match(html,/<h3>Foster City<\/h3>/);
  assert.match(html,/Verified address · coordinates unavailable/);
  assert.ok(!html.includes('data-map-office-focus='),'Ungeocoded addresses do not promise a map pin');
}
{
  const data=fixture();
  const stale={id:'stale-site',company:'Alpha',city:'San Francisco',address:'Former worksite',precision:'building',
    relationship:'job_site',worksite_confirmed:true,job_ids:['sf'],latitude:37.78,longitude:-122.41,source_url:'https://example.com/old-posting'};
  data.locations=[stale];
  const app=appContext(data);
  assert.equal(app.evaluate('mapOfficeLocations(mapJobs()).length'),0,'Historical location evidence alone does not establish a current worksite');
  assert.ok(!app.evaluate('renderCityDetail(mapJobs())').includes('Former worksite'));
  data.locations=[{...stale,id:'unsafe-office',relationship:'company_office',worksite_confirmed:false,
    address:'<script>bad()</script>',source_url:'javascript:alert(1)',coordinate_source_url:'data:text/html,bad'}];
  const escaped=app.evaluate('renderCityDetail(mapJobs())');
  assert.match(escaped,/&lt;script&gt;bad\(\)&lt;\/script&gt;/);
  assert.ok(!escaped.includes('<script>'));
  assert.ok(!escaped.includes('href="javascript:'));
  assert.ok(!escaped.includes('href="data:'));
}

{
  const data=fixture();
  data.jobs=Array.from({length:139},(_,index)=>job('company-'+index,{company:'Company '+index,location:index===2?'Remote':'San Francisco',title:index===2?'Distributed Systems Engineer':'Software Engineer'}));
  const reference={id:'pin',company:'Company 0',city:'San Francisco',address:'123 Verified Avenue, San Francisco',
    relationship:'company_office',precision:'address',latitude:37.78,longitude:-122.41,source_url:'https://example.com/contact'};
  data.locations=[reference,{...reference,id:'address',company:'Company 1',latitude:undefined,longitude:undefined}];
  data.location_reviews=[{company:'Company 2',status:'remote_only',text:'Employer explicitly operates without offices.',observed_at:'2026-11-14',sources:[{url:'https://example.com/remote',title:'Employer policy'}]},
    {company:'Company 3',status:'unverified',text:'<script>untrusted evidence</script>',observed_at:'2026-11-14',sources:[{url:'javascript:alert(1)',title:'Unsafe link'}]}];
  const before=JSON.stringify(data),app=appContext(data);
  let html=app.evaluate('renderCityDetail(mapJobs())');
  assert.match(html,/139 companies/);
  assert.equal((html.match(/class="map-company-group"/g)||[]).length,139,'No company vanishes for lacking address evidence');
  assert.match(html,/data-map-coverage="unverified"[^>]*><strong>136<\/strong>/);
  assert.match(html,/Remote-only company/);
  assert.ok(!html.includes('<script>')&&!html.includes('href="javascript:'));
  dispatchClick(app,{mapCoverage:'unverified'});
  assert.equal(app.evaluate('mapRoleGroups(mapJobs()).length'),136);
  dispatchClick(app,{mapCoverage:'remote_only'});
  assert.deepEqual(ids(app,'mapRoleGroups(mapJobs()).flatMap(group=>group.roles)'),['company-2']);
  dispatchClick(app,{mapCoverage:''});
  app.evaluate('state.mapRoleQuery="Verified Avenue";');
  assert.equal(app.evaluate('mapRoleGroups(mapJobs()).length'),2,'Office addresses participate in company/role search');
  app.evaluate('state.mapRoleQuery="distributed";');
  assert.deepEqual(ids(app,'mapRoleGroups(mapJobs()).flatMap(group=>group.roles)'),['company-2']);
  app.evaluate('state.mapRoleQuery="";state.mapSelectedCompany="Company 138";');
  assert.deepEqual(ids(app,'mapRoleGroups(mapJobs()).flatMap(group=>group.roles)'),['company-138']);
  dispatchClick(app,{mapClear:'true'});
  assert.equal(app.evaluate('mapRoleGroups(mapJobs()).length'),139);
  assert.equal(JSON.stringify(data),before);
}
console.log('Insights integration checks passed.');

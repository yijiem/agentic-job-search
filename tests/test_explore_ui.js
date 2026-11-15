/* Run with node tests/test_explore_ui.js. Exercises the shipped Explore roles
 * controller without a browser, network requests, or workspace mutations. */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assets = path.join(__dirname, '../jobflow/web_assets');
const source = fs.readFileSync(path.join(assets, 'app.js'), 'utf8')
  .split('// Bootstrap browser session.')[0];

function appContext(data) {
  const nodes = new Map(), listeners = new Map();
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {
      innerHTML: '', textContent: '', addEventListener() {}, close() {}, setAttribute() {},
      querySelector() { return null; }, querySelectorAll() { return []; },
      focus() { this.focused = true; }, scrollIntoView() {}, remove() {}, insertAdjacentHTML() {},
      classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    });
    return nodes.get(selector);
  };
  const context = vm.createContext({
    URL, URLSearchParams, Intl, Date,
    setTimeout() { return 1; }, clearTimeout() {},
    document: {querySelector: node, querySelectorAll: () => [],
      addEventListener(type, callback) {
        if (!listeners.has(type)) listeners.set(type, []);
        listeners.get(type).push(callback);
      }},
    window: {addEventListener() {}, scrollTo() {}},
    location: {hash: '#jobs'},
    fetch: () => { throw new Error('Unexpected network request'); },
  });
  vm.runInContext(source, context, {filename: 'app.js'});
  context.fixture = data;
  vm.runInContext('setData(fixture); state.page = "jobs";', context);
  return {context, nodes, listeners, evaluate: code => vm.runInContext(code, context)};
}

const job = (id, fields = {}) => ({
  id, company: 'Alpha', title: 'Senior Engineer', location: 'San Francisco, CA',
  eligible: true, score: 90, availability: 'open', first_seen: '2026-11-14T18:00:00Z',
  url: 'https://example.com/jobs/' + id, ...fields,
});
const fixture = (jobs = []) => ({
  today: '2026-11-14', timezone: 'America/Los_Angeles',
  settings: {search: {min_score: 40}}, jobs,
  applications: [], interviews: [], tasks: [], research: [], history: [],
});
const read = (app, expression) => JSON.parse(app.evaluate(`JSON.stringify(${expression})`));
const ids = (app, expression = 'rolesInView()') => read(app, `${expression}.map(job => job.id)`);

async function click(app, dataset) {
  const attributes = new Set(Object.keys(dataset).map(key =>
    '[data-' + key.replace(/[A-Z]/g, letter => '-' + letter.toLowerCase()) + ']'));
  const element = {dataset};
  const target = {closest(selector) {
    return selector.split(',').some(part => attributes.has(part.trim())) ? element : null;
  }};
  for (const listener of app.listeners.get('click') || []) await listener({target});
}

function change(app, fields) {
  const target = {...fields, matches(selector) {
    return selector === '[data-filter]' ? Boolean(fields.dataset?.filter) :
      selector === '[data-select]' ? Boolean(fields.dataset?.select) : false;
  }};
  for (const listener of app.listeners.get('change') || []) listener({target});
}

async function run() {
  {
    const data = fixture([
      job('alpha-old', {score: 90, first_seen: '2026-11-10T18:00:00Z'}),
      job('beta-new', {company: 'Beta', score: 60}),
      job('alpha-low', {score: 50}),
      job('alpha-new', {score: 90}),
      job('recheck', {company: 'Beta', score: 80, availability: 'missing'}),
      job('closed', {availability: 'closed'}),
      job('nonmatch', {eligible: false}),
      job('below-threshold', {score: 10}),
    ]);
    const before = JSON.stringify(data), app = appContext(data);
    assert.equal(app.evaluate('state.rolesView'), 'companies', 'Explore opens the company grid by default');
    const initial = app.evaluate('renderJobs()');
    assert.ok(initial.includes('data-role-company='), 'The default view exposes company tiles');
    assert.ok(!initial.includes('<table'), 'The default view does not auto-open roles');
    assert.deepEqual(ids(app), []);
    await click(app, {rolesView: 'list'});
    assert.deepEqual(ids(app), ['alpha-new', 'alpha-old', 'recheck', 'beta-new', 'alpha-low'],
      'All roles list ranks by score then discovery time');
    app.evaluate('state.filters.company = "Beta"; state.filters.sort = "newest";');
    const listFilters = read(app, 'state.filters');
    await click(app, {rolesView: 'companies'});
    assert.equal(app.evaluate('state.rolesView'), 'companies', 'The actual view toggle is delegated');
    const groups = read(app, 'exploreCompanyGroups().map(group => ({company:group.company, ids:group.roles.map(role=>role.id)}))');
    assert.deepEqual(groups, [
      {company: 'Alpha', ids: ['alpha-new', 'alpha-old', 'alpha-low']},
      {company: 'Beta', ids: ['recheck', 'beta-new']},
    ], 'Tiles use all companies, current listing filters, and score order independent of list sort');
    assert.equal(app.evaluate('state.roleCompany'), '', 'Entering Companies never preselects the list-filter company');
    assert.deepEqual(ids(app), [], 'No roles are exposed before a company is clicked');
    const grid = app.evaluate('renderJobs()');
    assert.ok(!grid.includes('<table'), 'The initial company grid contains no role table');
    assert.ok(!grid.includes('data-action="next"'), 'The initial grid has no role pagination');
    assert.ok(!grid.includes('data-action="select-results"'), 'The initial grid has no select-all-roles action');
    assert.ok(!grid.includes('role-company-picker'), 'Companies are opened through tiles, not a preselected picker');
    assert.equal(app.evaluate('state.visibleJobs.length'), 0);
    await click(app, {roleCompany: 'Alpha'});
    assert.deepEqual(ids(app), ['alpha-new', 'alpha-old', 'alpha-low']);
    const detail = app.evaluate('renderJobs()');
    assert.ok(detail.includes('<table'), 'Clicking a company reveals its role table');
    assert.ok(detail.includes('data-action="companies-back"'), 'Company details provide a route back to the grid');
    assert.ok(!detail.includes('data-role-company='), 'Company detail is separate from the company grid');
    assert.deepEqual(read(app, 'state.filters'), listFilters, 'Company browsing preserves list filters');
    app.evaluate('state.selected.add("alpha-old"); state.pageIndex = 8;');
    await click(app, {rolesView: 'list'});
    assert.equal(app.evaluate('state.pageIndex'), 0);
    assert.deepEqual(read(app, 'state.filters'), listFilters);
    assert.deepEqual(ids(app), ['beta-new', 'recheck'], 'Returning to the list restores its selected company and sort');
    assert.deepEqual(read(app, '[...state.selected]'), ['alpha-old'], 'Changing views preserves selections');
    assert.equal(JSON.stringify(data), before, 'Browsing never edits records or their source order');
  }

  {
    // These companies deliberately lead different rankings, so choosing a
    // ranking cannot accidentally keep alphabetical or single-score order.
    const data = fixture([
      job('z-low', {company: 'Zulu', score: 40}),
      job('a-best', {company: 'Alpha', score: 94}),
      job('b-low', {company: 'Bravo', score: 41}),
      job('c-mid', {company: 'Charlie', score: 83}),
      job('z-strong', {company: 'Zulu', score: 80}),
      job('c-low', {company: 'Charlie', score: 82}),
      job('b-best', {company: 'Bravo', score: 99}),
      job('z-mid', {company: 'Zulu', score: 60}),
      job('c-best', {company: 'Charlie', score: 84}),
      job('z-other', {company: 'Zulu', score: 50}),
      job('closed', {company: 'Zulu', score: 100, availability: 'closed'}),
    ]);
    const before = JSON.stringify(data), app = appContext(data);
    const companies = () => read(app, 'exploreCompanyGroups().map(group => group.company)');
    assert.equal(app.evaluate('state.companySort'), 'roles', 'Company ranking defaults to the most matching positions');
    assert.deepEqual(companies(), ['Zulu', 'Charlie', 'Bravo', 'Alpha']);
    const stats = read(app, 'exploreCompanyGroups().map(({company, topScore, averageScore, strongMatches}) => ({company, topScore, averageScore, strongMatches}))');
    assert.deepEqual(stats, [
      {company:'Zulu', topScore:80, averageScore:57.5, strongMatches:1},
      {company:'Charlie', topScore:84, averageScore:83, strongMatches:3},
      {company:'Bravo', topScore:99, averageScore:70, strongMatches:1},
      {company:'Alpha', topScore:94, averageScore:94, strongMatches:1},
    ], 'Company statistics use all filtered matching roles; closed rows do not inflate them');
    app.evaluate('state.filters.sort = "newest"; state.selected.add("z-mid");');
    const filters = read(app, 'state.filters');
    for (const [sort, expected] of [
      ['score', ['Bravo', 'Alpha', 'Charlie', 'Zulu']],
      ['average', ['Alpha', 'Charlie', 'Bravo', 'Zulu']],
      ['strong', ['Charlie', 'Alpha', 'Bravo', 'Zulu']],
      ['roles', ['Zulu', 'Charlie', 'Bravo', 'Alpha']],
    ]) {
      change(app, {id:'company-sort', value:sort});
      assert.equal(app.evaluate('state.companySort'), sort, 'The actual company-sort change event updates ranking');
      assert.deepEqual(companies(), expected);
      const html = app.evaluate('renderJobs()');
      assert.match(html, /id="company-sort"/);
      const tiles = [...html.matchAll(/data-role-company="([^"]+)"/g)].map(match => match[1]);
      assert.deepEqual(tiles, expected, 'Rendered tile order follows the selected ranking');
      assert.ok(!html.includes('<table'), 'Ranking never automatically opens a company');
      assert.deepEqual(ids(app), []);
      assert.deepEqual(read(app, 'state.filters'), filters, 'Company ranking does not change role list sorting or filters');
      assert.deepEqual(read(app, '[...state.selected]'), ['z-mid'], 'Company ranking preserves queued selections');
    }
    change(app, {dataset:{filter:'score'}, value:'80'});
    assert.deepEqual(companies(), ['Charlie', 'Bravo', 'Alpha', 'Zulu'], 'Company order is recomputed from the filtered role counts');
    assert.equal(app.evaluate('exploreCompanyGroups().find(group=>group.company==="Zulu").averageScore'), 80);
    assert.equal(app.evaluate('state.roleCompany'), '', 'Changing a filter does not choose the leading company');
    change(app, {id:'company-sort', value:'average'});
    assert.deepEqual(companies(), ['Bravo', 'Alpha', 'Charlie', 'Zulu'], 'Average ranking uses the filtered scores');
    await click(app, {roleCompany:'Charlie'});
    assert.deepEqual(ids(app), ['c-best', 'c-mid', 'c-low'], 'Roles remain highest score first regardless of company ranking or list sort');
    assert.equal(app.evaluate('state.roleCompany'), 'Charlie');
    change(app, {id:'company-sort', value:'strong'});
    assert.equal(app.evaluate('state.roleCompany'), 'Charlie', 'A sort change cannot replace an explicitly selected company');
    assert.deepEqual(ids(app), ['c-best', 'c-mid', 'c-low']);
    await click(app, {action:'companies-back'});
    assert.equal(app.evaluate('state.companySort'), 'strong', 'Back preserves the company ranking');
    assert.deepEqual(companies(), ['Charlie', 'Bravo', 'Alpha', 'Zulu']);
    assert.ok(!app.evaluate('renderJobs()').includes('<table'));
    assert.equal(JSON.stringify(data), before, 'Ranking and statistics are read-only projections');
  }

  {
    const app = appContext(fixture([
      job('a-sf'), job('a-remote', {location: 'Remote, US', score: 70}),
      job('b-remote', {company: 'Beta', location: 'Remote, US', score: 80}),
      job('c-data', {company: 'Gamma', title: 'Data Engineer', location: 'Remote, US', score: 95}),
    ]));
    app.evaluate('state.rolesView = "companies"; state.roleCompany = "Alpha"; state.pageIndex = 4;');
    change(app, {dataset: {filter: 'location'}, value: 'remote'});
    assert.deepEqual(ids(app), ['a-remote'], 'Location filtering remains active within a company');
    change(app, {dataset: {filter: 'score'}, value: '80'});
    assert.equal(app.evaluate('state.roleCompany'), '', 'Filtering out the selected company returns to the grid without opening another company');
    assert.equal(app.evaluate('state.pageIndex'), 0);
    assert.deepEqual(ids(app), []);
    assert.ok(!app.evaluate('renderJobs()').includes('<table'));
    change(app, {dataset: {filter: 'q'}, value: 'Data'});
    assert.equal(app.evaluate('state.roleCompany'), '', 'Even one matching company is not auto-opened');
    assert.deepEqual(read(app, 'exploreCompanyGroups().map(group => group.company)'), ['Gamma']);
    await click(app, {roleCompany: 'Gamma'});
    assert.deepEqual(ids(app), ['c-data'], 'Text filtering determines company counts and role content');
    change(app, {dataset: {filter: 'q'}, value: 'No matching title'});
    assert.deepEqual(ids(app), []);
    assert.equal(app.evaluate('state.visibleJobs.length'), 0, 'Empty filters cannot retain stale selectable rows');
    const empty = app.evaluate('renderJobs()');
    assert.ok(!empty.includes('NaN') && !empty.includes('undefined'));
    assert.ok(!empty.includes('<table'), 'No matching companies cannot reveal a stale role table');
    change(app, {dataset: {filter: 'q'}, value: ''});
    await click(app, {roleCompany: 'Gamma'});
    assert.equal(app.evaluate('state.roleCompany'), 'Gamma', 'A company tile dispatches through the real click handler');
    assert.deepEqual(ids(app), ['c-data']);
  }

  {
    const data = fixture([
      ...Array.from({length: 57}, (_, index) => job(`a-${index}`, {score: 100 - index / 10})),
      job('beta', {company: 'Beta'}),
    ]);
    const app = appContext(data);
    await click(app, {rolesView: 'companies'});
    await click(app, {roleCompany: 'Alpha'});
    assert.equal(app.evaluate('state.visibleJobs.length'), 50);
    assert.equal(app.evaluate('rolesInView().length'), 57, 'Company grouping precedes pagination');
    await click(app, {action: 'next'});
    assert.equal(app.evaluate('state.pageIndex'), 1);
    assert.deepEqual(ids(app, 'state.visibleJobs'), ['a-50', 'a-51', 'a-52', 'a-53', 'a-54', 'a-55', 'a-56']);
    change(app, {id: 'select-page', checked: true});
    assert.deepEqual(read(app, '[...state.selected]'), ['a-50', 'a-51', 'a-52', 'a-53', 'a-54', 'a-55', 'a-56'],
      'Select page affects only visible company roles');
    const filters = read(app, 'state.filters');
    await click(app, {action: 'companies-back'});
    assert.equal(app.evaluate('state.roleCompany'), '', 'Back to companies clears the drilldown');
    assert.equal(app.evaluate('state.pageIndex'), 0, 'Returning to companies resets pagination');
    assert.deepEqual(ids(app, 'state.visibleJobs'), [], 'Hidden role rows cannot remain selected by page controls');
    assert.deepEqual(read(app, 'state.filters'), filters, 'Returning to the grid preserves active filters');
    assert.equal(app.evaluate('state.selected.size'), 7, 'Returning to the grid preserves queued selections');
    assert.ok(!app.evaluate('renderJobs()').includes('<table'));
    await click(app, {roleCompany: 'Beta'});
    assert.equal(app.evaluate('state.pageIndex'), 0, 'A company click resets pagination');
    assert.deepEqual(ids(app, 'state.visibleJobs'), ['beta']);
    await click(app, {action: 'select-results'});
    assert.equal(app.evaluate('state.selected.size'), 8);
    assert.equal(app.evaluate('state.selected.has("beta")'), true);
    await click(app, {roleCompany: 'Alpha'});
    await click(app, {action: 'select-results'});
    assert.equal(app.evaluate('state.selected.size'), 58,
      'Select all results includes every matching role in the selected company across pages');
  }

  {
    const app = appContext(fixture([
      job('alpha-today'), job('beta-today', {company: 'Beta', score: 70}),
      job('beta-earlier', {company: 'Beta', first_seen: '2026-11-13T18:00:00Z'}),
    ]));
    app.evaluate('state.rolesView = "companies"; state.roleCompany = "Alpha"; state.filters.company = "Beta"; state.page = "inbox";');
    assert.deepEqual(ids(app), ['beta-today'], 'Daily inbox uses its own date and list filters, not the selected company');
    const html = app.evaluate('renderJobs()');
    assert.ok(!html.includes('data-role-company='), 'Daily inbox does not show company tiles');
    await click(app, {action: 'select-results'});
    assert.deepEqual(read(app, '[...state.selected]'), ['beta-today']);
    assert.equal(app.evaluate('state.roleCompany'), 'Alpha', 'Visiting inbox preserves Explore company choice');
  }

  {
    const app = appContext(fixture([
      job('alpha'), job('beta', {company: 'Beta'}),
      job('beta-remote', {company: 'Beta', location: 'Remote, US'}),
    ]));
    app.evaluate('state.rolesView = "companies"; state.roleCompany = "Alpha"; state.page = "overview"; location.hash = "#overview";');
    await click(app, {company: 'Beta'});
    assert.equal(app.context.location.hash, '#jobs');
    app.evaluate('applyRoute(); render();');
    assert.equal(app.evaluate('state.roleCompany'), 'Beta', 'Company links synchronize the active company drilldown');
    assert.deepEqual(ids(app), ['beta', 'beta-remote'], 'An overview company link cannot retain the previously browsed company');
    app.evaluate('state.roleCompany = "Alpha"; state.page = "analytics"; location.hash = "#analytics";');
    await click(app, {companyCity: 'Beta', cityName: 'remote'});
    app.evaluate('applyRoute(); render();');
    assert.equal(app.evaluate('state.roleCompany'), 'Beta');
    assert.deepEqual(ids(app), ['beta-remote'], 'Map company/city drilldowns preserve their full scope in Companies view');
  }

  {
    const malicious = '<img src=x onerror="bad()"> & Partners';
    const app = appContext(fixture([job('unsafe', {company: malicious}), job('safe', {company: 'Beta'})]));
    app.evaluate('state.rolesView = "companies";');
    const html = app.evaluate('renderJobs()');
    assert.ok(html.includes('&lt;img src=x onerror=&quot;bad()&quot;&gt; &amp; Partners'));
    assert.ok(!html.includes('<img src=x'), 'Company data cannot inject markup into tiles or role tables');
    const tiles = html.match(/<button\b[^>]*data-role-company="[^"]+"[^>]*>/g) || [];
    assert.equal(tiles.length, 2, 'Each company receives a native keyboard-activatable tile button');
    assert.ok(!html.includes('role="tablist"') && !html.includes('role="tabpanel"'));
    assert.ok(!html.includes('<table'), 'Unselected grid remains role-free, including with unusual names');
    await click(app, {roleCompany: malicious});
    const detail = app.evaluate('renderJobs()');
    assert.ok(detail.includes('&lt;img src=x onerror=&quot;bad()&quot;&gt; &amp; Partners'));
    assert.ok(!detail.includes('<img src=x'), 'Company names stay escaped in the drilldown');
  }

  {
    const app = appContext(fixture([job('alpha'), job('beta', {company: 'Beta'})]));
    await click(app, {rolesView: 'companies'});
    await click(app, {roleCompany: 'Alpha'});
    await click(app, {rolesView: 'list'});
    await click(app, {rolesView: 'companies'});
    assert.equal(app.evaluate('state.roleCompany'), '', 'Re-entering Companies shows the grid instead of reopening an old detail');
    assert.deepEqual(ids(app), []);
    assert.ok(!app.evaluate('renderJobs()').includes('<table'));
  }

  {
    const app = appContext(fixture());
    app.context.window.JobflowCompanyLogos = {
      Alpha: '/logos/alpha.svg', External: 'https://example.com/logo.png',
      Traversal: '/logos/../private.svg', Unsafe: '/logos/a.svg" onerror="bad()',
    };
    assert.match(app.evaluate('companyLogo("Alpha")'), /<img src="\/logos\/alpha\.svg" alt=""/);
    for (const company of ['External', 'Traversal', 'Unsafe', 'Unmapped & Company']) {
      app.context.company = company;
      const fallback = app.evaluate('companyLogo(company)');
      assert.ok(!fallback.includes('<img'), 'Only local allowlisted logo paths become images');
      assert.ok(!fallback.includes('undefined') && !fallback.includes('null'));
      assert.match(fallback, /<span>[^<]+<\/span>/, 'Unknown companies retain readable initials');
    }
    const removed = [];
    const image = {hidden: false, matches: selector => selector === '.company-logo img',
      parentElement: {classList: {remove: value => removed.push(value)}}};
    for (const listener of app.listeners.get('error') || []) listener({target: image});
    assert.equal(image.hidden, true, 'A broken image is hidden');
    assert.deepEqual(removed, ['has-image'], 'A broken image reveals the initials fallback');
  }

  console.log('Explore UI checks passed: company rankings and statistics, role ranking, company filters, pagination, selection, list/inbox isolation, escaping, tile drilldowns/back navigation and logo fallbacks.');
}

run().catch(error => { console.error(error); process.exitCode = 1; });

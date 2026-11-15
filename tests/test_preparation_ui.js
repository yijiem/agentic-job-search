/* Optional presentation regressions: node tests/test_preparation_ui.js.
 * No browser or third-party packages are required. Python remains the app's
 * only runtime dependency; these checks exercise the real presentation code.
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../jobflow/web_assets/app.js'), 'utf8')
  .split('// Bootstrap browser session.')[0];

function appContext(data, fetchImpl, options = {}) {
  const nodes = new Map();
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {
      innerHTML: '', textContent: '', addEventListener() {}, close() {},
      classList: {add() {}, remove() {}, toggle() {}},
    });
    return nodes.get(selector);
  };
  const context = vm.createContext({
    URL, URLSearchParams, Intl, Date, setTimeout, clearTimeout,
    document: {querySelector: node, querySelectorAll: () => [], addEventListener() {}},
    window: {addEventListener() {}, scrollTo() {}},
    location: {hash: options.hash || '#prep', reload() { this.reloads = (this.reloads || 0) + 1; }},
    fetch: fetchImpl || (() => { throw new Error('Unexpected network request'); }),
  });
  node('meta[name="jobflow-build"]').content = options.build || '';
  vm.runInContext(source, context, {filename: 'app.js'});
  context.fixture = data;
  vm.runInContext('setData(fixture); state.page = "prep";', context);
  return {context, nodes, evaluate: code => vm.runInContext(code, context)};
}

const makeJob = (id, company = 'Example') => ({
  id, company, title: 'Senior Engineer ' + id, eligible: true, score: 90,
  availability: 'active', matched_skills: ['Go'],
});
const makeTask = (id, fields = {}) => ({
  id, title: 'Task ' + id, status: 'todo', category: 'coding', minutes: 15,
  due_date: '2026-11-14', notes: '', ...fields,
});
const makeGuide = (jobId = 'role-a') => ({
  job_id: jobId, company: 'Example', title: 'Senior Engineer',
  summary: 'Prepare before a date is confirmed.', focus: ['Storage'],
  steps: [{key: 'coding', category: 'coding', title: 'Practice coding', minutes: 30,
    prompt: 'Implement and test a bounded cache.'}],
  process: {text: 'Confirm the actual format.', observed_at: '2026-10-28',
    sources: [{url: 'https://example.com/guide', title: 'Official guide'}]},
  research: [], disclaimer: 'Original practice, not reported questions.',
});
const fixture = () => ({
  today: '2026-11-14', generated_at: '2026-11-14T12:00:00-08:00',
  timezone: 'America/Los_Angeles',
  settings: {search: {min_score: 40}, preparation: {daily_minutes: 90}},
  jobs: [makeJob('role-a'), makeJob('role-b'), makeJob('other', 'Elsewhere')],
  applications: [{job_id: 'role-b', company: 'Example', stage: 'saved'}],
  interviews: [], tasks: [], research: [], plan: {research_notes: []},
});

async function main() {
  {
    const data = fixture();
    data.tasks = [
      makeTask('shared', {target_key: 'general', job_id: '', company: 'General preparation'}),
      makeTask('company', {target_key: 'application:example', job_id: 'role-b', company: 'Example'}),
      makeTask('role', {target_key: 'interview:role-a', job_id: 'role-a', company: 'Example'}),
      makeTask('unrelated', {target_key: 'interview:other', job_id: 'other', company: 'Elsewhere'}),
      makeTask('other-interview', {target_key: 'interview:role-b', job_id: 'role-b', company: 'Example'}),
      makeTask('manual-general', {job_id: '', company: 'General preparation'}),
    ];
    const before = JSON.stringify(data);
    const app = appContext(data);
    app.context.guide = makeGuide();
    app.evaluate('state.prepJob = "role-a"; state.prepGuideFor = "role-a"; state.prepGuide = guide;');
    const html = app.evaluate('renderPrep()');
    for (const id of ['shared', 'company', 'role', 'manual-general']) {
      assert.ok(html.includes(`data-task="${id}"`), `Selected role retains ${id} practice`);
    }
    for (const id of ['unrelated', 'other-interview']) {
      assert.ok(!html.includes(`data-task="${id}"`), `Selected role excludes ${id} work`);
    }
    assert.ok(html.includes('Shared daily practice'));
    assert.ok(html.includes('Company preparation'));
    assert.ok(html.includes('90 / 90 min across all roles'), 'Total includes hidden role commitments once');
    assert.ok(html.includes('No interview scheduled yet'), 'Guide and schedule do not require an interview');
    assert.equal(JSON.stringify(data), before, 'Rendering must not change stored task or completion data');
  }

  {
    const data = fixture();
    data.plan.research_notes = [
      {kind: 'official_process', company: 'Example', text: 'Dated company process evidence', sources: []},
      {kind: 'scheduling', company: '', text: 'Preserved capacity explanation'},
      {kind: 'live_research', company: 'Example', text: 'Sourced current role guidance',
        sources: [{url: 'https://example.com/live-research', title: 'Current source'}]},
      {kind: 'custom_note', company: 'Example', text: 'Additional cited preparation context',
        sources: ['https://example.com/custom-context']},
      {kind: 'live_research', company: 'Elsewhere', text: 'Unrelated company discovery',
        sources: [{url: 'https://elsewhere.com/private-context', title: 'Other source'}]},
    ];
    data.research = [
      {id: 'company', kind: 'interview_report', company: 'Example', text: 'Company report evidence', sources: []},
      {id: 'role', kind: 'interview_report', job_id: 'role-a', company: 'Example', text: 'Exact role evidence', sources: []},
      {id: 'other', kind: 'interview_report', job_id: 'role-b', company: 'Example', text: 'Other team private format', sources: []},
      {id: 'unscoped', role: 'Interview Preparation Agent', text: 'Employer claim with unknown association',
        sources: [{url: 'https://elsewhere.com/unscoped-report', title: 'Unscoped report'}]},
      {id: 'shared-learning', kind: 'learning_resource', text: 'General learning material', sources: []},
    ];
    const app = appContext(data);
    app.context.guide = makeGuide();
    app.evaluate('state.prepJob = "role-a"; state.prepGuideFor = "role-a"; state.prepGuide = guide;');
    const html = app.evaluate('renderPrep()');
    for (const text of ['Dated company process evidence', 'Preserved capacity explanation', 'Company report evidence', 'Exact role evidence']) {
      assert.ok(html.includes(text), `Render relevant evidence: ${text}`);
    }
    assert.ok(!html.includes('Other team private format'), 'Do not imply another role’s report applies');
    assert.ok(html.includes('href="https://example.com/live-research"'), 'Live research preserves its citation');
    assert.ok(html.includes('href="https://example.com/custom-context"'), 'Other sourced plan notes preserve string citations');
    assert.ok(!html.includes('Unrelated company discovery'));
    assert.ok(!html.includes('href="https://elsewhere.com/private-context"'));
    assert.ok(!html.includes('Employer claim with unknown association'));
    assert.ok(!html.includes('href="https://elsewhere.com/unscoped-report"'));
    assert.ok(html.includes('General learning material'), 'General learning resources remain shared');
    app.evaluate('resetPrepGuide(); state.prepJob = "";');
    assert.ok(app.evaluate('renderPrep()').includes('Employer claim with unknown association'),
      'Unscoped research remains available in the all-roles view');
  }

  {
    const data = fixture();
    data.jobs = Array.from({length: 40}, (_, index) => makeJob('candidate-' + index));
    data.jobs.push({...makeJob('unsaved'), eligible: false, availability: 'closed'});
    const app = appContext(data);
    const guide = makeGuide('unsaved');
    guide.summary = '<img src=x onerror=alert(1)>';
    guide.steps[0].prompt = '<script>alert("untrusted")</script>';
    guide.process.sources = [{url: 'javascript:alert(1)', title: '<unsafe source>'}];
    app.context.guide = guide;
    app.evaluate('state.prepJob = "unsaved"; state.prepGuideFor = "unsaved"; state.prepGuide = guide;');
    const html = app.evaluate('renderPrep()');
    assert.match(html, /<option value="unsaved" selected>/, 'Directly selected role remains selectable beyond top matches');
    assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
    assert.ok(html.includes('&lt;script&gt;alert(&quot;untrusted&quot;)&lt;/script&gt;'));
    assert.ok(!html.includes('<img src=x'));
    assert.ok(!html.includes('href="javascript:'), 'Unsafe source schemes never become links');
    assert.ok(html.includes('Original practice, not reported questions.'));
  }

  {
    const requests = new Map();
    const app = appContext(fixture(), url => new Promise(resolve => requests.set(url, resolve)));
    app.evaluate('state.prepJob = "role-a";');
    const first = app.evaluate('loadPrepGuide("role-a")');
    assert.ok(app.evaluate('renderPrep()').includes('Preparing the guide'), 'Loading has visible feedback');
    app.evaluate('resetPrepGuide(); state.prepJob = "role-b";');
    const second = app.evaluate('loadPrepGuide("role-b")');
    requests.get('/api/jobs/role-b')({ok: true, json: async () => ({preparation_guide: makeGuide('role-b')})});
    await second;
    requests.get('/api/jobs/role-a')({ok: true, json: async () => ({preparation_guide: makeGuide('role-a')})});
    await first;
    assert.equal(app.evaluate('state.prepGuide.job_id'), 'role-b', 'A slow previous response cannot replace selected role’s guide');
    assert.equal(app.evaluate('state.prepGuideFor'), 'role-b');
  }

  {
    let fail = true;
    const app = appContext(fixture(), async () => fail
      ? {ok: false, json: async () => ({error: 'Temporary read failure'})}
      : {ok: true, json: async () => ({preparation_guide: makeGuide()})});
    app.evaluate('state.prepJob = "role-a";');
    await app.evaluate('loadPrepGuide("role-a")');
    const failed = app.evaluate('renderPrep()');
    assert.ok(failed.includes('Temporary read failure'));
    assert.ok(failed.includes('data-action="retry-prep-guide"'), 'Guide failure exposes a retry');
    fail = false;
    await app.evaluate('loadPrepGuide("role-a", true)');
    assert.equal(app.evaluate('state.prepGuide.job_id'), 'role-a');
    assert.equal(app.evaluate('state.prepGuideError'), '');
  }

  {
    const app = appContext(fixture());
    const guide = makeGuide();
    guide.steps[0].why_chosen = 'Network infrastructure benefits from <bounded> requests.';
    guide.steps[0].role_evidence = [{field:'title', text:'Staff Engineer, Network Infrastructure', url:'https://example.com/role'}];
    guide.leetcode_note = 'Role-relevant practice; completion status is unknown.';
    guide.leetcode_problems = [{id:'743', title:'Network Delay Time', url:'https://leetcode.com/problems/network-delay-time/',
      difficulty:'Medium', topics:['Graph'], why_chosen:'Compare reachable network paths <safely>.',
      role_evidence:guide.steps[0].role_evidence, minutes:30, observed_at:'2026-11-14',
      access:'Public problem statement verified.', practice_note:'Skill practice, not a reported company question.'}];
    app.context.guide = guide;
    app.evaluate('state.prepJob="role-a";state.prepGuideFor="role-a";state.prepGuide=guide;');
    const html = app.evaluate('renderPrep()');
    assert.ok(html.includes('Why this exercise'));
    assert.ok(html.includes('Network infrastructure benefits from &lt;bounded&gt; requests.'));
    assert.ok(html.includes('Staff Engineer, Network Infrastructure'));
    assert.ok(html.includes('href="https://leetcode.com/problems/network-delay-time/"'));
    assert.ok(html.includes('Compare reachable network paths &lt;safely&gt;.'));
    assert.ok(html.includes('completion status is unknown'));
    assert.ok(html.includes('not a reported company question'));
  }

  {
    const data = fixture();
    const linkedRole = {...makeJob('digitalocean-role','DigitalOcean'),title:'Staff Engineer, Network Infrastructure'};
    const app = appContext(data, async () => ({ok:true, json:async () => ({job:linkedRole,preparation_guide:makeGuide(linkedRole.id)})}),
      {hash:'#prep?job=digitalocean-role'});
    app.evaluate('applyRoute()');
    assert.equal(app.evaluate('state.page'), 'prep');
    assert.equal(app.evaluate('state.prepJob'), 'digitalocean-role', 'Reload restores selected role from URL');
    assert.equal(app.evaluate('prepRoleChoices().selected.id'), 'digitalocean-role', 'Loading does not relabel a selected role as all roles');
    await app.evaluate('loadPrepGuide(state.prepJob)');
    const html = app.evaluate('renderPrep()');
    assert.ok(html.includes('<option value="digitalocean-role" selected>DigitalOcean · Staff Engineer, Network Infrastructure</option>'),
      'Role absent from matching catalog remains selected after read-only detail fetch');
    app.evaluate('state.prepJob="role-a";navigate("prep");');
    assert.equal(app.context.location.hash, '#prep?job=role-a', 'Changing role updates a durable link');
    app.context.location.hash = '#prep?job=digitalocean-role';
    app.evaluate('applyRoute()');
    assert.equal(app.evaluate('state.prepJob'), 'digitalocean-role', 'Browser history restores role selection');
    assert.equal(app.evaluate('parseRoute("#prep?job=%3Cscript%3E").prepJob'), '', 'Invalid identifiers do not become a fetch target');
  }

  {
    const app = appContext({...fixture(),app_build:'build-1'}, async () => ({ok:true,json:async () => ({app_build:'build-2'})}), {build:'build-1'});
    app.evaluate('$("#main").innerHTML = "<form>Unsubmitted edits</form>";');
    await app.evaluate('checkAppVersion()');
    assert.equal(app.nodes.get('#app-update').hidden, false);
    assert.ok(app.nodes.get('#app-update').innerHTML.includes('Reload app'));
    assert.equal(app.nodes.get('#main').innerHTML, '<form>Unsubmitted edits</form>', 'Version checks do not rerender or overwrite open work');
    assert.equal(app.context.location.reloads, undefined, 'An update never forces a reload');
    app.evaluate('$("#action-dialog").open=true;reloadApp();clearTimeout(state.toastTimer);');
    assert.equal(app.context.location.reloads, undefined, 'Reload waits for an open edit to be closed');
    app.evaluate('$("#action-dialog").open=false;reloadApp();');
    assert.equal(app.context.location.reloads, 1);
  }

  {
    const data = fixture();
    data.services = [{id:'leetcode',name:'LeetCode',url:'https://leetcode.com/',mode:'browser_assisted',
      connection_status:'not_connected',summary:'Read saved account context.',capability_note:'No API connection established.',
      access_status:'signed_in',account_handle:'user<one>',last_observed_at:'2026-11-14',context:[{text:'Practice progress <recorded>',
        observed_at:'2026-11-14',sources:[{url:'https://leetcode.com/u/example/',title:'Observed profile'}]}]}];
    const app = appContext(data);
    const html = app.evaluate('renderAccountContext()');
    assert.ok(html.includes('No background sync'));
    assert.ok(html.includes('Signed in when checked'));
    assert.ok(html.includes('Browser access last checked:'));
    assert.ok(html.includes('user&lt;one&gt;'));
    assert.ok(html.includes('Practice progress &lt;recorded&gt;'));
    assert.ok(html.includes('href="https://leetcode.com/"'));
    assert.ok(!html.includes('Connect account'));
  }

  {
    const data = fixture();
    data.tasks = [makeTask('existing')];
    const app = appContext(data);
    app.context.proposal = {
      job_id:'role-a',selected_job_id:'role-a',company:'Example',title:'Storage <Engineer>',
      summary:'Recorded evidence only',budget:90,timezone:'America/Los_Angeles',
      new_count:1,rescheduled_count:1,focused_count:1,completed_preserved:3,
      next_steps:[{kind:'resume',job_id:'role-a',title:'Review saved resume',detail:'Reuse your saved version'},
        {kind:'practice',job_id:'role-a',title:'Practice <coding>',detail:'Recorded role overlap',prompt:'<script>untrusted()</script>'}],
      changes:[{id:'existing',kind:'focused',title:'Existing practice',date:'2026-11-14',minutes:30,
        focus:'Storage systems',guidance:'Test the <invariants>'}],
      days:[{date:'2026-11-14',minutes:90}],notes:['Keep my notes'],
      evidence_note:'Local planner; no live research',fingerprint:'reviewed-inputs',
    };
    app.evaluate('state.agentProposal=proposal');
    const html = app.evaluate('renderAgentPlanner()');
    assert.ok(html.includes('data-action="agent-apply"'), 'Schedule changes require explicit review approval');
    assert.ok(html.includes('data-task="existing"'), 'Existing practice remains editable from the preview');
    assert.ok(html.includes('data-resume-role="role-a"'), 'Guidance connects to the saved resume workflow');
    assert.ok(html.includes('3 completed tasks preserved'));
    assert.ok(html.includes('90 / 90 min'));
    assert.ok(html.includes('&lt;script&gt;untrusted()&lt;/script&gt;'));
    assert.ok(!html.includes('<script>'), 'Recorded job and prompt text cannot execute');
    app.evaluate('setData(fixture)');
    assert.equal(app.evaluate('state.agentProposal'), null, 'Refreshing context invalidates a displayed preview');
  }

  {
    const calls = [];
    const proposal = {job_id:'role-a',fingerprint:'current-state',next_steps:[],changes:[],days:[]};
    const app = appContext(fixture(), async (url, options) => {
      calls.push({url,payload:JSON.parse(options.body)});
      return {ok:true,json:async()=>({ok:true,proposal})};
    });
    app.evaluate('render=()=>{};state.prepJob="role-a";');
    await app.evaluate('previewAgentPlan()');
    assert.equal(calls.length,1);
    assert.equal(calls[0].payload.operation,'preview');
    assert.equal(calls[0].payload.job_id,'role-a');
    assert.equal(app.evaluate('state.agentProposal.fingerprint'),'current-state');
    assert.equal(app.evaluate('state.busy'),false);
    assert.ok(!calls.some(call=>call.payload.operation==='apply'), 'Generation never silently applies the schedule');
  }

  {
    const app = appContext(fixture(), async()=>({ok:false,json:async()=>({error:'Workspace is busy. Try again.'})}));
    app.evaluate('render=()=>{};');
    await app.evaluate('previewAgentPlan()');
    app.evaluate('clearTimeout(state.toastTimer)');
    assert.ok(app.evaluate('renderAgentPlanner()').includes('Workspace is busy. Try again.'), 'Preview errors remain visible with a retry action');
    app.evaluate('state.agentProposal={job_id:"role-a",fingerprint:"stale"};mutate=async()=>false;');
    await app.evaluate('applyAgentPlan()');
    assert.equal(app.evaluate('state.agentProposal'),null,'A failed apply cannot reuse the stale proposal');
    assert.ok(app.evaluate('renderAgentPlanner()').includes('Generate a fresh preview'));
  }

  console.log('Preparation UI regressions passed (12 scenarios).');
}

main().catch(error => { console.error(error); process.exitCode = 1; });

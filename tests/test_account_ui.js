/* Optional account presentation regressions: node tests/test_account_ui.js.
 * Exercises the browser code without a build step or third-party packages.
 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'../jobflow/web_assets/account.js'),'utf8')
  .split('// Bootstrap browser session.')[0];
function accountContext(options = {}) {
  const nodes = new Map();
  const query = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {innerHTML:'', textContent:'', value:'', className:'',
      focus(){}, scrollIntoView(){}, querySelector(){return null;}, dataset:{}});
    return nodes.get(selector);
  };
  const location = {search:options.search || '', replace(value){this.replaced=value;}, assign(value){this.assigned=value;}};
  const context = vm.createContext({URLSearchParams, Intl, Date, setTimeout, clearTimeout,
    document:{querySelector:query, querySelectorAll:() => [], title:''},
    window:{scrollTo(){}}, location,
    fetch:options.fetch || (() => {throw new Error('Unexpected network access');}),
  });
  vm.runInContext(source,context,{filename:'account.js'});
  return {context,nodes,query,location,evaluate:code => vm.runInContext(code,context)};
}
const fixture = () => ({complete:true, migrated:true,
  profile:{name:'Existing owner',headline:'Infrastructure engineer',skills:['C++','Distributed systems'],
    evidence:[{id:'evidence-1',text:'Exact experience; no invented metrics.',tags:['C++'],source_url:'https://example.com/resume',employer:'Original employer',original_note:'Preserve me'}],
    resume_sources:['https://example.com/resume']},
  preferences:{preferred_locations:['San Francisco, CA','Remote, US'],allowed_levels:['principal','management','senior'],
    target_tc:500000,currency:'USD',daily_minutes:90,timezone:'America/Los_Angeles',location_mode:'bay_area_remote',
    search:{max_packets_per_run:5,preferred_locations:['San Francisco, CA']}},
  uploads:[],
});
async function main() {
  for (const [reason, message] of [
    ['retry', 'This sign-in link could not be used.'],
    ['unavailable', 'a service was temporarily unavailable.'],
  ]) {
    const app = accountContext({search:`?signin=${reason}`,
      fetch:async () => ({ok:true,json:async()=>({authenticated:false})})});
    await app.evaluate('initializeAccount()');
    assert.ok(app.query('#account-main').innerHTML.includes(message));
    if (reason === 'unavailable')
      assert.ok(!app.query('#account-main').innerHTML.includes('This sign-in link could not be used.'));
  }
  {
    const app = accountContext();
    const original = fixture(); app.context.data = original;
    const before = JSON.stringify(original);
    app.evaluate('accountState.draft = draftFromOnboarding(data);');
    const payload = JSON.parse(app.evaluate('JSON.stringify(draftPayload(accountState.draft))'));
    assert.deepEqual(payload.profile, original.profile, 'Existing evidence IDs, exact claims, provenance and metadata survive');
    assert.deepEqual(payload.preferences.allowed_levels,['principal','management','senior']);
    assert.deepEqual(payload.preferences.preferred_locations,['San Francisco, CA','Remote, US']);
    assert.equal(payload.preferences.target_tc,500000);
    assert.equal(payload.preferences.search,undefined,'Unrelated backend settings are not sent as changes');
    app.evaluate('accountState.draft.profile.name = "Edited name"');
    assert.equal(JSON.stringify(original),before,'Form editing never mutates the fetched snapshot');
  }
  {
    const app = accountContext(); app.context.data = fixture();
    await app.evaluate('acceptAccount({authenticated:true,email:"owner@example.com",csrf_token:"csrf",onboarding:data})');
    assert.equal(app.location.replaced,'/','Existing owner goes directly to the workspace');
  }
  {
    const app = accountContext({search:'?edit=1'}); app.context.data = fixture();
    await app.evaluate('acceptAccount({authenticated:true,email:"owner@example.com",csrf_token:"csrf",onboarding:data})');
    assert.equal(app.location.replaced,undefined,'Explicit edit shows the existing profile');
    assert.ok(app.query('#account-main').innerHTML.includes('Your saved profile is already here.'));
    assert.ok(app.query('#account-main').innerHTML.includes('Exact experience; no invented metrics.'));
    const prefs = app.evaluate('preferencesCard()');
    for (const level of ['principal','management','senior']) assert.ok(prefs.includes(`value="${level}" checked`));
  }
  {
    const app = accountContext({search:'?edit=1'}); const data = fixture();
    data.profile.name='<img src=x onerror=alert(1)>';
    data.profile.evidence[0].text='</textarea><script>steal()</script>';
    app.context.data=data;
    await app.evaluate('acceptAccount({authenticated:true,email:"<script>email</script>",csrf_token:"csrf",onboarding:data})');
    const markup=app.query('#account-main').innerHTML;
    assert.ok(!markup.includes('<script>steal()'));
    assert.ok(markup.includes('&lt;/textarea&gt;&lt;script&gt;steal()&lt;/script&gt;'));
    assert.ok(!app.query('#account-navigation').innerHTML.includes('<script>email'));
  }
  {
    const app = accountContext(); app.context.data = fixture();
    app.evaluate('accountState.draft = draftFromOnboarding(data);');
    app.context.upload={id:'a'.repeat(32),filename:'resume.txt',extraction_status:'extracted',
      suggestions:[{id:'s1',text:'  Exact source text with a bullet •',tags:[],source_file_id:'a'.repeat(32),source_url:''}]};
    app.evaluate('accountState.pendingUpload=upload; accountState.pendingSuggestions=normalizedSuggestions(upload)');
    assert.equal(app.evaluate('accountState.draft.profile.evidence.length'),1,'Uploading does not silently add evidence');
    assert.ok(!app.evaluate('pendingUpload()').includes(' checked'),'Suggestions require an explicit selection');
    app.evaluate('appendSuggestions(accountState.draft,accountState.pendingSuggestions)');
    assert.equal(app.evaluate('accountState.draft.profile.evidence[1].text'),'  Exact source text with a bullet •');
    assert.equal(app.evaluate('accountState.draft.profile.evidence[1].source_file_id'),'a'.repeat(32));
    app.evaluate('accountState.pendingUpload={id:"pdf",filename:"resume.pdf",extraction_status:"needs_text"}; accountState.pendingSuggestions=[]');
    assert.ok(app.evaluate('pendingUpload()').includes('does not extract PDF text'));
  }
  {
    let requested;
    const app = accountContext({fetch:async (url,options) => {requested={url,options};return {ok:true,json:async()=>({ok:true})};}});
    app.evaluate('accountState.account={authenticated:true,csrf_token:"session-csrf"};');
    await app.evaluate('accountAPI("/api/onboarding",{profile:{name:"Updated"},preferences:{}})');
    assert.equal(requested.options.credentials,'same-origin');
    assert.equal(requested.options.headers['X-CSRF-Token'],'session-csrf');
    assert.equal(requested.options.headers.Authorization,undefined,'No browser bearer token required');
    assert.equal(requested.options.cache,'no-store');
  }
  {
    const app=accountContext(); app.context.data=fixture();
    app.evaluate('accountState.draft=draftFromOnboarding(data)');
    app.query('#preferred-locations').value='San Francisco, CA\nRemote, US';
    app.query('#location-mode').value='custom';
    app.query('#target-compensation').value='500000';
    app.query('#compensation-currency').value='USD';
    app.query('#daily-minutes').value='90';
    app.query('#profile-timezone').value='America/Los_Angeles';
    app.evaluate('capturePreferences()');
    assert.deepEqual(JSON.parse(app.evaluate('JSON.stringify(accountState.draft.preferences.preferred_locations)')),['San Francisco, CA','Remote, US'],'Commas inside locations survive form capture');
  }
  {
    const app=accountContext(),data=fixture();data.applicant={first_name:'Saved',email:'saved@example.com',phone:'123'};data.uploads=[{id:'onboard',filename:'Saved resume.pdf',application_resume_id:'resume-one'}];app.context.data=data;
    app.evaluate('accountState.draft=draftFromOnboarding(data)');
    const payload=JSON.parse(app.evaluate('JSON.stringify(draftPayload(accountState.draft))'));
    assert.deepEqual(payload.applicant,data.applicant,'Saved applicant details travel with onboarding');
    assert.match(app.evaluate('profileCard()'),/type="file"[^>]* multiple/,'Onboarding accepts multiple resume versions');
    assert.match(app.evaluate('uploadList()'),/\/api\/application-resumes\/resume-one/,'Saved uploads are reusable application files');
    app.evaluate('accountState.uploadQueue=[{id:"first",filename:"first.txt",suggestions:["First exact claim"]},{id:"second",filename:"second.txt",suggestions:["Second exact claim"]}];nextUploadReview()');
    assert.equal(app.evaluate('accountState.pendingUpload.id'),'first');
    assert.equal(app.evaluate('accountState.pendingSuggestions[0].text'),'First exact claim');
    app.evaluate('nextUploadReview()');assert.equal(app.evaluate('accountState.pendingUpload.id'),'second');
    assert.equal(app.evaluate('accountState.draft.profile.evidence.length'),1,'Review queue never silently adds evidence');
  }
  console.log('Account UI checks passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});

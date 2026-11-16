/* Run with Node's standard library: node tests/test_my_account_ui.js. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assets = path.join(__dirname, '../jobflow/web_assets');
const source = fs.readFileSync(path.join(assets, 'my-account.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const profile = () => ({profile: {name:'Taylor Candidate', headline:'Infrastructure engineer', skills:['Python'], evidence:[{text:'Built a queue service.', source_url:'https://example.com/evidence'}]}});
const library = () => ({workspace_id:'workspace-one', applicant:{first_name:'Taylor',last_name:'Candidate',email:'taylor@example.com'}, resumes:[
  {id:'newer',label:'Focused version',filename:'Focused.pdf',version:2,created_at:'2026-11-14T12:00:00Z'},
  {id:'default',label:'Current resume',filename:'Current.docx',version:1,created_at:'2026-11-12T12:00:00Z'},
], default_resume_id:'default', drafts:[]});
function workspace(rows=[]) {
  return {applications:rows, jobs:rows.map(row=>({id:row.job_id,company:row.company||'Example',title:row.title||row.job_id})),
    timezone:'America/Los_Angeles',settings:{search:{preferred_locations:['San Francisco'],allowed_levels:['senior'],target_tc:500000,currency:'USD'}}};
}
const response = data => ({ok:true,status:200,json:async()=>structuredClone(data)});
function harness(options={}) {
  const listeners={}, nodes=new Map(), reads=[], redirects=[];
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector,{innerHTML:'',textContent:'',attributes:{},setAttribute(key,value){this.attributes[key]=value;},addEventListener(){},close(){},querySelector(){return null;},querySelectorAll(){return [];},classList:{add(){},remove(){},toggle(){}}});
    return nodes.get(selector);
  };
  const fixture={profile:profile(),library:library(),...options};
  const context=vm.createContext({URL,URLSearchParams,Intl,Date,setTimeout,clearTimeout,
    document:{querySelector:node,querySelectorAll:()=>[],addEventListener(type,callback){(listeners[type]||=[]).push(callback);}},
    window:{addEventListener(){},scrollTo(){}},location:{hash:'#account',assign(url){redirects.push(url);}},
    fetch:async(url,request={})=>{reads.push({url,request});return fixture.fetch?fixture.fetch(url,request):response(url==='/api/profile'?fixture.profile:fixture.library);},
  });
  vm.runInContext(source,context,{filename:'my-account.js'});
  const api=context.window.JobflowMyAccount;
  let data=workspace();
  return {api,fixture,nodes,reads,redirects,context,node,
    async load(value=data){data=value;node('#main').innerHTML=api.render(data);api.mount();await flush();return node('#main').innerHTML;},
    html(){return api.render(data);},
    async click(dataset){for(const callback of listeners.click||[])callback({target:{closest:()=>({dataset})}});await flush();return node('#main').innerHTML;},
    search(value){for(const callback of listeners.input||[])callback({target:{id:'my-account-search',value}});return node('#my-account-results').innerHTML;},
    evaluate(code){return vm.runInContext(code,context);},
  };
}
const appRow=(job_id,stage,extra={})=>({id:job_id,job_id,company:'Example',title:job_id,stage,...extra});
const table=html=>html.match(/<tbody>([\s\S]*?)<\/tbody>/)?.[1]||'';
const stat=(html,label)=>html.match(new RegExp('<dt>'+label+'</dt><dd>(.*?)</dd>'))?.[1];

async function main() {
  {
    const h=harness(), html=await h.load();
    assert.match(html,/Taylor Candidate/);assert.match(html,/taylor@example.com/);assert.match(html,/Infrastructure engineer/);
    assert.match(html,/Built a queue service/);assert.match(html,/\$500,000/);assert.match(html,/San Francisco/);
    assert.equal(stat(html,'Resume versions'),'2');
    assert.ok(html.indexOf('Current resume')<html.indexOf('Focused version'),'Default resume appears before other versions');
    assert.match(html,/href="\/api\/application-resumes\/default"/);
    assert.deepEqual(h.reads.map(r=>r.url).sort(),['/api/profile','/api/resumes']);
    assert.ok(h.reads.every(r=>!r.request.method||r.request.method==='GET'),'Opening account is read-only');
    assert.ok(h.reads.every(r=>r.request.cache==='no-store'&&r.request.credentials==='same-origin'));
    assert.ok(!html.includes('/account?edit=1'),'Local account does not link to hosted setup');
    const hosted={...workspace(),account:{hosted:true,email:'login@example.com'}};
    const cloudHtml=await h.load(hosted);
    assert.match(cloudHtml,/Signed in as login@example.com/);assert.match(cloudHtml,/href="\/account\?edit=1"/);
  }
  {
    const rows=[appRow('saved-only','saved'),appRow('applied-undated','applied'),appRow('technical-undated','technical'),
      appRow('rejected-submitted','rejected',{applied_at:'2026-11-14T07:30:00Z'}),appRow('withdrawn-unknown','withdrawn'),
      appRow('saved-submitted','saved',{applied_at:'2026-11-12T12:00:00Z'}),appRow('rejected-invalid','rejected',{applied_at:'invalid'})];
    const h=harness(), html=await h.load(workspace(rows)), body=table(html);
    assert.equal(stat(html,'Applied roles'),'4');assert.equal(stat(html,'Saved roles'),'1');assert.equal(stat(html,'In interviews'),'1');
    for(const id of ['applied-undated','technical-undated','rejected-submitted','saved-submitted']) assert.ok(body.includes(id),id+' belongs to submitted history');
    for(const id of ['saved-only','withdrawn-unknown','rejected-invalid']) assert.ok(!body.includes(id),id+' is not evidence of submission');
    assert.match(body,/Date not recorded/);assert.match(body,/Nov 13, 2026/,'Dates use workspace timezone, not UTC');
    const saved=table(await h.click({myAccountFilter:'saved'}));
    assert.ok(saved.includes('saved-only'));assert.ok(!saved.includes('saved-submitted'));assert.match(saved,/Not applied/);
    const all=table(await h.click({myAccountFilter:'all'}));
    assert.ok(all.includes('withdrawn-unknown'));assert.match(all,/Submission not recorded/);
  }
  {
    const h=harness();h.fixture.profile.profile.name='<script>profile()</script>';
    h.fixture.profile.profile.headline='"><img src=x onerror=attack()>';
    h.fixture.profile.profile.skills=['<svg onload=attack()>'];
    h.fixture.profile.profile.evidence=[{text:'<script>evidence()</script>',source_url:'javascript:attack()'}];
    h.fixture.library.applicant.linkedin='javascript:attack()';
    h.fixture.library.resumes[0].label='"><img src=x onerror=attack()>';
    const html=await h.load(workspace([appRow('unsafe','applied',{company:'<script>company()</script>',title:'<img src=x onerror=attack()>',next_action:'<svg onload=attack()>'})]));
    assert.ok(!html.includes('<script>'));assert.ok(!html.includes('<img'));assert.ok(!html.includes('<svg'));
    assert.ok(!html.includes('href="javascript:'));assert.match(html,/&lt;script&gt;profile/);assert.match(html,/&lt;script&gt;company/);
  }
  {
    let failProfile=true;
    const h=harness({fetch:async url=>url==='/api/profile'&&failProfile?{ok:false,status:503}:response(url==='/api/profile'?profile():library())});
    const html=await h.load();
    assert.match(html,/Your profile could not be loaded/);assert.match(html,/Current resume/);assert.match(html,/taylor@example.com/);
    failProfile=false;
    const recovered=await h.click({myAccountAction:'retry'});
    assert.match(recovered,/Infrastructure engineer/);assert.ok(!recovered.includes('could not be loaded'));
    assert.equal(h.reads.length,4);assert.ok(h.reads.every(r=>!r.request.method));
    const failedLibrary=harness({fetch:async url=>url==='/api/resumes'?Promise.reject(new Error('Offline')):response(profile())});
    const partial=await failedLibrary.load();
    assert.match(partial,/Infrastructure engineer/);assert.match(partial,/Your resume library and contact details could not be loaded/);
    assert.equal(stat(partial,'Resume versions'),'—','Unavailable resumes do not become a zero count');
  }
  {
    const h=harness({library:{...library(),resumes:[],default_resume_id:null,drafts:[]}}), html=await h.load();
    assert.match(html,/No resumes in your library yet/);assert.equal(stat(html,'Resume versions'),'0');
    assert.ok(!html.includes('Default resume'));assert.match(html,/No submitted applications recorded/);
  }
  {
    const pending=[];
    const h=harness({fetch:url=>new Promise(resolve=>pending.push({url,resolve}))});
    await h.load();assert.equal(pending.length,2);
    h.api.unmount();h.node('#main').innerHTML='Another page';
    for(const item of pending)item.resolve(response(item.url==='/api/profile'?profile():library()));
    await flush();assert.equal(h.node('#main').innerHTML,'Another page','Late account results cannot overwrite another page');
    h.fixture.fetch=null;
    const reopened=await h.load();assert.match(reopened,/Current resume/);assert.equal(h.reads.length,4,'Reopening refreshes private data');
  }
  {
    const pending=[];
    const h=harness({fetch:url=>new Promise(resolve=>pending.push({url,resolve}))});
    const first=workspace();await h.load(first);
    const second=workspace();await h.load(second);assert.equal(pending.length,4);
    pending[2].resolve(response({profile:{...profile().profile,name:'Fresh profile'}}));
    pending[3].resolve(response({...library(),resumes:[],applicant:{first_name:'Fresh'}}));await flush();
    for(const item of pending.slice(0,2))item.resolve(response(item.url==='/api/profile'?profile():library()));
    await flush();const html=h.node('#main').innerHTML;
    assert.match(html,/Fresh profile/);assert.ok(!html.includes('Current resume'),'An older request cannot replace a refreshed snapshot');
    assert.equal(stat(html,'Resume versions'),'0');
  }
  {
    const rows=Array.from({length:23},(_,i)=>appRow('role-'+String(i).padStart(2,'0'),'applied',{company:i===22?'Unique Company':'Example',title:'Engineer '+i}));
    const h=harness(), html=await h.load(workspace(rows));
    assert.equal((table(html).match(/<tr>/g)||[]).length,10);assert.match(html,/1–10 of 23/);
    assert.match(await h.click({myAccountAction:'next'}),/11–20 of 23/);
    assert.match(await h.click({myAccountAction:'next'}),/21–23 of 23/);
    const filtered=h.search('unique company');assert.match(filtered,/Unique Company/);assert.match(filtered,/1–1 of 1/);
    assert.match(h.search('not-a-company'),/No matching applications/);assert.match(h.search('recruiter'),/No matching applications/);
    assert.match(h.search('Applied'),/1–10 of 23/,'Stage labels can be searched');
    assert.match(await h.click({myAccountFilter:'saved'}),/No matching applications/);
    assert.match(h.search(''),/No saved roles yet/);
  }
  {
    const h=harness({fetch:async()=>({ok:false,status:401})});await h.load();
    assert.ok(h.redirects.length>0);assert.ok(h.redirects.every(url=>url==='/account'));
  }
  {
    const h=harness();
    const appSource=fs.readFileSync(path.join(assets,'app.js'),'utf8').split('// Bootstrap browser session.')[0];
    vm.runInContext(appSource,h.context,{filename:'app.js'});
    h.context.fixture={...workspace(),today:'2026-11-14'};
    h.evaluate('setData(fixture); state.page="account"; render();');await flush();
    assert.equal(h.node('#account-link').hidden,false,'Account navigation is available locally');
    assert.equal(h.node('#account-link').attributes['aria-current'],'page');
    assert.equal(h.node('#breadcrumb').textContent,'My account');assert.match(h.node('#main').innerHTML,/<h1>My account<\/h1>/);
    assert.equal(h.context.document.title,'My account · Jobflow');
    const html=fs.readFileSync(path.join(assets,'index.html'),'utf8');
    assert.match(html,/<a id="account-link" href="#account">My account<\/a>/);
    assert.match(html,/src="\/my-account\.js\?v=/);assert.match(html,/href="\/my-account\.css\?v=/);
  }
  console.log('My account UI checks passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});

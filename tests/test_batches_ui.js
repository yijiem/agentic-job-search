/* node tests/test_batches_ui.js -- real batch UI with a small DOM boundary. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'../jobflow/web_assets/batches.js'),'utf8');
function harness(data) {
  const listeners={},writes=[],maintenanceWrites=[],posts=[],reads=[],nodes=new Map(),timers=[],timerDelays=[],dialogs=[],copied=[],tailoring=[],stats={renders:0};
  const document={addEventListener(name,callback){listeners[name]=callback;},querySelector(selector){return nodes.get(selector)||null;}};
  const context=vm.createContext({URL,Intl,Date,Set,Map,document,navigator:{clipboard:{async writeText(text){copied.push(text);}}},window:{confirm:()=>true},setTimeout(callback,delay){timers.push(callback);timerDelays.push(delay);return timers.length;},clearTimeout(){},fetch:async(url,options={})=>{
    if(options.method==='POST'){
      const payload=JSON.parse(options.body);posts.push({payload,headers:options.headers});
      (payload.op==='resume_sync'?maintenanceWrites:writes).push(payload);
      if(payload.op==='resume_sync'&&data.syncError)return {ok:false,json:async()=>({error:data.syncError})};
      if(payload.op==='codex_prepare'){
        if(data.prepareError)return {ok:false,json:async()=>({error:data.prepareError})};
        const batch=data.batches.find(value=>value.id===payload.batch_id);
        if(!batch.codex_run||['complete','cancelled'].includes(batch.codex_run.status))batch.codex_run={id:'run-fixture',status:'awaiting_start',started:false,job_ids:payload.job_ids};
        return {ok:true,json:async()=>({ok:true,batch,run:batch.codex_run,launch_url:data.launch_url||'codex://new?path=%2Fworkspace&prompt=Apply%20to%20the%20prepared%20batch',prompt:data.prompt||'Apply to the prepared batch and record actual confirmations.'})};
      }
      if(payload.op==='codex_discard')delete data.batches.find(value=>value.id===payload.batch_id).codex_run;
      return {ok:true,json:async()=>({ok:true,batch:data.batches[0]})};
    }
    reads.push(url);return {ok:true,json:async()=>JSON.parse(JSON.stringify(data))};
  }});
  vm.runInContext(source,context,{filename:'batches.js'});
  const api=context.window.JobflowBatches;
  return {api,listeners,writes,maintenanceWrites,posts,reads,nodes,context,dialogs,copied,tailoring,timers,timerDelays,stats,document,async load(workspace){api.render(workspace);api.mount({render(){stats.renders++;},toast(){},showDialog(html){dialogs.push(html);},startResumes(ids,options){tailoring.push({ids:Array.from(ids),options});},csrfToken:'fixture'});await new Promise(resolve=>setImmediate(resolve));return api.render(workspace);}};
}
const job=(id,fields={})=>({id,company:'Example',title:'Engineer '+id,score:90,availability:'open',eligible:true,url:'https://jobs.example.com/'+id,first_seen_date:'2026-11-14',...fields});
const workspace={jobs:[job('one'),job('two',{score:99}),job('three',{score:92}),job('four',{availability:'missing'}),job('five',{company:'Elsewhere'})],applications:[{job_id:'two',stage:'technical'}],selected:['one'],today:'2026-11-14'};
const snapshot=()=>({csrf_token:'token',workspace_id:'workspace',codex:{available:true},resumes:[{id:'resume-1',filename:'Resume.pdf',size:1024}],batches:[{id:'batch-1',name:'Reviewed roles',status:'draft',revision:3,resume_id:'resume-1',created_at:'2026-11-14T18:00:00Z',applicant:{first_name:'Test',last_name:'Candidate',email:'test@example.com'},items:[{job_id:'one',company:'Example',title:'Engineer',url:'https://jobs.example.com/one',status:'ready',resume_id:'resume-1',fields:[{key:'email',label:'Email',required:true,type:'email',supported:true},{key:'work',label:'Authorized to work?',type:'select',required:true,supported:true,options:['Yes','No']}],planned_answers:{email:'test@example.com',work:'Yes'}},{job_id:'three',company:'Example',title:'Other role',status:'uncertain',url:'https://jobs.example.com/three',message:'No confirmation was seen.'}]}]});
const batchClick=(action,extras={})=>({target:{closest:()=>({dataset:{batchAction:action,...extras}})}});
const codexButton=html=>html.match(/<button\b[^>]*data-batch-action="codex-review"[^>]*>/)?.[0];
async function main(){
  {
    const data=snapshot();data.codex={available:false,reason:'Use the local workspace for Codex.'};
    const app=harness(data),hosted={...workspace,account:{hosted:true}};
    await app.load(hosted);
    assert.equal(app.timers.length,0,'Hosted mode does not schedule full batch snapshots');
    const before=app.reads.length;
    await app.listeners.click(batchClick('reload'));
    assert.equal(app.reads.length,before+2,'Explicit refresh still restores resumes and reads the current hosted batch');
    const review={dataset:{batchReview:'one'}};
    app.listeners.input({target:{dataset:{batchAnswer:'work',answerType:'select'},value:'No',closest(selector){return selector==='[data-batch-review]'?review:null;},hasAttribute(){return false;}},type:'input'});
    data.batches[0].revision++;
    await app.listeners.click(batchClick('reload'));
    assert.match(app.api.render(hosted),/value="No" selected/,'Explicit hosted refresh retains unsaved answers');
  }
  {
    const data=snapshot();data.codex={available:false};
    const app=harness(data);await app.load(workspace);
    const before=app.reads.length;
    await app.timers[0]();
    assert.equal(app.reads.length,before,'A snapshot reporting Codex unavailable cancels an already scheduled poll');
    assert.equal(app.timers.length,1,'Unavailable Codex does not reschedule polling');
  }
  {
    const app=harness(snapshot());await app.load(workspace);
    const before=app.reads.length;
    assert.equal(app.timerDelays[0],5000,'Local Codex polling retains the five-second cadence');
    await app.timers[0]();
    assert.equal(app.reads.length,before+1);
    assert.equal(app.timerDelays.at(-1),5000);
  }
  {
    const {api}=harness(snapshot());
    const selection=api.selectRoles(workspace,{preset:'company',company:'Example',count:2},[],[]);
    assert.deepEqual(Array.from(selection.roles,j=>j.id),['three','one'],'Top N selects after excluding submitted/recheck roles');
    assert.match(selection.excluded.find(x=>x.job.id==='two').reason,/technical/);
    assert.match(selection.excluded.find(x=>x.job.id==='four').reason,/rechecking/);
    const daily=api.selectRoles(workspace,{preset:'daily',date:'2026-11-14'},[{id:'existing',status:'active',items:[{job_id:'one',status:'queued'}]}],[]);
    assert.deepEqual(Array.from(daily.roles,j=>j.id),['three','five']);
    assert.match(daily.excluded.find(x=>x.job.id==='one').reason,/active batch/);
    assert.equal(api.selectRoles(workspace,{preset:'selected'},[],['five']).roles[0].id,'five');
  }
  {
    const data=snapshot(),app=harness(data),html=await app.load(workspace);
    assert.ok(html.includes('Ready for your review'));
    assert.ok(html.includes('Outcome needs checking'));
    assert.ok(html.includes('Jobflow will not automatically resubmit'));
    assert.ok(!html.includes('data-batch-approve'));
    assert.ok(!html.includes('data-batch-approve="three"'));
    assert.ok(!html.includes('application-batch-authorize'));
    assert.ok(html.includes('Resume.pdf'));
    assert.ok(html.includes('Authorized to work?'));
    assert.ok(html.includes('test@example.com'));
    assert.ok(html.includes('Apply with Codex'));
    assert.ok(!html.includes('Download companion'));
    assert.equal(app.writes.length,0,'Rendering a ready batch never submits');
  }
  {
    const data=snapshot();data.batches[0].name='<script>alert(1)</script>';data.batches[0].items[0].title='<img src=x onerror=alert(1)>';data.batches[0].items[0].url='javascript:alert(1)';data.batches[0].items[0].fields[1].label='</label><script>bad()</script>';data.batches[0].items[0].planned_answers.work='<script>value</script>';
    const app=harness(data),html=await app.load(workspace);
    assert.ok(html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'));
    assert.ok(!html.includes('<script>'));
    assert.ok(!html.includes('href="javascript:'));
    assert.ok(!html.includes('<img src=x'));
  }
  {
    const data=snapshot(),app=harness(data);await app.load(workspace);
    assert.doesNotMatch(codexButton(app.api.render(workspace)),/\bdisabled\b/);
    await app.listeners.click(batchClick('codex-review'));
    assert.match(app.dialogs[0],/Review your Codex request/);
    assert.match(app.dialogs[0],/Example · Engineer/);
    assert.doesNotMatch(app.dialogs[0],/Other role/,'Uncertain applications are not offered for a new run');
    assert.match(app.dialogs[0],/Preview Resume.pdf/);
    assert.match(app.dialogs[0],/\/api\/application-resumes\/resume-1/);
    assert.match(app.dialogs[0],/No application is sent by this button/);
    assert.match(app.dialogs[0],/Pressing Send in Codex authorizes/);
    assert.equal(app.writes.length,0,'Reviewing a request does not reserve or submit anything');
    await app.listeners.click(batchClick('codex-prepare'));
    assert.deepEqual(app.writes,[{action:'batch',op:'codex_prepare',batch_id:'batch-1',revision:3,job_ids:['one'],workspace_id:'workspace'}]);
    assert.equal(app.posts.find(post=>post.payload.op==='codex_prepare').headers['X-CSRF-Token'],'token');
    const handoff=app.dialogs.at(-1);
    assert.match(handoff,/<a[^>]*href="codex:\/\/new\?path=/,'Only the server response becomes an explicit launch link');
    assert.match(handoff,/Press Send in Codex to start/);
    assert.match(handoff,/View request to copy manually/);
    assert.match(app.api.render(workspace),/Waiting for you to start in Codex/);
    assert.equal(data.batches[0].codex_run.status,'awaiting_start','Preparing does not claim that Codex is running');
    await app.listeners.click(batchClick('codex-copy'));
    assert.deepEqual(app.copied,['Apply to the prepared batch and record actual confirmations.']);
    assert.equal(app.writes.length,1,'Copying the request has no application side effect');
    await app.listeners.submit({target:{id:'application-batch-authorize',dataset:{}},preventDefault(){}});
    await app.listeners.click(batchClick('worker-reload'));
    await app.listeners.click(batchClick('reinspect',{jobId:'one'}));
    assert.equal(app.writes.length,1,'Retired extension controls cannot dispatch even through synthetic events');
  }
  {
    for(const reason of ['edits','hosted','paused','busy','started']){
      const data=snapshot();
      if(reason==='hosted')data.codex={available:false,reason:'Open your local Jobflow workspace.'};
      if(reason==='paused')data.batches[0].status='paused';
      if(reason==='busy')data.batches[0].items[0].status='running';
      if(reason==='started')data.batches[0].codex_run={id:'existing',status:'needs_input',started:true,job_ids:['one']};
      const app=harness(data);await app.load(workspace);
      if(reason==='edits'){
        const review={dataset:{batchReview:'one'}};
        app.listeners.input({target:{dataset:{batchAnswer:'work',answerType:'select'},value:'No',closest(selector){return selector==='[data-batch-review]'?review:null;},hasAttribute(){return false;}},type:'input'});
      }
      const html=app.api.render(workspace);
      if(reason==='started'){assert.equal(codexButton(html),undefined);assert.match(html,/original Codex chat/);}
      else assert.match(codexButton(html)||'',/\bdisabled\b/);
      if(reason==='edits')assert.match(html,/Save your edited answers and materials/);
      if(reason==='hosted')assert.match(html,/Open your local Jobflow workspace/);
      await app.listeners.click(batchClick('codex-review'));await app.listeners.click(batchClick('codex-prepare'));
      assert.equal(app.writes.length,0,reason+' prevents a new request');
    }
  }
  {
    const data=snapshot(),app=harness(data);await app.load(workspace);
    await app.listeners.click(batchClick('codex-prepare'));
    assert.equal(app.writes.length,0,'Preparing requires the explicit materials review step');
    await app.listeners.click(batchClick('codex-review'));
    data.batches[0].revision++;
    await app.timers[0]();
    await app.listeners.click(batchClick('codex-prepare'));
    assert.equal(app.writes.length,0,'A changed batch cannot use a stale review dialog');
  }
  {
    for(const launch_url of ['javascript:alert(1)','https://evil.example/new','codex://other?path=x&prompt=y']){
      const data=snapshot();data.launch_url=launch_url;data.prompt='</textarea><script>bad()</script>';
      const app=harness(data);await app.load(workspace);
      await app.listeners.click(batchClick('codex-review'));await app.listeners.click(batchClick('codex-prepare'));
      const html=app.dialogs.at(-1);
      assert.doesNotMatch(html,/<a[^>]*href=/,'Unexpected launch URLs are never rendered');
      assert.doesNotMatch(html,/<script>/);
      assert.match(html,/Copy request/,'Manual copy remains available when a launch link is unavailable');
    }
  }
  {
    const data=snapshot(),batch=data.batches[0];
    batch.codex_run={id:'finished-run',status:'complete',started:true,job_ids:['one']};
    batch.items[0].status='submitted';batch.items[0].evidence={text:'Application confirmed.',url:batch.items[0].url};
    batch.items[1].status='pending';batch.items[1].resume_id='resume-1';
    const app=harness(data);await app.load(workspace);
    assert.doesNotMatch(codexButton(app.api.render(workspace)),/\bdisabled\b/,'A completed partial run allows the remaining eligible roles');
    await app.listeners.click(batchClick('codex-review'));
    assert.match(app.dialogs.at(-1),/Other role/);
    assert.doesNotMatch(app.dialogs.at(-1),/Example · Engineer/,'Confirmed roles are excluded from the next request');
    await app.listeners.click(batchClick('codex-prepare'));
    assert.deepEqual(app.writes[0].job_ids,['three'],'The new request has exactly the scope displayed for review');
    assert.equal(data.batches[0].codex_run.status,'awaiting_start');
    assert.match(app.dialogs.at(-1),/<a[^>]*href="codex:\/\/new/);
    assert.match(app.api.render(workspace),/Application confirmed/,'Preparing remaining roles retains the completed application evidence');
  }
  {
    const data=snapshot();data.prepareError='This batch changed. Refresh it.';
    const app=harness(data);await app.load(workspace);
    await app.listeners.click(batchClick('codex-review'));await app.listeners.click(batchClick('codex-prepare'));
    assert.equal(app.dialogs.length,1,'A failed prepare never displays a launch link');
    assert.match(app.api.render(workspace),/This batch changed/);
  }
  {
    const data=snapshot();data.batches[0].items[0].status='queued';
    const app=harness(data);await app.load(workspace);
    await app.listeners.click(batchClick('codex-review'));await app.listeners.click(batchClick('codex-prepare'));
    let html=app.api.render(workspace);
    assert.match(html,/Cancel prepared request/,'An unstarted request can release its materials without cancelling the batch');
    assert.match(html,/<fieldset disabled>/,'Reserved batch materials cannot be edited through a dead-end form');
    assert.match(html,/<select[^>]*data-batch-answer="work"[^>]*disabled/,'Reserved role answers are read only');
    await app.listeners.click(batchClick('tailor-role',{jobId:'one'}));
    assert.equal(app.tailoring.length,0,'Tailoring cannot change reserved materials');
    await app.listeners.click(batchClick('codex-discard'));
    assert.deepEqual(app.writes.at(-1),{action:'batch',op:'codex_discard',batch_id:'batch-1',revision:3,workspace_id:'workspace'});
    html=app.api.render(workspace);
    assert.doesNotMatch(html,/Cancel prepared request/);
    assert.equal(data.batches[0].status,'draft','Discarding a request preserves the application batch');
    assert.equal(data.batches[0].items[0].planned_answers.work,'Yes');
  }
  {
    const data=snapshot(),app=harness(data);await app.load(workspace);
    await app.listeners.click(batchClick('codex-review'));await app.listeners.click(batchClick('codex-prepare'));
    app.context.navigator.clipboard.writeText=async()=>{throw new Error('Clipboard denied');};
    const status={textContent:''};app.nodes.set('[data-batch-copy-status]',status);
    await app.listeners.click(batchClick('codex-copy'));
    assert.match(status.textContent,/copy manually/,'A blocked clipboard has a visible copy fallback');
    assert.equal(app.writes.length,1);
  }
  {
    const data=snapshot(),app=harness(data);await app.load(workspace);
    const review={dataset:{batchReview:'one'}};
    app.listeners.input({target:{dataset:{batchAnswer:'work',answerType:'select'},value:'No',closest(selector){return selector==='[data-batch-review]'?review:null;},hasAttribute(){return false;}},type:'input'});
    const progress={innerHTML:''};app.nodes.set('[data-batch-codex-progress]',progress);
    const renders=app.stats.renders;
    data.batches[0].codex_run={id:'external-run',status:'needs_input',started:true,job_ids:['one']};
    data.batches[0].revision++;
    await app.timers[0]();
    assert.equal(app.stats.renders,renders,'Codex progress can refresh without replacing unsaved controls');
    assert.match(progress.innerHTML,/Codex needs your answers/);
    assert.match(progress.innerHTML,/original Codex chat/);
    assert.match(app.api.render(workspace),/value="No" selected/,'External progress does not discard unsaved answers');
    await app.listeners.click(batchClick('codex-discard'));
    assert.equal(app.writes.length,0,'An already started run cannot be discarded through stale controls');
  }
  {
    const data=snapshot(),app=harness(data);await app.load(workspace);
    const review={dataset:{batchReview:'one'}};
    const input={dataset:{batchAnswer:'work',answerType:'select'},value:'No',closest(selector){return selector==='[data-batch-review]'?review:null;},hasAttribute(){return false;}};
    app.listeners.input({target:input,type:'input'});
    let html=app.api.render(workspace);assert.ok(html.includes('value="No" selected'),'Edited answers survive a redraw');
    data.batches[0].revision=4;
    app.api.unmount();await app.load(workspace);
    html=app.api.render(workspace);assert.ok(html.includes('changed while you were editing'));
    await app.listeners.submit({target:{id:'',dataset:{batchReview:'one'}},preventDefault(){}});
    assert.equal(app.writes[0].revision,3,'Stale edits are not silently rebased over a worker update');
    assert.deepEqual(app.writes[0].items,[{job_id:'one',answers:{work:'No'}}]);
  }
  {
    const app=harness(snapshot());app.api.start(['five']);const html=await app.load(workspace);
    assert.ok(html.includes('Create an application batch'));assert.ok(html.includes('Selected roles (1)'));
    assert.ok(html.includes('Create batch'));
    assert.ok(html.includes('Top roles at a company'));assert.ok(html.includes('New daily roles'));
    assert.equal(app.writes.length,0,'Creating UI does not create or submit a batch');
  }
  {
    const data=snapshot();data.batches[0].codex_run={id:'prepared-run',status:'awaiting_start',started:false,job_ids:['one']};
    const app=harness(data);app.api.start(['one']);const html=await app.load(workspace);
    assert.doesNotMatch(html,/Create an application batch/,'First-load single-role entry opens its saved batch instead of an empty duplicate composer');
    assert.match(html,/data-batch-open="batch-1" aria-current="true"/);
    assert.match(html,/Open prepared request/);
    assert.equal(app.writes.length,0,'Opening the existing batch does not prepare or submit a request');
  }
  {
    const data=snapshot(),app=harness(data);app.api.start(['five']);await app.load(workspace);
    const create={id:'application-batch-create',dataset:{}};
    app.listeners.input({target:{dataset:{batchApplicant:'email'},value:'draft@example.com',closest(selector){return selector==='#application-batch-create'?create:null;},hasAttribute(){return false;}},type:'input'});
    app.api.start(['one']);await new Promise(resolve=>setImmediate(resolve));
    let html=app.api.render(workspace);
    assert.doesNotMatch(html,/Create an application batch/);
    assert.doesNotMatch(codexButton(html),/\bdisabled\b/,'A preserved hidden composer does not block the existing batch');
    await app.listeners.click(batchClick('new'));html=app.api.render(workspace);
    assert.match(html,/value="draft@example.com"/,'Opening an existing batch preserves an unfinished new-batch draft');
    assert.match(html,/Engineer five/,'Reopening the preserved draft also retains its selected roles');
    await app.listeners.click(batchClick('close-create'));
    const review={dataset:{batchReview:'one'}};
    app.listeners.input({target:{dataset:{batchAnswer:'work',answerType:'select'},value:'No',closest(selector){return selector==='[data-batch-review]'?review:null;},hasAttribute(){return false;}},type:'input'});
    app.api.start(['one']);await new Promise(resolve=>setImmediate(resolve));
    assert.match(app.api.render(workspace),/value="No" selected/,'Single-role routing preserves unsaved answers');
    assert.equal(app.writes.length,0);
  }
  {
    const data=snapshot(),app=harness(data);app.api.start(['one','five']);const html=await app.load(workspace);
    assert.match(html,/Create an application batch/,'Multi-role entry still creates a grouped batch');
    assert.match(html,/Selected roles \(2\)/);
    assert.match(html,/Already in an active batch/,'Existing roles retain the duplicate exclusion in multi-role selection');
    assert.match(html,/1 roles in this batch/);
  }
  {
    for(const status of ['uncertain','skipped']){
      const data=snapshot();data.batches[0].status='cancelled';data.batches[0].items[0].status=status;
      const app=harness(data);app.api.start(['one']);const html=await app.load(workspace);
      if(status==='uncertain')assert.doesNotMatch(html,/Create an application batch/,'A retained uncertain outcome opens for resolution');
      else assert.match(html,/Create an application batch/,'A cancelled skipped role can enter a new batch');
    }
  }
  {
    for(const status of ['needs_input','failed']) {
      const data=snapshot();data.batches[0].items[0].status=status;
      const app=harness(data),html=await app.load(workspace);
      assert.ok(html.includes('Record manual submission'));
      await app.listeners.click({target:{closest:()=>({dataset:{batchAction:'reconcile',jobId:'one'}})}});
      assert.equal(app.dialogs.length,1);
      assert.ok(app.dialogs[0].includes('name="outcome" value="submitted"'));
      assert.ok(!app.dialogs[0].includes('value="not_submitted"'));
      assert.ok(app.dialogs[0].includes('I submitted this role and verified its successful submission.'));
      assert.ok(app.dialogs[0].includes('name="text" required'));
      assert.equal(app.writes.length,0,'Opening reconciliation never records a submission');
    }
    for(const status of ['queued','running','submitting','submitted']) {
      const data=snapshot();data.batches[0].items[0].status=status;
      const app=harness(data);await app.load(workspace);
      await app.listeners.click({target:{closest:()=>({dataset:{batchAction:'reconcile',jobId:'one'}})}});
      assert.equal(app.dialogs.length,0,'Busy and completed roles cannot be manually reconciled');
    }
    const app=harness(snapshot());await app.load(workspace);
    await app.listeners.click({target:{closest:()=>({dataset:{batchAction:'reconcile',jobId:'three'}})}});
    assert.ok(app.dialogs[0].includes('value="not_submitted"'),'Uncertain outcomes keep both explicit resolutions');
  }
  {
    const data=snapshot(),item=data.batches[0].items[0];
    item.fields=[{key:'cv',label:'Resume',type:'file',required:true,supported:true},{key:'cover',label:'Cover letter',type:'file',required:false,supported:true}];
    item.planned_answers={cv:'Resume.pdf',cover:''};
    const app=harness(data),html=await app.load(workspace);
    assert.ok(html.includes('Resume *</span><strong>Resume.pdf</strong>'));
    assert.ok(html.includes('Cover letter</span><strong>No attachment</strong>'));
    assert.ok(html.includes('No file is selected for this optional attachment'));
  }
  {
    const data=snapshot(),item=data.batches[0].items[0];
    item.status='needs_input';item.fields[1].supported=false;
    item.planned_answers.work='Yes';
    const app=harness(data),html=await app.load(workspace);
    assert.doesNotMatch(html,/Manual submission required/,'Legacy unsupported controls do not force manual submission');
    assert.match(html,/fresh inspection in Codex/);
    assert.match(html,/Codex will inspect this control and its available choices/);
    assert.match(html,/Record manual submission/,'Actual manual confirmations can still be recorded');
    assert.match(html,/<select[^>]*data-batch-answer="work"[^>]*disabled/,'A prefilled answer does not make an unsupported control editable');
    assert.doesNotMatch(html,/<input[^>]*data-batch-answer="email"[^>]*disabled/,'Supported answers remain available for review');
    assert.ok(!html.includes('data-batch-approve="one"'),'Unsupported form cannot be authorized from the UI');
    assert.equal(app.writes.length,0,'Explaining the Codex path makes no application changes');
  }
  {
    for(const preserveEdits of [false,true]){
      const data=snapshot(),item=data.batches[0].items[0];item.status='needs_input';
      const app=harness(data);await app.load(workspace);
      await app.listeners.click(batchClick('codex-review'));await app.listeners.click(batchClick('codex-prepare'));
      assert.match(app.api.render(workspace),/Codex request prepared/);
      if(preserveEdits){
        const review={dataset:{batchReview:'one'}};
        app.listeners.input({target:{dataset:{batchAnswer:'work',answerType:'select'},value:'No',closest(selector){return selector==='[data-batch-review]'?review:null;},hasAttribute(){return false;}},type:'input'});
      }
      data.batches[0].revision++;item.status='ready';item.message='Form inspection finished.';data.batches[0].codex_run.status='running';data.batches[0].codex_run.started=true;
      const live={textContent:''};app.nodes.set('#batch-live-message',live);
      await app.timers[0]();
      const html=app.api.render(workspace);
      assert.ok(!html.includes('Codex request prepared. Open it'),'Actual progress replaces the stale prepared notice');
      assert.match(html,/Application progress updated|Progress refreshed/);
      if(preserveEdits){
        assert.match(html,/value="No" selected/,'Progress notices do not wipe unsaved answers');
        assert.match(live.textContent,/unsaved answers are retained/,'The visible notice updates without replacing edited controls');
      }
      assert.equal(app.writes.length,1,'Polling makes no additional application mutations');
    }
  }
  {
    const data=snapshot(),app=harness(data);await app.load(workspace);
    const initialRenders=app.stats.renders;
    await app.timers[0]();
    assert.equal(app.stats.renders,initialRenders,'Identical polling does not redraw the app');
    data.worker={worker_id:'worker-a',last_seen_at:new Date().toISOString()};
    await app.timers[1]();
    assert.equal(app.stats.renders,initialRenders,'A heartbeat updates status without redrawing the app');
    assert.doesNotMatch(app.api.render(workspace),/Browser worker recently active|Download companion/);
  }
  {
    const data=snapshot(),app=harness(data);await app.load(workspace);
    const focused={id:'batch-edit-first_name',selectionStart:1,selectionEnd:3,disabled:false,dataset:{},focus(options){this.focused=options;app.document.activeElement=this;},setSelectionRange(start,end){this.restoredSelection=[start,end];}};
    const disclosures=[{dataset:{batchDetails:'materials:batch-1'},open:true},{dataset:{batchDetails:'role-review:batch-1|one'},open:false}];
    const main={writes:0,html:'',contains(node){return node===focused;},querySelectorAll(selector){return selector.startsWith('details')?disclosures:[focused];},set innerHTML(value){this.writes++;this.html=value;app.document.activeElement=null;},get innerHTML(){return this.html;}};
    app.nodes.set('#main',main);app.document.activeElement=focused;
    data.batches[0].revision++;
    data.batches[0].items[0].message='The worker supplied a fresh inspection.';
    const appRenders=app.stats.renders;
    await app.timers[0]();
    assert.equal(main.writes,1,'Changed batch data updates only its main area');
    assert.equal(app.stats.renders,appRenders,'Scoped polling leaves app navigation untouched');
    assert.ok(main.html.includes('data-batch-details="materials:batch-1" open'),'Expanded materials stay open');
    assert.ok(main.html.includes('data-batch-details="role-review:batch-1|one"><summary>'),'Explicitly collapsed review stays closed');
    assert.equal(app.document.activeElement,focused,'Polling restores the active control');
    assert.deepEqual(Array.from(focused.restoredSelection),[1,3]);
    assert.equal(focused.focused.preventScroll,true);
  }
  {
    const data=snapshot();data.default_resume_id='resume-default';data.resumes.push({id:'resume-default',filename:'Default.docx',label:'Saved default',size:2048});data.applicant={first_name:'Saved',last_name:'Candidate',email:'saved@example.com'};
    const app=harness(data);app.api.start(['five']);let html=await app.load(workspace);
    assert.match(html,/value="resume-default" selected/);assert.match(html,/value="saved@example.com"/);
    assert.equal(app.maintenanceWrites.length,1,'Opening batches synchronizes existing resume files once');
    assert.equal(app.maintenanceWrites[0].op,'resume_sync');
    const form={id:'application-batch-create',dataset:{}};
    app.listeners.input({target:{dataset:{batchApplicant:'email'},value:'edited@example.com',closest(selector){return selector==='#application-batch-create'?form:null;},hasAttribute(){return false;}},type:'input'});
    data.applicant.email='new-default@example.com';
    app.api.unmount();await app.load(workspace);html=app.api.render(workspace);
    assert.match(html,/value="edited@example.com"/,'Reloading preserves the user’s entered applicant details');
    await app.listeners.submit({target:form,preventDefault(){}});
    const create=app.writes.find(call=>call.op==='create');assert.equal(create.applicant.email,'edited@example.com');assert.equal(create.applicant.first_name,'Saved');assert.equal(create.resume_id,'resume-default');
  }
  {
    const data=snapshot();data.syncError='Another workflow is updating this workspace.';
    const app=harness(data),html=await app.load(workspace);
    assert.match(html,/Saved resume restoration is postponed/);assert.match(html,/Reviewed roles/);assert.match(html,/Resume.pdf/,'A locked restoration leaves existing batches and resumes readable');
    assert.equal(app.maintenanceWrites[0].workspace_id,'workspace','Restoration uses the freshly read workspace');
  }
  {
    const data=snapshot();data.batches=[];data.applicant={first_name:'Test',last_name:'Candidate',email:'test@example.com'};
    const app=harness(data);app.api.start(['five','four','one','two']);await app.load(workspace);
    const form={id:'application-batch-create',dataset:{}};
    app.listeners.input({target:{dataset:{batchApplicant:'email'},value:'my-edit@example.com',closest(selector){return selector==='#application-batch-create'?form:null;},hasAttribute(){return false;}},type:'input'});
    await app.listeners.click({target:{closest:()=>({dataset:{batchAction:'tailor-create'}})}});
    assert.deepEqual(app.tailoring[0].ids,['five','one'],'Tailoring uses the eligible selected roles, excluding submitted and missing listings');
    assert.equal(app.tailoring[0].options.resumeId,'resume-1');
    assert.equal(app.writes.length,0,'Preparing a batch resume never creates or authorizes applications');
    const version={id:'tailored-five',filename:'Elsewhere.docx',label:'Elsewhere tailored',size:1024};data.resumes.push(version);data.default_resume_id=version.id;
    await app.tailoring[0].options.onSaved(version,'five');
    await app.tailoring[0].options.onSaved({id:'wrong-role',filename:'Wrong.docx'},'three');
    let html=app.api.render(workspace);assert.match(html,/Elsewhere tailored/);assert.match(html,/value="my-edit@example.com"/,'Tailoring preserves entered applicant details');
    await app.listeners.submit({target:form,preventDefault(){}});
    const create=app.writes.find(call=>call.op==='create');
    assert.deepEqual(create.job_ids,['five','one']);assert.deepEqual(create.items,[{job_id:'five',resume_id:'tailored-five'}]);
    assert.equal(create.resume_id,'resume-1','A tailored version becoming the future default never replaces this application’s chosen batch resume');
    assert.equal(create.applicant.email,'my-edit@example.com');
    assert.ok(!app.writes.some(call=>call.op==='authorize'));
  }
  {
    const data=snapshot();data.batches=[];
    const app=harness(data);app.api.start(['five']);await app.load(workspace);
    await app.listeners.click({target:{closest:()=>({dataset:{batchAction:'tailor-create'}})}});
    await app.tailoring[0].options.onSaved({id:'tailored-five',filename:'Tailored.docx',size:100},'five');
    await app.listeners.click({target:{closest:()=>({dataset:{batchAction:'clear-tailored',jobId:'five'}})}});
    await app.listeners.submit({target:{id:'application-batch-create',dataset:{}},preventDefault(){}});
    assert.deepEqual(app.writes.find(call=>call.op==='create').items,[],'A tailored assignment can be returned to the batch resume');
  }
  {
    const data=snapshot();data.batches[0].items[0].status='pending';data.batches[0].items[0].fields=[];
    const app=harness(data);await app.load(workspace);
    assert.match(app.api.render(workspace),/data-batch-action="tailor-role" data-job-id="one"/,'Tailoring is available before form inspection');
    await app.listeners.click({target:{closest:()=>({dataset:{batchAction:'tailor-role',jobId:'one'}})}});
    assert.deepEqual(app.tailoring[0].ids,['one']);
    await app.tailoring[0].options.onSaved({id:'tailored-one',filename:'One.docx',size:100},'one');
    const html=app.api.render(workspace);
    assert.match(html,/data-batch-review="one"/,'The staged resume can be saved even without form fields');
    assert.match(html,/value="tailored-one" selected/);
    assert.equal(app.writes.length,0,'Saving the resume only stages application material changes');
    await app.listeners.submit({target:{id:'',dataset:{batchReview:'one'}},preventDefault(){}});
    assert.deepEqual(app.writes[0].items,[{job_id:'one',answers:{},resume_id:'tailored-one'}]);
    assert.equal(app.writes[0].op,'update');
  }
  {
    const data=snapshot(),app=harness(data);await app.load(workspace);
    const review={dataset:{batchReview:'one'}};
    app.listeners.input({target:{dataset:{batchAnswer:'work',answerType:'select'},value:'No',closest(selector){return selector==='[data-batch-review]'?review:null;},hasAttribute(){return false;}},type:'input'});
    await app.listeners.click({target:{closest:()=>({dataset:{batchAction:'tailor-batch'}})}});
    assert.deepEqual(app.tailoring[0].ids,['one'],'Existing batch tailoring omits uncertain and noneditable roles');
    data.batches[0].revision=4;
    await app.tailoring[0].options.onSaved({id:'tailored-one',filename:'One.docx',size:100},'one');
    await app.listeners.submit({target:{id:'',dataset:{batchReview:'one'}},preventDefault(){}});
    assert.equal(app.writes[0].revision,3,'Tailoring preserves the revision of existing unsaved answers');
    assert.deepEqual(app.writes[0].items,[{job_id:'one',answers:{work:'No'},resume_id:'tailored-one'}]);
  }
  {
    for(const mode of ['tailor-create','tailor-batch']) {
      const data=snapshot(),app=harness(data);
      if(mode==='tailor-create'){data.batches=[];app.api.start(['five']);}
      await app.load(workspace);
      await app.listeners.click({target:{closest:()=>({dataset:{batchAction:mode}})}});
      data.workspace_id='another-account';data.resumes=[];data.batches=[];
      await app.tailoring[0].options.onSaved({id:'private-version',filename:'Private.docx',size:100},mode==='tailor-create'?'five':'one');
      assert.ok(!app.api.render(workspace).includes('Private.docx'),'A saved callback cannot attach the old account’s resume after a workspace switch');
      assert.equal(app.writes.length,0);
    }
  }
  {
    const data=snapshot(),app=harness(data);await app.load(workspace);
    await app.listeners.click({target:{closest:()=>({dataset:{batchAction:'tailor-role',jobId:'one'}})}});
    data.batches[0].items[0].status='submitted';
    await assert.rejects(app.tailoring[0].options.onSaved({id:'late-version',filename:'Late.docx',size:100},'one'),/application changed/);
    assert.ok(!app.api.render(workspace).includes('Late.docx'),'A saved resume cannot change an application that completed during review');
    assert.equal(app.writes.length,0);
  }
  console.log('Batch UI checks passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});

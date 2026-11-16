/* Run with Node's standard library: node tests/test_resumes_ui.js. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../jobflow/web_assets/resumes.js'),'utf8');
const workspace={jobs:[{id:'role-1',company:'Example',title:'Infrastructure engineer'}],applications:[],today:'2026-11-14'};
const snapshot=()=>({workspace_id:'one',csrf_token:'private-csrf',resumes:[{id:'resume-base',label:'Core resume',filename:'Core.docx',size:1024,editable:true,version:1},{id:'resume-pdf',label:'PDF resume',filename:'Resume.pdf',size:1024,editable:false,extraction_status:'needs_text'}],applicant:{first_name:'Taylor',last_name:'Candidate',email:'taylor@example.com'},default_resume_id:'resume-base',drafts:[]});
const draft=()=>({id:'draft-1',resume_id:'resume-base',job_id:'role-1',original_text:'Original verified experience',text:'Focused verified experience',status:'pending',changes:['Moved relevant experience first.'],matched_skills:['Python'],missing_skills:['Rust'],sources:[{label:'Role posting',url:'https://example.com/job'}]});
function harness(data=snapshot()){
  const listeners={},nodes=new Map(),writes=[],reads=[],drafts=new Map();
  const context=vm.createContext({URL,Intl,Date,document:{addEventListener(name,fn){listeners[name]=fn;},querySelector(selector){return nodes.get(selector)||null;}},window:{},fetch:async(url,options={})=>{
    reads.push(url);
    if(options.method==='POST'){
      const payload=JSON.parse(options.body);writes.push({payload,options});
      if(payload.op==='resume_sync'&&data.syncError)return {ok:false,json:async()=>({error:data.syncError})};
      if(payload.op==='resume_draft'){const value=draft();value.id=payload.job_id&&payload.job_id!=='role-1'?'draft-'+payload.job_id:'draft-1';value.job_id=payload.job_id||'';value.resume_id=payload.resume_id;if(payload.source_text)value.text=payload.source_text;drafts.set(value.id,value);data.drafts=[...drafts.values()];return {ok:true,json:async()=>({ok:true,draft:value})};}
      if(payload.op==='resume_default')data.default_resume_id=payload.resume_id;
      if(payload.op==='resume_profile')data.applicant=payload.applicant;
      if(payload.op==='resume_save_version'){drafts.get(payload.draft_id).status='saved';const version={id:'resume-new-'+data.resumes.length,parent_resume_id:'resume-base',filename:'tailored.docx',label:payload.label,version:2,editable:true};data.resumes.push(version);if(payload.set_default)data.default_resume_id=version.id;return {ok:true,json:async()=>({ok:true,resume:version})};}
      return {ok:true,json:async()=>({ok:true})};
    }
    if(url.startsWith('/api/resume-drafts/'))return {ok:true,json:async()=>({draft:drafts.get(url.split('/').pop())||draft()})};
    if(url.startsWith('/api/jobs/'))return {ok:true,json:async()=>({job:{id:'outside',company:'Other',title:'Archived role'}})};
    return {ok:true,json:async()=>JSON.parse(JSON.stringify(data))};
  }});
  vm.runInContext(source,context,{filename:'resumes.js'});
  const api=context.window.JobflowResumes;
  return {data,api,nodes,listeners,writes,reads,browserWindow:context.window,
    async load(){api.render(workspace);api.mount({csrfToken:'private-csrf',render(){},toast(){}});await new Promise(resolve=>setImmediate(resolve));return api.render(workspace);},
    html(){return api.render(workspace);},
    async click(action,extra={}){return listeners.click({target:{closest:()=>({dataset:{resumeAction:action,...extra}})}});},
    input(dataset,value,type='text',checked=false){listeners.input({target:{dataset,value,type,checked},type:'input'});},
    async submit(id){return listeners.submit({target:{id},preventDefault(){}});},
  };
}
async function main(){
  {
    const app=harness(),html=await app.load();
    assert.match(html,/Core resume/);assert.match(html,/taylor@example.com/);assert.match(html,/multiple required/);
    assert.ok(!html.includes('resume-target'));assert.ok(!html.includes('resume-role-search'));assert.ok(!html.includes('id="resume-tailor-form"'),'Tailoring starts from an application, not a standalone role picker');
    assert.equal(app.writes.length,0,'Opening the library is read-only');
    assert.deepEqual(app.reads,['/api/resumes'],'Navigation needs one library read');
    assert.match(html,/Restore earlier uploads/);
    assert.ok(!app.writes.some(x=>['create','authorize'].includes(x.payload.op)),'Library never submits applications');
  }
  {
    const data=snapshot();data.resumes[0].label='<script>bad()</script>';data.applicant.first_name='\"><img src=x onerror=bad()>';
    const html=await harness(data).load();assert.ok(!html.includes('<script>bad()'));assert.ok(!html.includes('<img src=x'));assert.ok(html.includes('&lt;script&gt;bad()'));
  }
  {
    const app=harness();await app.load();app.api.start('role-1');
    await app.submit('resume-tailor-form');
    assert.match(app.html(),/Review your resume draft/);assert.match(app.html(),/Moved relevant experience first/);assert.match(app.html(),/Original verified experience/);
    await app.submit('resume-review-form');assert.equal(app.writes.filter(x=>x.payload.op==='resume_save_version').length,0,'Saving requires explicit review');
    app.input({resumeReview:'confirmed'},'', 'checkbox',true);
    app.input({resumeReview:'draftText'},'User corrected exact experience');
    await app.submit('resume-review-form');assert.equal(app.writes.filter(x=>x.payload.op==='resume_save_version').length,0,'Editing clears confirmation');
    app.input({resumeReview:'label'},'Example application version');
    app.input({resumeReview:'confirmed'},'', 'checkbox',true);
    await app.submit('resume-review-form');
    const save=app.writes.find(x=>x.payload.op==='resume_save_version').payload;
    assert.equal(save.text,'User corrected exact experience');assert.equal(save.confirmed,true);assert.equal(save.draft_id,'draft-1');
    assert.match(app.html(),/Example application version/);assert.match(app.html(),/Based on Core resume/);
    assert.ok(app.data.resumes.some(r=>r.id==='resume-base'),'Saving never replaces the original');
  }
  {
    const app=harness();await app.load();await app.click('edit',{resumeId:'resume-base'});
    app.input({resumeReview:'draftText'},'Unsaved manual edit');
    app.input({resumeReview:'label'},'My separate copy');
    await app.click('close-draft');await app.click('open-draft',{draftId:'draft-1'});
    assert.match(app.html(),/Unsaved manual edit/);assert.match(app.html(),/My separate copy/);
    assert.ok(app.reads.includes('/api/resume-drafts/draft-1'),'Reopening loads the stored draft');
  }
  {
    const app=harness();await app.load();await app.click('edit',{resumeId:'resume-pdf'});
    assert.match(app.html(),/Edit a resume copy/);assert.ok(!app.html().includes('id="resume-target"'),'Manual edit needs no target role');
    app.input({resumeChoice:'sourceText'},'Exact pasted PDF text');
    await app.submit('resume-tailor-form');
    const call=app.writes.find(x=>x.payload.op==='resume_draft').payload;
    assert.equal(call.source_text,'Exact pasted PDF text');assert.equal(call.job_id,undefined);
    assert.match(app.html(),/Exact pasted PDF text/);
  }
  {
    const app=harness(),opened=[];app.browserWindow.JobflowPDFReview={open:async options=>opened.push(options)};
    app.data.default_resume_id='resume-pdf';await app.load();app.api.start('role-1');
    assert.ok(!app.html().includes('id="resume-source-text"'),'The PDF viewer extracts saved text without another paste or upload');
    await app.submit('resume-tailor-form');
    assert.equal(opened.length,1);assert.equal(opened[0].resumeId,'resume-pdf');assert.equal(opened[0].jobId,'role-1');assert.equal(opened[0].autoSuggest,true);
    assert.ok(!app.writes.some(write=>write.payload.op==='resume_draft'),'PDF tailoring opens an anchored review');
  }
  {
    const app=harness();await app.load();
    app.input({resumeApplicant:'phone'},'+1 415 555 0100');await app.click('default',{resumeId:'resume-pdf'});
    assert.match(app.html(),/\+1 415 555 0100/,'Default updates retain unsaved profile edits');
    await app.submit('resume-applicant-form');assert.equal(app.data.applicant.phone,'+1 415 555 0100');
  }
  {
    const app=harness();app.api.start('outside');await app.load();assert.match(app.html(),/Other · Archived role/,'Role links outside overview still have a target');
  }
  {
    const app=harness();await app.load();await app.click('edit',{resumeId:'resume-base'});
    app.input({resumeReview:'draftText'},'Private first-account edit');app.input({resumeApplicant:'phone'},'Private phone');
    app.data.workspace_id='second';app.data.applicant={first_name:'Another'};app.data.resumes=[];app.data.drafts=[];app.data.default_resume_id='';
    app.api.unmount();await app.load();
    assert.ok(!app.html().includes('Private first-account edit'));assert.ok(!app.html().includes('Private phone'),'Workspace changes clear unsaved private state');
    assert.match(app.html(),/value="Another"/);
  }
  {
    const data=snapshot();data.syncError='Another workflow is updating this workspace.';
    const app=harness(data);await app.load();
    assert.equal(app.writes.length,0,'A background search cannot turn navigation into a competing mutation');
    await app.click('restore');const html=app.html();
    assert.equal(app.writes[0].options.headers['X-CSRF-Token'],'private-csrf');
    assert.match(html,/Saved resume restoration is postponed/);assert.match(html,/Core resume/);assert.match(html,/taylor@example.com/,'A locked migration never hides saved files or application details');
    assert.equal(app.writes[0].payload.workspace_id,'one','Library sync is scoped to the fetched workspace');
    assert.equal(app.reads[0],'/api/resumes','Read identity before migrating account files');
  }
  {
    const app=harness();await app.load();await app.click('edit',{resumeId:'resume-pdf'});app.input({resumeChoice:'sourceText'},'Unsaved pasted source');await app.click('reload');
    assert.match(app.html(),/Unsaved pasted source/,'Refreshing the library keeps pasted resume text');
    await app.click('default',{resumeId:'resume-pdf'});
    assert.equal(app.writes.find(write=>write.payload.op==='resume_default').payload.workspace_id,'one','Mutations cannot be silently rebound to another account');
  }
  {
    const original=workspace.jobs;workspace.jobs=Array.from({length:350},(_,i)=>({id:'many-'+i,company:i===349?'Zebra':'Example',title:i===349?'Distributed systems engineer':'Infrastructure engineer '+i}));
    try{
      const app=harness();app.api.start('many-349');await app.load();let html=app.html();
      assert.match(html,/Zebra · Distributed systems engineer/);assert.match(html,/Selected application role/);
      assert.ok(!html.includes('resume-target'));assert.ok(!html.includes('resume-role-search'));
      assert.ok(!html.includes('Example · Infrastructure engineer'),'Other catalog roles are never selectable in resume tailoring');
      await app.submit('resume-tailor-form');
      assert.equal(app.writes.find(write=>write.payload.op==='resume_draft').payload.job_id,'many-349');
    }finally{workspace.jobs=original;}
  }
  {
    const original=workspace.jobs;workspace.jobs=[...original,{id:'role-2',company:'Second',title:'Backend engineer'},{id:'unselected',company:'Unselected',title:'Other job'}];
    try{
      const app=harness(),saved=[];let returned=0;await app.load();
      app.api.start(['role-1','role-2','role-1'],{resumeId:'resume-base',returnLabel:'Back to batch',onSaved:async(resume,jobId)=>saved.push({resume,jobId}),onReturn:()=>returned++});
      assert.match(app.html(),/Application role 1 of 2/);assert.match(app.html(),/Back to batch/);
      await app.submit('resume-tailor-form');app.input({resumeReview:'confirmed'},'','checkbox',true);await app.submit('resume-review-form');
      assert.equal(saved.length,1);assert.equal(saved[0].jobId,'role-1');assert.match(app.html(),/1 of 2 saved/);
      await app.click('next-role');assert.match(app.html(),/Application role 2 of 2/);assert.match(app.html(),/Second · Backend engineer/);
      await app.submit('resume-tailor-form');
      const requests=app.writes.filter(write=>write.payload.op==='resume_draft');
      assert.deepEqual(requests.map(write=>write.payload.job_id),['role-1','role-2']);
      assert.ok(requests.every(write=>write.payload.resume_id==='resume-base'),'Each role starts with the chosen base, not the previous tailored copy');
      app.input({resumeReview:'draftText'},'Unfinished second role edits');
      await app.click('previous-role');await app.click('next-role');await app.click('open-draft',{draftId:'draft-role-2'});
      assert.match(app.html(),/Unfinished second role edits/,'Moving among selected roles retains unsaved draft edits');
      app.input({resumeReview:'draftText'},'More edits before switching saved drafts');
      app.input({resumeReview:'label'},'Second role reviewed version');
      await app.click('open-draft',{draftId:'draft-1'});await app.click('open-draft',{draftId:'draft-role-2'});
      assert.match(app.html(),/More edits before switching saved drafts/,'Opening another saved draft caches the current edits');
      assert.match(app.html(),/Second role reviewed version/);
      app.input({resumeReview:'confirmed'},'','checkbox',true);await app.submit('resume-review-form');
      assert.deepEqual(saved.map(value=>value.jobId),['role-1','role-2']);
      await app.click('return-application');assert.equal(returned,1);
      assert.ok(!app.writes.some(write=>['create','authorize'].includes(write.payload.op)));
    }finally{workspace.jobs=original;}
  }
  {
    const app=harness(),opened=[],saved=[];app.browserWindow.JobflowPDFReview={open:async options=>opened.push(options)};
    await app.load();app.api.start('role-1',{resumeId:'resume-pdf',onSaved:async(resume,jobId)=>saved.push({resume,jobId})});
    await app.submit('resume-tailor-form');assert.equal(opened[0].jobTitle,'Example · Infrastructure engineer');assert.equal(opened[0].jobs,undefined);
    await opened[0].onSaved({id:'pdf-tailored',label:'Reviewed PDF'});assert.equal(saved[0].jobId,'role-1');assert.match(app.html(),/Reviewed PDF/);
    await app.click('view-pdf',{resumeId:'resume-pdf'});assert.equal(opened[1].jobId,'','Library review remains general even during tailoring');
    app.api.start('outside');await opened[0].onSaved({id:'late-pdf',label:'Old context'});assert.equal(saved.length,1,'Stale review callbacks cannot attach to a new application');
  }
  {
    const app=harness();await app.load();app.api.start('role-1',{onSaved:async()=>{throw new Error('Batch changed.');}});
    await app.submit('resume-tailor-form');app.input({resumeReview:'confirmed'},'','checkbox',true);await app.submit('resume-review-form');
    assert.match(app.html(),/was saved, but could not be added to the application/);assert.match(app.html(),/Batch changed/);
    assert.equal(app.writes.filter(write=>write.payload.op==='resume_save_version').length,1,'Attachment failure does not repeat the version save');
  }
  console.log('Resume UI checks passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});

/* Anchored PDF review invariants. Run with Node's standard library. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../jobflow/web_assets/pdf-review.js'),'utf8');
function harness(fetcher,storage=new Map(),timer){
  const calls=[],delays=[],listeners={},sidebar={innerHTML:''};
  const sessionStorage={getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)};
  const context={module:{exports:{}},window:{sessionStorage,addEventListener:(type,listener)=>{listeners[type]=listener;}},setTimeout:(fn,ms)=>{if(ms<10000)delays.push(ms);if(timer)timer(fn,ms);else if(ms<10000)fn();return ms;},clearTimeout:()=>{},URL,Uint8Array,Intl,console,btoa:value=>Buffer.from(value,'binary').toString('base64'),fetch:async(url,options={})=>{calls.push({url,payload:options.body?JSON.parse(options.body):undefined,options});if(fetcher)return fetcher(url,options);return {ok:true,json:async()=>({ok:true,review:{id:'review-one',revision:2,status:'saved',saved_resume_id:'revision-two',annotations:[]},resume:{id:'revision-two'}})};}};
  vm.runInNewContext(source,context,{filename:'pdf-review.js'});const api=context.module.exports;
  api._state.dialog={open:true,querySelector:selector=>selector==='#pdf-review-sidebar'?sidebar:null,querySelectorAll:()=>[]};
  api._state.options={resumeId:'original-one',jobs:[],workspaceId:'workspace-one',csrfToken:'csrf-one'};
  api._state.library={workspace_id:'workspace-one',csrf_token:'csrf-one',pdf_ai:{ai_available:false,ai_reason:'No model is configured.'}};
  api._state.label='Reviewed revision';api._state.document={text:'Built a Python service.\nMaintained C++ tools.'};
  api._state.review={id:'review-one',revision:1,status:'draft',annotations:[]};
  return {api,calls,sidebar,storage,delays,listeners,context};
}
function annotation(overrides={}){return {id:'a-one',kind:'replace',source:'manual',status:'accepted',start:8,end:14,before:'Python',after:'Go',page:1,rects:[[.1,.2,.2,.02]],note:'',...overrides};}
const click=(api,op,id)=>api._click({target:{closest:()=>({dataset:{pdfAction:op,annotationId:id}})}});
const reply=(body,status=200)=>({ok:status<400,status,json:async()=>body});
function typeDraft(api,text='My unsaved wording'){
  api._state.selection={...annotation({id:undefined,status:'pending'})};api._state.editingId=null;
  api._input({target:{id:'pdf-annotation-after',value:text}});
}
function wireForm(api){const old=api._state.dialog.querySelector;api._state.dialog.querySelector=selector=>({'#pdf-annotation-kind':{value:api._state.selection?.kind},'#pdf-annotation-after':{value:api._state.selection?.after,focus(){}},'#pdf-annotation-note':{value:api._state.selection?.note}}[selector]||old(selector));}
const submit=api=>api._submit({target:{id:'pdf-annotation-form'},preventDefault(){}});
async function main(){
  {
    const {api}=harness();const text='Built a Python service.\nMaintained C++ tools.';
    const proposed=annotation({status:'pending'}),rejected=annotation({status:'rejected'});
    assert.equal(api.applyEdits(text,[proposed]),text,'Pending suggestions never enter the revision');
    assert.equal(api.applyEdits(text,[rejected]),text,'Rejected suggestions never enter the revision');
    assert.equal(api.applyEdits(text,[annotation()]),'Built a Go service.\nMaintained C++ tools.');
    assert.equal(api.applyEdits(text,[annotation({kind:'note',note:'A comment'})]),text,'Notes are separate from resume content');
    assert.throws(()=>api.applyEdits(text,[annotation({before:'Ruby'})]),/no longer match/);
    assert.throws(()=>api.applyEdits(text,[annotation(),annotation({id:'a-two',start:8,end:22,before:'Python service',after:'Rust service'})]),/overlap/);
    assert.equal(api.applyEdits('A 🚀 team',[annotation({start:5,end:9,before:'team',after:'project'})]),'A 🚀 project','PDF offsets use JavaScript UTF-16 coordinates');
    assert.equal(api.applyEdits(text,[annotation({after:''})]),'Built a  service.\nMaintained C++ tools.','Intentional deletion remains an exact selected edit');
  }
  {
    const {api}=harness();const counts=api.countChanges([annotation(),annotation({status:'pending'}),annotation({status:'rejected'}),annotation({kind:'note'})]);
    assert.deepEqual(JSON.parse(JSON.stringify(counts)),{pending:1,accepted:1,rejected:1,notes:1});
    const normalized=api.normalizedRects([{left:110,top:220,width:50,height:10}],{left:100,top:200,width:200,height:400});
    assert.deepEqual(JSON.parse(JSON.stringify(normalized)),[[.05,.05,.25,.025]],'Highlights persist independently of zoom');
  }
  {
    const {api,sidebar}=harness();api._state.jobId='role-one';api._state.review.annotations=[annotation({before:'<script>unsafe()</script>',after:'<img onerror=bad()>',note:'User <notes>'})];api._state.label='\"><script>label()</script>';api._renderSidebar();
    assert.ok(!sidebar.innerHTML.includes('<script>unsafe()'));assert.ok(!sidebar.innerHTML.includes('<img onerror'));assert.ok(sidebar.innerHTML.includes('&lt;script&gt;unsafe()'));
    assert.match(sidebar.innerHTML,/No model is configured/);assert.match(sidebar.innerHTML,/Generate AI suggestions/);assert.match(sidebar.innerHTML,/data-pdf-action="suggest-ai" disabled/);
    assert.match(sidebar.innerHTML,/Confirm &amp; save|Confirm & save/);assert.match(sidebar.innerHTML,/type="submit" class="button primary" disabled/,'Saving is gated until the accepted preview is reviewed');
  }
  {
    const {api,calls}=harness();api._state.review.annotations=[annotation()];api._state.previewBytes=new Uint8Array([37,80,68,70]);
    await api._saveRevision();assert.equal(calls.length,0,'Preview alone is not explicit approval');
    api._state.confirmed=true;api._state.review.annotations=[annotation({status:'pending'})];await api._saveRevision();assert.equal(calls.length,0,'Pending proposals block saving');
    api._state.review.annotations=[];await api._saveRevision();assert.equal(calls.length,0,'Comments or unchanged originals do not create empty revisions');
    api._state.review.annotations=[annotation()];await api._saveRevision();assert.equal(calls.length,1);
    const payload=calls[0].payload;assert.equal(payload.op,'pdf_review_save');assert.equal(payload.workspace_id,'workspace-one');assert.equal(payload.review_id,'review-one');assert.equal(payload.revision,1);assert.equal(payload.confirmed,true);assert.equal(payload.text,'Built a Go service.\nMaintained C++ tools.');assert.equal(calls[0].options.headers['X-CSRF-Token'],'csrf-one');
    assert.equal(api._state.review.saved_resume_id,'revision-two');
  }
  {
    const {api,sidebar}=harness();api._state.selection={kind:'note',start:0,end:0,before:'',after:'',note:'',page:1,rects:[[.2,.2,.02,.02]]};api._renderSidebar();assert.match(sidebar.innerHTML,/Page note/);assert.match(sidebar.innerHTML,/value="replace" disabled/,'Scanned-page notes cannot silently become text replacements');
  }
  {
    const {api,calls,sidebar}=harness();api._state.jobId='role-one';
    api._state.options.jobTitle='Selected Company · Platform Engineer';
    api._state.options.jobs=[{id:'role-one',company:'Selected Company',title:'Platform Engineer'},{id:'unrelated',company:'Other Company',title:'Unrelated role'}];
    api._renderSidebar();
    assert.match(sidebar.innerHTML,/Selected Company · Platform Engineer/);
    assert.ok(!sidebar.innerHTML.includes('Other Company'),'Unselected jobs never appear in the PDF review');
    assert.ok(!sidebar.innerHTML.includes('pdf-target-role'));assert.ok(!sidebar.innerHTML.includes('pdf-role-search'));
    await api._change({target:{id:'pdf-target-role',value:'unrelated'}});
    await api._click({target:{closest:()=>({dataset:{pdfAction:'confirm-role'}})}});
    assert.equal(api._state.jobId,'role-one');assert.equal(calls.length,0,'Legacy role switch events cannot create a different review');
    await api._suggestions('local');
    assert.equal(calls.length,1);assert.equal(calls[0].payload.review_id,'review-one');assert.equal(calls[0].payload.op,'pdf_review_suggest','Suggestions remain attached to the application review');
  }
  {
    const {api,calls,sidebar}=harness();api._state.options.jobs=[{id:'unrelated',company:'Other Company',title:'Unrelated role'}];
    api._renderSidebar();
    assert.match(sidebar.innerHTML,/General resume review/);assert.match(sidebar.innerHTML,/Select text on the PDF/);
    assert.ok(!sidebar.innerHTML.includes('suggest-local'));assert.ok(!sidebar.innerHTML.includes('suggest-ai'));assert.ok(!sidebar.innerHTML.includes('Other Company'));
    await api._suggestions('local');assert.equal(calls.length,0,'General review never guesses a role from available jobs');
    api._state.jobId='role-one';api._state.review.company='Company & Co';api._state.review.job_title='Engineer <Platform>';api._renderSidebar();
    assert.match(sidebar.innerHTML,/Company &amp; Co · Engineer &lt;Platform&gt;/,'Recorded role information supplies the fixed label when the caller omits it');
  }
  {
    const {api}=harness();const font={widthOfTextAtSize:(text,size)=>text.length*size/2};
    const lines=api.wrapText('Averylongunbrokenword plus regular words',font,10,35);assert.ok(lines.every(line=>font.widthOfTextAtSize(line,10)<=35));assert.equal(lines.join('').replace(/ /g,''),'Averylongunbrokenwordplusregularwords');
    const paragraphs=api.layoutParagraphs('Taylor Candidate\nSkills\nPython, C++\nWork Experience\nExample Inc 2020 - Present\n- Built services with\nmeasured reliability.\nEducation\nExample University');
    assert.equal(api.layoutParagraphs('Taylor Candidate\n123 Main St · taylor@example.com\nSkills')[0],'Taylor Candidate','Contact digits never merge into the name heading');
    assert.equal(api.exportLink('javascript:alert(1)'),'');assert.equal(api.exportLink('https://example.com/'),'https://example.com/');assert.equal(api.exportLink('mailto:person@example.com'),'mailto:person@example.com');
    assert.ok(paragraphs.includes('- Built services with measured reliability.'));assert.ok(paragraphs.includes('Education'));assert.ok(paragraphs.includes('Example Inc 2020 - Present'),'Reflow preserves resume section and employer boundaries');
  }
  {
    let finish;const {api}=harness(()=>new Promise(resolve=>{finish=resolve;}));
    api._state.review.annotations=[annotation()];api._state.confirmed=true;api._state.previewBytes=new Uint8Array([37,80,68,70]);
    const pending=api._saveRevision();api._state.generation++;api._state.review={id:'new-account-review',revision:1,status:'draft',annotations:[]};api._state.notice='New account';api._state.busy=true;
    finish({ok:true,json:async()=>({ok:true,review:{id:'old-account-review',status:'saved'},resume:{id:'old-file'}})});await pending;
    assert.equal(api._state.review.id,'new-account-review','An old request cannot overwrite a newly opened account review');assert.equal(api._state.notice,'New account');assert.equal(api._state.busy,true,'Old completion does not change the new viewer loading state');
  }
  {
    const {api,calls,sidebar}=harness();api._state.review.annotations=[annotation()];api._state.previewBytes=new Uint8Array([37,80,68,70]);api._state.confirmed=true;api._state.selection={...annotation()};
    api._input({target:{id:'pdf-annotation-after',value:'My revised wording'}});
    assert.equal(api._state.selection.after,'My revised wording');assert.equal(api._state.editorDirty,true);assert.equal(api._state.confirmed,false,'Typing changes requires reviewing the saved proposal again');
    api._renderSidebar();assert.match(sidebar.innerHTML,/My revised wording/,'Sidebar rerenders preserve typed edits');
    api._state.confirmed=true;await api._saveRevision();assert.equal(calls.length,0,'Unsaved annotation text cannot be omitted silently from a saved PDF');
    api._state.jobId='role-one';await api._change({target:{id:'pdf-target-role',value:'role-two'}});assert.equal(api._state.jobId,'role-one','A stale role control cannot change the application context or discard an unfinished edit');
    await api._click({target:{closest:()=>({dataset:{pdfAction:'close'}})}});assert.equal(api._state.closeRequested,true);assert.match(sidebar.innerHTML,/Keep your unsaved annotation/);
  }
  {
    const {api}=harness();api._state.mode='preview';api._state.previewPdf={getPage:async()=>({getViewport:()=>({width:760})})};api._state.pages=[{viewport:{width:612}}];
    api._state.dialog.querySelector=selector=>selector==='.pdf-review-viewport'?{clientWidth:850}:null;
    await api._click({target:{closest:()=>({dataset:{pdfAction:'fit'}})}});
    assert.equal(api._state.scale,790/760,'Fit uses the displayed revision page dimensions');
  }
  {
    let attempts=0;const h=harness((url,options)=>{attempts++;return attempts<3?reply({error:'Busy',code:'workspace_busy',retryable:true},409):reply({review:{id:'review-one',revision:2,status:'draft',annotations:JSON.parse(options.body).annotations}});});
    typeDraft(h.api);wireForm(h.api);await submit(h.api);
    assert.equal(attempts,3);assert.deepEqual(h.delays,[500,1000]);assert.equal(h.api._state.error,'');assert.equal(h.api._state.editorDirty,false);assert.equal(h.storage.size,0,'Saved annotations clear their recovery copy');
    assert.equal(h.api._state.review.annotations[0].after,'My unsaved wording');assert.equal(new Set(h.calls.map(c=>JSON.stringify(c.payload))).size,1,'Busy retries repeat exactly the refused operation');
  }
  {
    const h=harness(()=>reply({error:'Workspace busy',code:'workspace_busy',retryable:true},409));typeDraft(h.api);wireForm(h.api);await submit(h.api);
    assert.equal(h.calls.length,4,'Retries are bounded');assert.deepEqual(h.delays,[500,1000,2000]);assert.equal(h.api._state.pendingSync.annotations[0].after,'My unsaved wording');assert.equal(h.api._state.editorDirty,false);assert.equal(h.storage.size,1);assert.equal(h.api._state.busy,false);
    assert.match(h.sidebar.innerHTML,/Saved in this tab; waiting to sync/);assert.equal(h.api._state.error,'');assert.ok(!h.sidebar.innerHTML.includes('Refresh review'),'Temporary contention queues the edit without a repeated warning');
  }
  {
    for(const body of [{error:'Changed',code:'review_conflict',retryable:false},{error:'Uncertain save',code:'workspace_update_uncertain',retryable:false},{error:'Legacy conflict'},{error:'Busy without safe retry',code:'workspace_busy'}]){
      const h=harness(()=>reply(body,409));typeDraft(h.api);wireForm(h.api);await submit(h.api);assert.equal(h.calls.length,1,'Only an explicitly safe pre-mutation refusal is retried');assert.equal(h.api._state.selection.after,'My unsaved wording');assert.equal(h.api._state.editorDirty,true);
    }
  }
  {
    const h=harness(()=>{throw new Error('Connection lost');});typeDraft(h.api);wireForm(h.api);await submit(h.api);assert.equal(h.calls.length,1,'Uncertain network writes are never repeated');assert.equal(h.storage.size,1);
  }
  {
    const other=annotation({id:'a-other',start:33,end:36,before:'C++',after:'Rust'});
    const h=harness((url,options)=>options.method==='POST'?reply({review:{id:'review-one',revision:3,status:'draft',annotations:JSON.parse(options.body).annotations}}):reply({review:{id:'review-one',revision:2,status:'draft',annotations:[other]}}));
    typeDraft(h.api);await click(h.api,'reload');assert.equal(h.api._state.selection.after,'My unsaved wording');assert.equal(h.api._state.editorDirty,true);assert.equal(h.api._state.draftConflict,'');
    wireForm(h.api);await submit(h.api);assert.equal(h.calls[1].payload.revision,2);assert.equal(h.calls[1].payload.annotations.find(a=>a.id==='a-other').after,'Rust','Refreshed unrelated edits are retained when saving the draft');
  }
  {
    const changed=annotation({after:'Someone else’s change'}),h=harness(()=>reply({review:{id:'review-one',revision:2,status:'draft',annotations:[changed]}}));h.api._state.review.annotations=[annotation()];await click(h.api,'edit','a-one');h.api._input({target:{id:'pdf-annotation-after',value:'My retained edit'}});
    await click(h.api,'reload');assert.equal(h.api._state.selection.after,'My retained edit');assert.match(h.api._state.draftConflict,/changed elsewhere/);assert.match(h.sidebar.innerHTML,/Someone else’s change/);
    wireForm(h.api);await submit(h.api);assert.equal(h.calls.length,1,'A refreshed edit cannot silently overwrite another user’s change');
    await click(h.api,'rebase-draft');assert.equal(h.api._state.draftConflict,'');assert.equal(h.api._state.selection.after,'My retained edit','Choosing the displayed latest version preserves local wording');
  }
  {
    const first=harness();typeDraft(first.api,'Survives a page reload');first.listeners.pagehide();const restored=harness(undefined,first.storage);
    assert.equal(restored.api._restoreDraft(),true);assert.equal(restored.api._state.selection.after,'Survives a page reload');assert.equal(restored.api._state.selection.start,8);assert.equal(restored.api._state.editorDirty,true);
    let prevented=false;restored.listeners.beforeunload({preventDefault(){prevented=true;}});assert.equal(prevented,true,'Leaving the page warns when text remains unsaved');
    await click(restored.api,'cancel-selection');assert.equal(first.storage.size,0,'Explicit cancellation clears the draft');
  }
  {
    const first=harness();typeDraft(first.api);const keys=[...first.storage.keys()];
    for(const mutate of [api=>{api._state.library.workspace_id='another-account';},api=>{api._state.options.resumeId='another-resume';},api=>{api._state.jobId='another-role';},api=>{api._state.review.id='another-review';},api=>{api._state.review.source_sha256='changed-pdf';}]){
      const h=harness(undefined,first.storage);mutate(h.api);assert.equal(h.api._restoreDraft(),false,'Recovery is scoped to the original account, PDF, role, review and source');assert.equal(h.api._state.selection,null);
    }
    assert.deepEqual([...first.storage.keys()],keys,'Opening other reviews does not delete the original draft');
    const h=harness(undefined,first.storage);h.storage.set(keys[0],'{broken');assert.equal(h.api._restoreDraft(),false,'Malformed browser data cannot break review loading');
  }
  {
    const first=harness();first.api._state.review.annotations=[annotation()];await click(first.api,'edit','a-one');first.api._input({target:{id:'pdf-annotation-note',value:'Keep this explanation'}});
    const h=harness(undefined,first.storage);h.api._state.review.annotations=[annotation({after:'Remote edit'})];assert.equal(h.api._restoreDraft(),true);assert.match(h.api._state.draftConflict,/changed elsewhere/);assert.equal(h.api._state.selection.note,'Keep this explanation');
    const removed=harness(undefined,first.storage);assert.equal(removed.api._restoreDraft(),true);assert.match(removed.api._state.draftConflict,/removed elsewhere/);assert.equal(removed.api._state.draftCanRebase,false);
  }
  {
    const h=harness();h.context.window.sessionStorage.setItem=()=>{throw new Error('Storage denied');};typeDraft(h.api);h.api._renderSidebar();assert.equal(h.api._state.selection.after,'My unsaved wording');assert.match(h.sidebar.innerHTML,/cannot keep a recovery copy/);
  }
  {
    let resume,ready;const waiting=new Promise(resolve=>{ready=resolve;});const h=harness(()=>reply({error:'Busy',code:'workspace_busy',retryable:true},409),new Map(),fn=>{resume=fn;ready();});typeDraft(h.api);wireForm(h.api);const pending=submit(h.api);
    await waiting;assert.equal(typeof resume,'function');h.api._state.generation++;resume();await pending;assert.equal(h.calls.length,1,'Closing or switching reviews cancels pending retries');assert.equal(h.api._state.selection.after,'My unsaved wording');
  }
  {
    let available=false;const h=harness((url,options)=>available?reply({review:{id:'review-one',revision:2,status:'draft',annotations:JSON.parse(options.body).annotations}}):reply({error:'Busy',code:'workspace_busy',retryable:true},409));typeDraft(h.api);wireForm(h.api);await submit(h.api);
    const id=h.api._state.pendingSync.annotations[0].id;assert.ok(id,'Queued new annotations have stable IDs before reaching the server');
    await click(h.api,'accept',id);assert.equal(h.calls.length,4,'Further edits update the local queue without fighting the worker');assert.equal(h.api._state.pendingSync.annotations[0].status,'accepted');
    h.api._state.confirmed=true;h.api._state.previewBytes=new Uint8Array([37,80,68,70]);await h.api._saveRevision();assert.equal(h.calls.length,4,'A PDF cannot be finalized while annotations are waiting to sync');
    available=true;await h.api._syncPending();assert.equal(h.calls.length,5);assert.equal(h.api._state.pendingSync,null);assert.equal(h.api._state.review.annotations[0].status,'accepted');assert.equal(h.storage.size,0);
  }
  {
    let available=false,finish;const h=harness((url,options)=>available?new Promise(resolve=>{finish=()=>resolve(reply({review:{id:'review-one',revision:2,status:'draft',annotations:JSON.parse(options.body).annotations}}));}):reply({error:'Busy',code:'workspace_busy',retryable:true},409));
    typeDraft(h.api);wireForm(h.api);await submit(h.api);available=true;const syncing=h.api._syncPending();await click(h.api,'edit',h.api._state.review.annotations[0].id);h.api._input({target:{id:'pdf-annotation-after',value:'Typed while syncing'}});finish();await syncing;
    assert.equal(h.api._state.pendingSync,null);assert.equal(h.api._state.selection.after,'Typed while syncing');assert.equal(h.api._state.editorDirty,true);assert.equal(h.storage.size,1,'Completing a background save never clears newer editor text');
    const restored=harness(undefined,h.storage);restored.api._state.review=h.api._state.review;assert.equal(restored.api._restoreDraft(),true);assert.equal(restored.api._state.selection.after,'Typed while syncing');assert.equal(restored.api._state.draftConflict,'');
  }
  {
    let available=false,finish;const h=harness((url,options)=>available?new Promise(resolve=>{finish=()=>resolve(reply({review:{id:'review-one',revision:2,status:'draft',annotations:JSON.parse(options.body).annotations}}));}):reply({error:'Busy',code:'workspace_busy',retryable:true},409));
    typeDraft(h.api);wireForm(h.api);await submit(h.api);available=true;const syncing=h.api._syncPending();await click(h.api,'accept',h.api._state.review.annotations[0].id);finish();await syncing;
    assert.equal(h.api._state.pendingSync.revision,2);assert.equal(h.api._state.pendingSync.annotations[0].status,'accepted','A queued change made during sync survives the older completion');assert.equal(h.api._state.review.annotations[0].status,'accepted');assert.equal(h.storage.size,1);
  }
  {
    const first=harness(()=>reply({error:'Busy',code:'workspace_busy',retryable:true},409));typeDraft(first.api);wireForm(first.api);await submit(first.api);
    const restored=harness(undefined,first.storage);assert.equal(restored.api._restoreDraft(),true);assert.equal(restored.api._state.pendingSync.annotations[0].after,'My unsaved wording');assert.equal(restored.api._state.pendingSync.blocked,false);
    const changed=harness(undefined,first.storage);changed.api._state.review.revision=3;assert.equal(changed.api._restoreDraft(),true);assert.equal(changed.api._state.pendingSync.blocked,true);await changed.api._syncPending();assert.equal(changed.calls.length,0,'An older queued list never overwrites a newer server revision');
  }
  {
    let conflict=false;const h=harness(()=>reply(conflict?{error:'Review changed',code:'review_conflict',retryable:false}:{error:'Busy',code:'workspace_busy',retryable:true},409));typeDraft(h.api);wireForm(h.api);await submit(h.api);conflict=true;await h.api._syncPending();assert.equal(h.api._state.pendingSync.blocked,true);assert.equal(h.api._state.pendingSync.annotations[0].after,'My unsaved wording');const count=h.calls.length;await h.api._syncPending();assert.equal(h.calls.length,count,'A conflict stops automatic queue retries');
  }
  {
    const original={id:'review-one',resume_id:'original-one',job_id:null,revision:4,status:'draft',source_sha256:'source-one',annotations:[],document:{text:'Built a Python service.\nMaintained C++ tools.'}};
    const h=harness(url=>url.includes('?')?reply({reviews:[original]}):reply({review:original}));h.api._state.resume={sha256:'source-one'};await h.api._createReview();assert.equal(h.calls.length,2);assert.ok(h.calls.every(c=>!c.payload),'Reopening a verified review uses GET requests even while the worker holds the write lease');assert.equal(h.api._state.review.revision,4);
  }
  {
    const h=harness((url,options)=>options.method==='POST'?reply({error:'Busy',code:'workspace_busy',retryable:true},409):reply({review:{id:'review-one',revision:2,status:'draft',annotations:[annotation({after:'Remote change'})]}}));typeDraft(h.api);wireForm(h.api);await submit(h.api);await click(h.api,'reload');assert.equal(h.api._state.pendingSync.blocked,true);assert.equal(h.api._state.review.annotations[0].after,'My unsaved wording');assert.equal(h.api._state.remoteReview.annotations[0].after,'Remote change');assert.match(h.sidebar.innerHTML,/Latest saved annotations/);
  }
  {
    let available=false,finish;const h=harness((url,options)=>available?new Promise(resolve=>{finish=()=>resolve(reply({review:{id:'review-one',revision:2,status:'draft',annotations:JSON.parse(options.body).annotations}}));}):reply({error:'Busy',code:'workspace_busy',retryable:true},409));typeDraft(h.api);wireForm(h.api);await submit(h.api);available=true;const syncing=h.api._syncPending();
    await click(h.api,'reload');await click(h.api,'discard-queued');await click(h.api,'discard-close');assert.equal(h.calls.length,5,'Refresh and discard cannot race an already-sent annotation');assert.ok(h.api._state.pendingSync);assert.match(h.api._state.notice,/finishing its save/);finish();await syncing;
  }
  {
    const h=harness();typeDraft(h.api);const active={id:'pdf-annotation-after',selectionStart:3,selectionEnd:8,selectionDirection:'backward'},restored={disabled:false,focus(options){this.focusOptions=options;},setSelectionRange(...range){this.range=range;}};
    const previous=h.api._state.dialog.querySelector;h.api._state.dialog.querySelector=selector=>selector===':focus'?active:selector==='#pdf-annotation-after'?restored:previous(selector);h.api._renderSidebar();assert.deepEqual(restored.range,[3,8,'backward']);assert.equal(restored.focusOptions.preventScroll,true,'Background status rendering retains editor focus and caret without scrolling');
  }
  {
    let mode='busy';const h=harness((url,options)=>mode==='busy'?reply({error:'Busy',code:'workspace_busy',retryable:true},409):mode==='invalid'?reply({error:'Replacement annotations overlap.'},400):reply({review:{id:'review-one',revision:2,status:'draft',annotations:JSON.parse(options.body).annotations}}));typeDraft(h.api);wireForm(h.api);await submit(h.api);mode='invalid';await h.api._syncPending();assert.equal(h.api._state.pendingSync.blocked,true);assert.equal(h.api._state.pendingSync.validationError,true);
    await click(h.api,'reject',h.api._state.pendingSync.annotations[0].id);assert.equal(h.api._state.pendingSync.blocked,false,'Correcting the invalid queued annotation resumes safe sync');mode='ok';await h.api._syncPending();assert.equal(h.api._state.pendingSync,null);
  }
  {
    const h=harness((url,options)=>options.method==='POST'?reply({error:'Busy',code:'workspace_busy',retryable:true},409):reply({review:{id:'review-one',revision:2,status:'saved',saved_resume_id:'saved-pdf',annotations:[]}}));typeDraft(h.api);wireForm(h.api);await submit(h.api);await click(h.api,'reload');assert.match(h.sidebar.innerHTML,/Unsynced annotations kept in this tab/);assert.match(h.sidebar.innerHTML,/My unsaved wording/,'Remote finalization never hides the queued local wording');await click(h.api,'close');assert.match(h.sidebar.innerHTML,/Discard & close/,'The saved-review branch offers a way to close retained drafts');
  }
  {
    let finish;const h=harness((url,options)=>options.method==='POST'?reply({error:'Busy',code:'workspace_busy',retryable:true},409):new Promise(resolve=>{finish=()=>resolve(reply({review:{id:'review-one',revision:1,status:'draft',annotations:[]}}));}));typeDraft(h.api);wireForm(h.api);await submit(h.api);const refreshing=click(h.api,'reload');assert.equal(h.api._state.busy,true);await h.api._syncPending();assert.equal(h.calls.length,5,'A scheduled sync waits while a saved-review GET is in flight');finish();await refreshing;assert.equal(h.api._state.pendingSync.annotations[0].after,'My unsaved wording');assert.equal(h.api._state.pendingSync.blocked,false);
  }
  {
    const h=harness();typeDraft(h.api);h.api._state.selection.after='x'.repeat(1200000);h.api._persistDraft();assert.equal(h.api._state.draftStorageError,true,'A recovery copy above the readable bound is never reported as safely persisted');
  }
  console.log('PDF review UI checks passed (35 scenarios).');
}
main().catch(error=>{console.error(error);process.exitCode=1;});

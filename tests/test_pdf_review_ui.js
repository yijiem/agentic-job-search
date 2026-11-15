/* Anchored PDF review invariants. Run with Node's standard library. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../jobflow/web_assets/pdf-review.js'),'utf8');
function harness(fetcher){
  const calls=[],sidebar={innerHTML:''};const context={module:{exports:{}},window:{},URL,Uint8Array,Intl,console,btoa:value=>Buffer.from(value,'binary').toString('base64'),fetch:async(url,options={})=>{if(fetcher)return fetcher(url,options);calls.push({url,payload:options.body?JSON.parse(options.body):undefined,options});return {ok:true,json:async()=>({ok:true,review:{id:'review-one',revision:2,status:'saved',saved_resume_id:'revision-two',annotations:[]},resume:{id:'revision-two'}})};}};
  vm.runInNewContext(source,context,{filename:'pdf-review.js'});const api=context.module.exports;
  api._state.dialog={open:true,querySelector:selector=>selector==='#pdf-review-sidebar'?sidebar:null,querySelectorAll:()=>[]};
  api._state.options={resumeId:'original-one',jobs:[],workspaceId:'workspace-one',csrfToken:'csrf-one'};
  api._state.library={workspace_id:'workspace-one',csrf_token:'csrf-one',pdf_ai:{ai_available:false,ai_reason:'No model is configured.'}};
  api._state.label='Reviewed revision';api._state.document={text:'Built a Python service.\nMaintained C++ tools.'};
  api._state.review={id:'review-one',revision:1,status:'draft',annotations:[]};
  return {api,calls,sidebar};
}
function annotation(overrides={}){return {id:'a-one',kind:'replace',source:'manual',status:'accepted',start:8,end:14,before:'Python',after:'Go',page:1,rects:[[.1,.2,.2,.02]],...overrides};}
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
  console.log('PDF review UI checks passed (11 scenarios).');
}
main().catch(error=>{console.error(error);process.exitCode=1;});

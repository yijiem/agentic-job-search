/* Private resume versions and explicit review. Resume content is never persisted in browser storage. */
(function () {
  'use strict';
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fields=[['first_name','First name','text','given-name'],['last_name','Last name','text','family-name'],['email','Email','email','email'],['phone','Phone','tel','tel'],['location','Location','text','address-level2'],['linkedin','LinkedIn URL','url','url'],['website','Website / portfolio URL','url','url']];
  const ui={data:null,workspace:null,runtime:null,active:false,loading:false,busy:false,error:'',notice:'',applicant:null,applicantDirty:false,resumeId:'',jobId:'',jobIds:[],flow:{},saved:{},sourceText:'',draft:null,label:'',draftText:'',confirmed:false,setDefault:false,editingCopy:false,reviewCache:{},uploading:false,extraJobs:{}};
  const get=id=>(ui.data?.resumes||[]).find(r=>r.id===id);
  const isPDF=resume=>resume?.mime==='application/pdf'||/\.pdf$/i.test(resume?.filename||'');
  async function reviewPDF(resumeId,jobId='',autoSuggest=false){
    if(!window.JobflowPDFReview){message('Reload Jobflow to open the PDF review workspace.',true);draw();return;}
    const flow=ui.flow,workspaceId=ui.data?.workspace_id;
    await window.JobflowPDFReview.open({resumeId,jobId,jobTitle:jobName(jobId),autoSuggest,csrfToken:ui.data?.csrf_token||ui.runtime?.csrfToken,
      workspaceId,onSaved:async resume=>{if(ui.data?.workspace_id!==workspaceId)return;const error=await savedVersion(resume,jobId,flow);await load();message(error||'PDF revision saved in your resume library.',Boolean(error));draw();}});
  }
  const button=(label,action,attrs='',primary=false)=>`<button type="button" class="button${primary?' primary':''}" data-resume-action="${action}" ${attrs}${ui.busy?' disabled':''}>${label}</button>`;
  const date=value=>{try{return value?new Intl.DateTimeFormat('en-US',{month:'short',day:'numeric',year:'numeric'}).format(new Date(value)):'';}catch{return '';}};
  const download=resume=>`<a class="button" href="/api/application-resumes/${encodeURIComponent(resume.id)}" target="_blank" rel="noopener noreferrer">Open file ↗</a>`;
  const roleList=()=>[...(ui.workspace?.jobs||[]),...Object.values(ui.extraJobs)];
  const jobName=id=>{const job=roleList().find(j=>j.id===id);return job?`${job.company} · ${job.title}`:'';};
  function draw(){if(!ui.active)return;const main=document.querySelector('#main');if(main){main.innerHTML=render(ui.workspace);window.JobflowResumeControls?.mount(main);}else ui.runtime?.render?.();}
  function message(text,error=false){ui.notice=error?'':text;ui.error=error?text:'';ui.runtime?.toast?.(text,error);}
  async function request(path,payload){
    const options={cache:'no-store',credentials:'same-origin',headers:{Accept:'application/json'}};
    if(payload){options.method='POST';options.headers['Content-Type']='application/json';options.headers['X-CSRF-Token']=ui.data?.csrf_token||ui.runtime?.csrfToken||'';options.body=JSON.stringify({...payload,...(ui.data?.workspace_id?{workspace_id:ui.data.workspace_id}:{})});}
    const response=await fetch(path,options);let data;
    try{data=await response.json();}catch{throw new Error('The resume library could not be reached. Please try again.');}
    if(!response.ok||data.error)throw new Error(data.error||'This resume change could not be saved.');
    return data;
  }
  function acceptData(data){
    if(ui.data?.workspace_id&&data.workspace_id!==ui.data.workspace_id){ui.applicant=null;ui.applicantDirty=false;ui.draft=null;ui.draftText='';ui.sourceText='';ui.resumeId='';ui.jobId='';ui.jobIds=[];ui.flow={};ui.saved={};ui.reviewCache={};ui.extraJobs={};ui.editingCopy=false;}
    ui.data=data;
    if(!ui.applicantDirty)ui.applicant={...(data.applicant||{})};
    if(!get(ui.resumeId))ui.resumeId=data.default_resume_id||data.resumes?.[0]?.id||'';
  }
  async function load(sync=false){
    if(ui.loading)return;ui.loading=true;
    try{let syncError='';acceptData(await request('/api/resumes'));if(sync){try{await request('/api/action',{action:'batch',op:'resume_sync'});acceptData(await request('/api/resumes'));}catch(error){syncError='Saved resume restoration is postponed. '+error.message;}}if(ui.jobId&&!roleList().some(j=>j.id===ui.jobId)){const detail=await request('/api/jobs/'+encodeURIComponent(ui.jobId));ui.extraJobs[detail.job.id]=detail.job;}ui.error=syncError;}
    catch(error){message(error.message,true);}
    finally{ui.loading=false;draw();}
  }
  async function mutate(payload,notice){
    if(ui.busy)return null;ui.busy=true;ui.error='';
    try{const result=await request('/api/action',{action:'batch',...payload});if(notice)message(notice);return result;}
    catch(error){message(error.message,true);return null;}
    finally{ui.busy=false;}
  }
  function resumeOptions(selected){return '<option value="">Choose a saved resume</option>'+ (ui.data?.resumes||[]).map(r=>`<option value="${esc(r.id)}"${r.id===selected?' selected':''} data-title="${esc(r.label||r.filename)}" data-filename="${esc(r.filename)}" data-type="${esc((r.filename||'').split('.').pop().toUpperCase())}" data-size="${Math.max(1,Math.ceil(Number(r.size||0)/1024))} KB" data-default="${r.id===ui.data.default_resume_id}" data-version="${esc(r.version||1)}">${esc(r.label||r.filename)}${r.id===ui.data.default_resume_id?' · default':''}</option>`).join('');}
  function renderLibrary(){
    const resumes=ui.data?.resumes||[];
    return `<section class="card resume-card"><div class="card-head"><div><h2>Your resume library</h2><p>${resumes.length} saved version${resumes.length===1?'':'s'} · reusable across applications</p></div>${button('Restore earlier uploads','restore')}</div><div class="card-body"><form id="resume-upload-form" class="resume-upload"><h3>Add resumes</h3><p>Keep different versions for different roles. PDF, DOCX, TXT or Markdown · up to 5 MB per file.</p><label class="sr-only" for="resume-library-files">Choose resume files</label><input id="resume-library-files" type="file" data-resume-upload data-upload-title="Choose resume files" data-upload-hint="PDF, DOCX, TXT or Markdown · up to 5 MB each" accept=".pdf,.docx,.txt,.md" multiple required><button class="button" type="submit"${ui.busy?' disabled':''}>Upload to library</button></form><div class="resume-list">${resumes.map(r=>`<article class="resume-version${r.id===ui.data.default_resume_id?' is-default':''}"><div class="resume-version-heading"><div><h3>${esc(r.label||r.filename)}</h3><div class="resume-meta">${esc(r.filename)} · ${Math.max(1,Math.ceil(Number(r.size||0)/1024))} KB<br>${esc(date(r.created_at))}${r.version?' · Version '+esc(r.version):''}</div></div>${r.id===ui.data.default_resume_id?'<span class="chip green">Default</span>':''}</div>${r.parent_resume_id?`<p class="resume-meta">Based on ${esc(get(r.parent_resume_id)?.label||get(r.parent_resume_id)?.filename||'an earlier saved version')}</p>`:''}${r.job_id?`<p class="resume-meta">For ${esc(jobName(r.job_id)||'a saved role')}</p>`:''}<div class="button-group">${isPDF(r)?button('Review PDF','view-pdf',`data-resume-id="${esc(r.id)}"`,true):''}${download(r)}${button('Edit a copy','edit',`data-resume-id="${esc(r.id)}"`)}${r.id!==ui.data.default_resume_id?button('Use as default','default',`data-resume-id="${esc(r.id)}"`):''}</div></article>`).join('')||'<div class="resume-empty"><p>Upload your first resume here. It will be available when you apply to one role or a batch.</p></div>'}</div></div></section>`;
  }
  function renderApplicant(){return `<section class="card resume-card"><div class="card-head"><div><h2>Application details</h2><p>Saved once, filled into new applications.</p></div></div><form id="resume-applicant-form" class="card-body"><div class="resume-contact-fields">${fields.map(([key,label,type,autocomplete])=>`<div class="field"><label for="resume-contact-${key}">${label}</label><input class="input" id="resume-contact-${key}" data-resume-applicant="${key}" type="${type}" autocomplete="${autocomplete}" maxlength="1000" value="${esc(ui.applicant?.[key]||'')}"></div>`).join('')}</div><div class="resume-actions"><span class="tiny muted">Review the employer’s questions before submitting.</span><button type="submit" class="button"${ui.busy?' disabled':''}>Save application details</button></div></form></section>`;}
  function draftList(){const drafts=(ui.data?.drafts||[]).filter(d=>d.status==='pending'&&d.id!==ui.draft?.id);return drafts.length?`<details><summary>${drafts.length} saved review draft${drafts.length===1?'':'s'}</summary><div class="resume-draft-list">${drafts.map(d=>button(esc(jobName(d.job_id)||get(d.resume_id)?.label||'Resume copy'),'open-draft',`data-draft-id="${esc(d.id)}"`)).join('')}</div></details>`:'';}
  function cacheDraft(){if(ui.draft)ui.reviewCache[ui.draft.id]={label:ui.label,text:ui.draftText};}
  function renderContext(){
    if(!ui.jobId||ui.editingCopy)return '';
    const index=ui.jobIds.indexOf(ui.jobId),count=ui.jobIds.length;
    return `<section class="card resume-card"><div class="card-body"><span class="tiny muted">${count>1?`Application role ${index+1} of ${count}`:'Selected application role'}</span><h2>${esc(jobName(ui.jobId)||'Selected role')}</h2>${ui.saved[ui.jobId]?`<p role="status">Saved version: ${esc(ui.saved[ui.jobId].label||ui.saved[ui.jobId].filename)}</p>`:''}${count>1?`<p>Review a separate resume for each role in your selection. ${Object.keys(ui.saved).length} of ${count} saved.</p><div class="button-group">${button('Previous role','previous-role',index<=0?'disabled':'')}${button('Next role','next-role',index>=count-1?'disabled':'')}</div>`:''}</div></section>`;
  }
  function renderResumeHelp(){return `<section class="card resume-card"><div class="card-head"><div><h2>Tailor from your applications</h2><p>Select a role or a batch to start an application, then tailor your resume there. Your selected roles carry through automatically.</p></div></div><div class="card-body">${draftList()}<div class="button-group"><a class="button" href="#jobs">Explore roles</a><a class="button" href="#batches">Application batches</a></div></div></section>`;}
  function renderTailor(){
    const resume=get(ui.resumeId);
    const needsText=(resume?.editable===false||resume?.extraction_status==='needs_text')&&(ui.editingCopy||!isPDF(resume)||!window.JobflowPDFReview);
    return `<section class="card resume-card"><div class="card-head"><div><h2>${ui.editingCopy?'Edit a resume copy':'Tailor your resume'}</h2><p>${ui.editingCopy?'Paste the text, then review and save a separate version.':'Bring the most relevant experience forward for a role.'}</p></div></div><div class="card-body"><div class="resume-workflow"><span class="current">1 · Choose a resume</span><span>2 · Review and edit</span><span>3 · Save a version</span></div>${draftList()}<form id="resume-tailor-form"><div class="field"><label for="resume-base">Start from</label><select id="resume-base" data-resume-picker data-resume-choice="resumeId" required>${resumeOptions(ui.resumeId)}</select></div>${needsText?`<div class="field"><label for="resume-source-text">Text from this resume</label><textarea id="resume-source-text" data-resume-choice="sourceText" rows="8" maxlength="200000" placeholder="Paste the text from your saved PDF to prepare an editable copy.">${esc(ui.sourceText)}</textarea><span class="tiny muted">Your uploaded file stays intact. The pasted text will be included in the draft for your review.</span></div>`:''}<div class="resume-review-note"><p>The assistant uses your saved resume and the recorded job description to propose a focused draft and show the evidence behind it. You review or edit the text before saving a new version.</p></div><button type="submit" class="button primary"${ui.busy||!resume?' disabled':''}>${ui.busy?'Preparing draft…':ui.editingCopy?'Prepare editable copy':isPDF(resume)?'Review PDF & tailor':'Prepare tailored draft'}</button></form></div></section>`;
  }
  function renderReview(){
    const draft=ui.draft,base=get(draft.resume_id),changes=draft.changes||[];
    return `<section class="card resume-card resume-review"><div class="card-head"><div><h2>Review your resume draft</h2><p>${esc(jobName(draft.job_id)||'An editable copy of your saved resume')}</p></div>${button('Back','close-draft')}</div><form id="resume-review-form" class="card-body"><div class="resume-workflow"><span>1 · Choose a resume</span><span class="current">2 · Review and edit</span><span>3 · Save a version</span></div><div class="resume-review-note"><p><strong>${draft.job_id?'Suggested changes':'Edit a separate copy'}</strong></p>${draft.method==='local_extractive'?'<p>Prepared locally from your existing text, using the recorded role to choose relevant wording. No new achievements or metrics are generated.</p>':''}${changes.length?`<ul class="resume-changes">${changes.map(change=>`<li>${esc(change)}</li>`).join('')}</ul>`:'<p>Edit the text below, then save it as a separate version.</p>'}${draft.matched_skills?.length?`<p>Relevant skills in your evidence: ${esc(draft.matched_skills.join(', '))}.</p>`:''}${draft.missing_skills?.length?`<p>Not established by this resume: ${esc(draft.missing_skills.join(', '))}. Add only experience you can support.</p>`:''}</div><details><summary>Compare with ${esc(base?.label||base?.filename||'the original')}</summary><pre class="resume-original">${esc(draft.original_text||'No text was extracted. Review the original file below.')}</pre>${base?download(base):''}</details>${draft.sources?.length?`<details><summary>Draft sources</summary><ul class="resume-changes">${draft.sources.map(s=>{let link='';try{const url=new URL(s.url);if(['http:','https:'].includes(url.protocol))link=url.href;}catch{}return `<li>${link?`<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">${esc(s.label||s.title||'Source')} ↗</a>`:esc(s.label||s.title||'Saved resume')}</li>`;}).join('')}</ul></details>`:''}<div class="field"><label for="resume-version-label">Version name</label><input class="input" id="resume-version-label" data-resume-review="label" value="${esc(ui.label)}" maxlength="200" required></div><div class="field"><label for="resume-version-text">Resume text · edit before saving</label><textarea id="resume-version-text" data-resume-review="draftText" maxlength="200000" required>${esc(ui.draftText)}</textarea></div><label class="resume-check"><input id="resume-reviewed" data-resume-review="confirmed" type="checkbox"${ui.confirmed?' checked':''}> I reviewed this draft and confirm the experience and details are accurate.</label><label class="resume-check"><input data-resume-review="setDefault" type="checkbox"${ui.setDefault?' checked':''}> Use this version by default for new applications</label><div class="resume-actions"><span class="tiny muted">Saves a new DOCX. Your original stays in the library.</span><button id="resume-save-version" class="button primary" type="submit"${ui.busy||!ui.confirmed?' disabled':''}>Confirm & save version</button></div></form></section>`;
  }
  function render(workspace){ui.workspace=workspace;return `<div class="page-header"><div><h1>Resumes & profile</h1><p>Your experience, ready for the right opportunity.</p></div><div class="header-actions">${ui.flow.onReturn?button(esc(ui.flow.returnLabel||'Back to application'),'return-application'):'<a class="button" href="#batches">Use in an application ↗</a>'}</div></div><div class="resume-live-message" role="status" aria-live="polite">${esc(ui.notice)}</div>${ui.error?`<div class="resume-warning" role="alert">${esc(ui.error)} ${button('Refresh library','reload')}</div>`:''}${ui.loading&&!ui.data?'<div class="loading"><span class="spinner"></span>Opening your resume library…</div>':`<div class="resume-layout"><div class="resume-stack">${renderLibrary()}</div><div class="resume-stack">${renderContext()}${ui.draft?renderReview():ui.jobId||ui.editingCopy?renderTailor():renderResumeHelp()}${renderApplicant()}</div></div>`}`;}
  function chooseDraft(draft){ui.draft=draft;ui.resumeId=draft.resume_id;ui.label=draft.label||((get(draft.resume_id)?.label||get(draft.resume_id)?.filename||'Resume').replace(/\.[^.]+$/,'')+' · '+(jobName(draft.job_id)||'Edited copy'));ui.label=ui.label.slice(0,200);ui.draftText=draft.text||'';ui.confirmed=false;ui.setDefault=false;const cached=ui.reviewCache[draft.id];if(cached){ui.label=cached.label;ui.draftText=cached.text;}}
  async function prepareDraft(resumeId,jobId=''){
    const result=await mutate({op:'resume_draft',resume_id:resumeId,...(jobId?{job_id:jobId}:{}),...(ui.sourceText?{source_text:ui.sourceText}:{})},'Draft ready for your review.');
    if(result?.draft){chooseDraft(result.draft);await load();}else draw();
  }
  async function savedVersion(resume,jobId,flow=ui.flow){
    if(!resume||flow!==ui.flow||!ui.jobIds.includes(jobId))return '';
    ui.saved[jobId]=resume;
    try{await flow.onSaved?.(resume,jobId);return '';}
    catch(error){return 'Your resume version was saved, but could not be added to the application. '+error.message;}
  }
  async function click(event){const node=event.target.closest?.('[data-resume-action]');if(!node||ui.busy||ui.uploading)return;const op=node.dataset.resumeAction;
    if(op==='reload')return load();
    if(op==='restore')return load(true);
    if(op==='view-pdf')return reviewPDF(node.dataset.resumeId);
    if(op==='return-application'){cacheDraft();ui.flow.onReturn?.();return;}
    if(op==='previous-role'||op==='next-role'){const index=ui.jobIds.indexOf(ui.jobId)+(op==='next-role'?1:-1);if(index<0||index>=ui.jobIds.length)return;cacheDraft();ui.jobId=ui.jobIds[index];ui.draft=null;ui.confirmed=false;ui.editingCopy=false;ui.notice='';await load();return;}
    if(op==='close-draft'){ui.reviewCache[ui.draft.id]={label:ui.label,text:ui.draftText};ui.draft=null;ui.editingCopy=false;draw();return;}
    if(op==='open-draft'){cacheDraft();try{const result=await request('/api/resume-drafts/'+encodeURIComponent(node.dataset.draftId));const draft=result.draft||result;if(!ui.jobIds.includes(draft.job_id))start(draft.job_id||'');ui.jobId=draft.job_id||'';ui.editingCopy=!ui.jobId;chooseDraft(draft);ui.error='';}catch(error){message(error.message,true);}draw();return;}
    if(op==='default'){const result=await mutate({op:'resume_default',resume_id:node.dataset.resumeId},'Default resume saved for future applications.');if(result)await load();else draw();return;}
    if(op==='edit'){
      start();ui.resumeId=node.dataset.resumeId;ui.editingCopy=true;const resume=get(ui.resumeId);
      if(resume?.editable===false||resume?.extraction_status==='needs_text'){ui.jobId='';ui.draft=null;ui.editingCopy=true;message('Paste the text from this file below to prepare an editable copy.');draw();return;}
      await prepareDraft(ui.resumeId);
    }
  }
  function capture(event){const node=event.target;if(node.dataset?.resumeApplicant){ui.applicant||={};ui.applicant[node.dataset.resumeApplicant]=node.value;ui.applicantDirty=true;}
    if(node.dataset?.resumeChoice){const key=node.dataset.resumeChoice;if(!['resumeId','sourceText'].includes(key))return;ui[key]=node.value;if(event.type==='change'&&key==='resumeId'){ui.sourceText='';draw();}}
    if(node.dataset?.resumeReview){const key=node.dataset.resumeReview;ui[key]=node.type==='checkbox'?node.checked:node.value;if(['label','draftText'].includes(key))ui.confirmed=false;const checked=document.querySelector('#resume-reviewed');if(checked)checked.checked=ui.confirmed;const save=document.querySelector('#resume-save-version');if(save)save.disabled=ui.busy||!ui.confirmed;}
  }
  async function readFile(file){if(!file?.size||file.size>5*1024*1024||!(/\.(pdf|docx|txt|md)$/i).test(file.name))throw new Error('Choose non-empty PDF, DOCX, TXT or Markdown files up to 5 MB each.');return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',',2)[1]);reader.onerror=()=>reject(new Error('Could not read '+file.name));reader.readAsDataURL(file);});}
  async function submit(event){const id=event.target.id;if(!['resume-upload-form','resume-applicant-form','resume-tailor-form','resume-review-form'].includes(id))return;event.preventDefault();if(ui.busy||ui.uploading)return;
    if(id==='resume-applicant-form'){const result=await mutate({op:'resume_profile',applicant:ui.applicant||{}},'Application details saved.');if(result){ui.applicantDirty=false;await load();}else draw();}
    else if(id==='resume-tailor-form'){if(!ui.resumeId||(!ui.jobId&&!ui.editingCopy))return;if(isPDF(get(ui.resumeId))&&window.JobflowPDFReview)return reviewPDF(ui.resumeId,ui.editingCopy?'':ui.jobId,!ui.editingCopy);await prepareDraft(ui.resumeId,ui.editingCopy?'':ui.jobId);}
    else if(id==='resume-review-form'){if(!ui.draft||!ui.confirmed||!ui.draftText.trim()||!ui.label.trim())return;const draft=ui.draft,flow=ui.flow;const result=await mutate({op:'resume_save_version',draft_id:draft.id,text:ui.draftText,label:ui.label.trim(),confirmed:true,set_default:ui.setDefault},'New resume version saved and ready to use in applications.');if(result){delete ui.reviewCache[draft.id];ui.draft=null;ui.draftText='';ui.confirmed=false;const error=await savedVersion(result.resume,draft.job_id,flow);await load();if(error){message(error,true);draw();}}else draw();}
    else {
      const files=Array.from(document.querySelector('#resume-library-files')?.files||[]);if(!files.length)return;
      let added=0;ui.uploading=true;
      try{for(const file of files){const content=await readFile(file);const result=await mutate({op:'upload_resume',filename:file.name,content_base64:content});if(!result)break;added++;}if(added){if(ui.error)message(`${added} resume${added===1?'':'s'} saved. `+ui.error,true);else message(`${added} resume${added===1?'':'s'} saved in your library.`);}}
      catch(error){message((added?`${added} file${added===1?'':'s'} saved. `:'')+error.message,true);}
      ui.uploading=false;const error=ui.error;await load();if(error){ui.error=error;draw();}
    }
  }
  function mount(runtime){ui.runtime=runtime;const wasActive=ui.active;ui.active=true;if(!wasActive)load();}
  function unmount(){ui.active=false;}
  function start(jobIds=[],options={}){cacheDraft();ui.jobIds=[...new Set((Array.isArray(jobIds)?jobIds:[jobIds]).filter(id=>typeof id==='string'&&id))];ui.jobId=ui.jobIds[0]||'';ui.flow={...options};ui.saved={};ui.draft=null;ui.sourceText='';ui.editingCopy=false;ui.confirmed=false;ui.notice='';ui.error='';if(options.resumeId)ui.resumeId=options.resumeId;if(ui.active)load();}
  document.addEventListener('click',click);document.addEventListener('input',capture);document.addEventListener('change',capture);document.addEventListener('submit',submit);
  window.JobflowResumes={render,mount,unmount,start};
})();

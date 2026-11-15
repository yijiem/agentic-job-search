/* Browser-assisted application batches. Durable state lives in the workspace.
 * This client never contacts an employer or infers an application was sent. */
(function () {
  'use strict';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels = {pending:'Waiting for form inspection',inspecting:'Inspecting form',ready:'Ready for your review',needs_input:'Needs your attention',queued:'Authorized · queued',running:'Filling application',submitting:'Checking submission',submitted:'Submission confirmed',uncertain:'Outcome needs checking',failed:'Could not continue',skipped:'Skipped'};
  const editable = new Set(['pending','ready','needs_input','failed']);
  const terminal = new Set(['submitted','skipped']);
  const applicantFields = [['first_name','First name','text'],['last_name','Last name','text'],['email','Email','email'],['phone','Phone','tel'],['location','Location','text'],['linkedin','LinkedIn URL','url'],['website','Website / portfolio URL','url']];
  const ui = {data:null,workspace:null,runtime:null,active:false,loading:false,busy:false,error:'',notice:'',selected:'',creating:false,seed:[],pendingStart:null,timer:null,draft:null,edits:{},materialEdits:{},editRevisions:{},handoff:null,handoffReview:null,details:{},focus:null};
  const button = (text, action, extra='', primary=false) => `<button type="button" class="button${primary?' primary':''}" data-batch-action="${action}" ${extra}${ui.busy?' disabled':''}>${text}</button>`;
  const safeLink = value => {try {const u=new URL(value);return ['https:','http:'].includes(u.protocol)?u.href:'';}catch{return '';}};
  const codexLink = value => {try {const u=new URL(value);return u.protocol==='codex:'&&u.hostname==='new'&&!u.username&&!u.password&&['','/'].includes(u.pathname)&&u.searchParams.has('path')&&u.searchParams.has('prompt')?u.href:'';}catch{return '';}};
  const formatTime = value => {try{return value?new Intl.DateTimeFormat('en-US',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}).format(new Date(value)):'Not yet connected';}catch{return 'Time unavailable';}};
  const getBatch = () => (ui.data?.batches||[]).find(b=>b.id===ui.selected);
  const resumeById = id => (ui.data?.resumes||[]).find(r=>r.id===id);
  const resumePreview = id => {const resume=resumeById(id);return resume?`<a class="text-link batch-resume-preview" href="/api/application-resumes/${encodeURIComponent(resume.id)}" target="_blank" rel="noopener noreferrer">Preview ${escape(resume.filename)} ↗</a>`:'';};
  const activeRevision = batch => ui.editRevisions[batch.id] ?? batch.revision;
  const hasEdits = () => Boolean(ui.creating&&ui.draft?.dirty || Object.keys(ui.edits).length || Object.keys(ui.materialEdits).length);
  const notify = (text, error=false) => {ui.runtime?.toast?.(text,error);};
  const all = (root,selector) => Array.from(root?.querySelectorAll?.(selector)||[]);
  const detailAttrs = (key,defaultOpen=false) => `data-batch-details="${escape(key)}"${(ui.details[key]??defaultOpen)?' open':''}`;
  function focusIdentity(node) {
    if(!node)return null;
    if(node.id)return {kind:'id',value:node.id};
    for(const key of ['batchOpen','batchAction'])if(node.dataset?.[key])return {kind:key,value:node.dataset[key],job:node.dataset.jobId||''};
    if(node.tagName==='SUMMARY')return {kind:'summary',value:node.closest?.('[data-batch-details]')?.dataset.batchDetails};
    if(node.type==='submit') {const form=node.closest?.('form');return form?{kind:'submit',value:form.id||form.dataset.batchReview}:null;}
    return null;
  }
  function rememberView() {
    const root=document.querySelector('#main');
    for(const detail of all(root,'details[data-batch-details]'))ui.details[detail.dataset.batchDetails]=detail.open;
    const focused=document.activeElement;
    if(root?.contains?.(focused)) {
      const identity=focusIdentity(focused);
      ui.focus=identity?{identity,start:focused.selectionStart,end:focused.selectionEnd}:null;
    } else ui.focus=null;
  }
  function restoreFocus() {
    const snapshot=ui.focus;ui.focus=null;if(!snapshot)return;
    const root=document.querySelector('#main'),identity=snapshot.identity;
    const node=all(root,'input,select,textarea,button,a,summary').find(candidate=>{
      const candidateId=focusIdentity(candidate);
      return candidateId&&candidateId.kind===identity.kind&&candidateId.value===identity.value&&(candidateId.job||'')===(identity.job||'');
    });
    if(!node||node.disabled)return;
    node.focus?.({preventScroll:true});
    if(Number.isInteger(snapshot.start)&&Number.isInteger(snapshot.end))try{node.setSelectionRange(snapshot.start,snapshot.end);}catch{/* Some input types do not have a text selection. */}
  }
  function draw() {
    if(!ui.active)return;
    const root=document.querySelector('#main');
    if(root&&ui.workspace){root.innerHTML=render(ui.workspace);window.JobflowResumeControls?.mount(root);restoreFocus();}
    else ui.runtime?.render?.();
  }
  function updateCodexProgress() {
    const updates=document.querySelector('[data-batch-codex-progress]'),batch=getBatch(),html=batch?codexProgress(batch):'';
    if(updates&&updates.innerHTML!==html)updates.innerHTML=html;
  }
  const contentFingerprint = data => JSON.stringify(data?{workspace_id:data.workspace_id,batches:data.batches,resumes:data.resumes,applicant:data.applicant,default_resume_id:data.default_resume_id,codex:data.codex}:null);
  function newDraft() {
    return {name:'',preset:ui.seed.length?'selected':'daily',company:ui.workspace?.company||'',count:5,date:ui.workspace?.today||'',resume_id:ui.data?.default_resume_id||ui.data?.resumes?.[0]?.id||'',applicant:{...(ui.data?.applicant||{})},dirty:false,applicantTouched:{},resumeTouched:false,resumeOverrides:{}};
  }
  function eligibility(job, applications, batches, currentBatch='') {
    const application = applications.find(a=>(a.job_id||a.id)===job.id);
    if(application && (application.stage!=='saved'||application.applied_at)) return 'Already tracked as '+(application.stage||'submitted');
    if(job.availability==='closed') return 'Listing recorded closed';
    if(job.availability==='missing') return 'Listing needs rechecking';
    if(!safeLink(job.url) || !String(job.url).startsWith('https://')) return 'A verified HTTPS application link is required';
    if((batches||[]).some(b=>b.id!==currentBatch && (b.items||[]).some(i=>i.job_id===job.id && i.status!=='skipped' && (b.status!=='cancelled'||['submitting','submitted','uncertain'].includes(i.status))))) return 'Already in an active batch or retained submission';
    return '';
  }
  function selectRoles(workspace, draft, batches=[], seed=[]) {
    const jobs=workspace?.jobs||[],apps=workspace?.applications||[];
    let candidates;
    if(draft.preset==='company') candidates=jobs.filter(j=>j.company===draft.company).sort((a,b)=>Number(b.score||0)-Number(a.score||0)||String(a.id).localeCompare(String(b.id)));
    else if(draft.preset==='daily') candidates=jobs.filter(j=>(j.first_seen_date||workspace.localDay?.(j.first_seen)||'')===draft.date).sort((a,b)=>Number(b.score||0)-Number(a.score||0));
    else candidates=seed.map(id=>jobs.find(j=>j.id===id)).filter(Boolean);
    const excluded=[],eligible=[];
    for(const job of candidates){const reason=eligibility(job,apps,batches);if(reason)excluded.push({job,reason});else eligible.push(job);}
    const limit=draft.preset==='company'?Math.min(100,Math.max(1,Number(draft.count)||5)):100;
    return {roles:eligible.slice(0,limit),excluded,total:eligible.length,truncated:Math.max(0,eligible.length-limit)};
  }
  function resumeOptions(value, inherit=false) {
    return `${inherit?'<option value="">Use batch resume</option>':'<option value="">Choose a resume</option>'}${(ui.data?.resumes||[]).map(r=>`<option value="${escape(r.id)}"${r.id===value?' selected':''} data-title="${escape(r.label||r.filename)}" data-filename="${escape(r.filename)}" data-type="${escape((r.filename||'').split('.').pop().toUpperCase())}" data-size="${Math.max(1,Math.ceil(Number(r.size||0)/1024))} KB" data-default="${r.id===ui.data?.default_resume_id}" data-version="${escape(r.version||1)}">${escape(r.label||r.filename)}${r.id===ui.data?.default_resume_id?' · default':''} · ${Math.ceil(Number(r.size||0)/1024)} KB</option>`).join('')}`;
  }
  function applicantInputs(values={}, prefix='create') {
    return `<div class="batch-fields">${applicantFields.map(([key,label,type])=>`<div class="field"><label for="${prefix}-${key}">${label}${['first_name','last_name','email'].includes(key)?' *':''}</label><input id="${prefix}-${key}" class="input" type="${type}" name="${key}" data-batch-applicant="${key}" value="${escape(values[key]||'')}" maxlength="1000" ${['first_name','last_name','email'].includes(key)?'required':''} autocomplete="${{first_name:'given-name',last_name:'family-name',email:'email',phone:'tel'}[key]||'off'}"></div>`).join('')}</div>`;
  }
  function codexHelp() {
    return `<section class="batch-codex card"><h2>Apply with Codex</h2><p>Choose a batch and review its saved materials, then open a prepared request in Codex. Codex inspects employer forms, asks for missing answers, and reports progress and actual confirmations here.</p><p class="tiny muted">Press Send in Codex to start. Keep your browser available for sign-in or verification. ${ui.data?.codex?.available===false?escape(ui.data.codex.reason||'Open your local Jobflow workspace to use Codex.'):''}</p></section>`;
  }
  function codexItems(batch) {
    return (batch.items||[]).filter(item=>['pending','ready','needs_input','failed','queued'].includes(item.status));
  }
  const reservedRun = batch => batch.codex_run&&!['complete','cancelled'].includes(batch.codex_run.status)?batch.codex_run:null;
  const canEditItem = (batch,item) => editable.has(item.status)&&!['cancelled','complete'].includes(batch.status)&&!reservedRun(batch)?.job_ids?.includes(item.job_id);
  function canPrepareCodex(batch) {
    return Boolean(batch&&ui.data?.codex?.available===true&&!reservedRun(batch)?.started&&!hasEdits()&&!ui.busy&&!['cancelled','paused','complete'].includes(batch.status)&&codexItems(batch).length);
  }
  function codexProgress(batch) {
    const run=batch.codex_run,status=run?.status,started=Boolean(reservedRun(batch)?.started);
    const names={awaiting_start:'Waiting for you to start in Codex',running:'Codex is working',needs_input:'Codex needs your answers',ready:'Codex has inspected the form',uncertain:'Check the application outcome',paused:'Codex run paused',complete:'Codex run finished',failed:'Codex could not continue',cancelled:'Codex run cancelled'};
    const explanation=status==='awaiting_start'?'The request is prepared. Open it in Codex and press Send to begin.':status==='running'?'Follow the application steps in your Codex chat. Results will appear here as Codex records them.':status==='needs_input'?'Answer the missing questions in your original Codex chat, then continue there.':status==='complete'?'Check each role below for its confirmed result or next step.':status==='uncertain'?'Check the actual employer confirmation before continuing. Jobflow will not automatically resubmit.':'';
    return `${run?`<div class="batch-codex-status" role="status" aria-live="polite"><strong>${escape(names[status]||status||'Codex request prepared')}</strong>${explanation?`<p>${explanation}</p>`:''}${run.message?`<p>${escape(run.message)}</p>`:''}</div>`:''}${started?'<p class="batch-note">Continue in the original Codex chat for this run. A new chat cannot take over work that has already started.</p>':`<div class="button-group">${button(status==='awaiting_start'?'Open prepared request':'Open in Codex','codex-review',canPrepareCodex(batch)?'':'disabled',true)}${status==='awaiting_start'?button('Cancel prepared request','codex-discard'):''}</div>`}${status==='awaiting_start'?'<p class="batch-note">Materials are reserved for this request. Cancel the prepared request to edit them before starting.</p>':''}${hasEdits()?'<p class="batch-note">Save your edited answers and materials before continuing in Codex.</p>':!codexItems(batch).length&&!started?'<p class="batch-note">No roles are ready for a new request. Resolve uncertain outcomes before trying those applications again.</p>':''}`;
  }
  function reviewCodex(batch) {
    if(!canPrepareCodex(batch))return;
    const items=codexItems(batch),excluded=(batch.items||[]).length-items.length;
    ui.handoffReview={batchId:batch.id,revision:batch.revision,workspaceId:ui.data.workspace_id,jobIds:items.map(item=>item.job_id)};
    ui.runtime?.showDialog?.(`<h2>Review your Codex request</h2><p>${escape(batch.name)} · ${items.length} role${items.length===1?'':'s'}</p><p>Codex asks for missing answers or required consent, then fills and submits the applications you authorize. Pressing Send in Codex authorizes uploading these reviewed materials and your supplied answers to the listed employers.</p><div class="batch-handoff-roles">${items.map(item=>{const id=item.resume_id||batch.resume_id,resume=resumeById(id);return `<article><strong>${escape(item.company)} · ${escape(item.title)}</strong><p>Resume: ${escape(resume?.label||resume?.filename||'Unavailable')}</p>${resumePreview(id)}</article>`;}).join('')}</div><p class="batch-note">Applicant: ${escape([batch.applicant?.first_name,batch.applicant?.last_name].filter(Boolean).join(' '))} · ${escape(batch.applicant?.email||'Email not saved')}</p>${excluded?`<p class="batch-note">${excluded} role${excluded===1?' is':'s are'} excluded because work is in progress, an outcome needs checking, or it is already finished.</p>`:''}<p class="batch-note">Preparing this request reserves these roles for Codex. No application is sent by this button.</p><div id="form-error" role="alert"></div><div class="button-group">${button('Prepare request','codex-prepare','',true)}</div>`);
  }
  function showCodexHandoff(result,batchId) {
    if(result.run?.started){ui.handoff=null;ui.runtime?.showDialog?.('<h2>Continue in your Codex chat</h2><p>This run has already started. Return to the original Codex chat to answer questions and continue the applications. Progress remains visible in this batch.</p>');return;}
    const link=codexLink(result.launch_url),prompt=typeof result.prompt==='string'?result.prompt:'';
    ui.handoff={batchId,runId:result.run?.id,workspaceId:ui.data?.workspace_id,prompt,launch_url:link};
    ui.runtime?.showDialog?.(`<h2>Your Codex request is ready</h2><p>Opens a prepared request. Press Send in Codex to start. Keep your browser available. No application is sent by this button.</p><p class="batch-note">Sending the request in Codex authorizes uploading the reviewed materials and supplied answers to the listed employers and submitting those applications. Codex asks for missing answers or required consent.</p><div class="button-group">${link?`<a class="button primary" href="${escape(link)}">Open in Codex</a>`:'<span class="batch-note">The Codex link is unavailable. Copy the request into Codex instead.</span>'}${prompt?button('Copy request','codex-copy'):''}</div>${prompt?`<details class="batch-handoff-fallback"><summary>View request to copy manually</summary><label class="sr-only" for="batch-codex-prompt">Prepared Codex request</label><textarea id="batch-codex-prompt" readonly rows="7">${escape(prompt)}</textarea></details>`:''}<p class="batch-note" data-batch-copy-status role="status" aria-live="polite">Jobflow will show “Codex is working” only after Codex starts the run.</p>`);
  }
  function renderCreate() {
    const draft=ui.draft||(ui.draft=newDraft()), selection=selectRoles(ui.workspace,draft,ui.data?.batches,ui.seed);
    const companies=[...new Set((ui.workspace?.jobs||[]).map(j=>j.company))].sort();
    return `<section class="card batch-compose"><div class="card-head"><div><h2>Create an application batch</h2><p>Choose roles and materials to review with Codex.</p></div>${button('Close','close-create')}</div><form id="application-batch-create" class="card-body"><div class="batch-fields"><div class="field"><label for="batch-name">Batch name</label><input id="batch-name" class="input" name="name" data-batch-draft="name" value="${escape(draft.name)}" maxlength="120" placeholder="e.g. This week’s infrastructure roles"></div><div class="field"><label for="batch-preset">Role group</label><select id="batch-preset" data-batch-draft="preset"><option value="selected"${draft.preset==='selected'?' selected':''}>Selected roles (${ui.seed.length})</option><option value="company"${draft.preset==='company'?' selected':''}>Top roles at a company</option><option value="daily"${draft.preset==='daily'?' selected':''}>New daily roles</option></select></div>${draft.preset==='company'?`<div class="field"><label for="batch-company">Company</label><select id="batch-company" data-batch-draft="company"><option value="">Choose a company</option>${companies.map(c=>`<option${draft.company===c?' selected':''}>${escape(c)}</option>`).join('')}</select></div><div class="field"><label for="batch-count">Number of top matches</label><input id="batch-count" class="input" type="number" min="1" max="100" step="1" data-batch-draft="count" value="${escape(draft.count)}"></div>`:''}${draft.preset==='daily'?`<div class="field"><label for="batch-day">Discovery date</label><input id="batch-day" class="input" type="date" data-batch-draft="date" value="${escape(draft.date)}" max="${escape(ui.workspace?.today||'')}"></div>`:''}</div><div class="batch-selection"><strong>${selection.roles.length} roles in this batch</strong><p class="tiny muted">${draft.preset==='daily'?'Discovery date uses your workspace timezone. ':''}Previously submitted roles, roles needing a listing recheck, and active-batch duplicates are excluded.</p>${selection.roles.length?`<div class="batch-role-preview">${selection.roles.map(j=>`<div><strong>${escape(j.company)}</strong><span>${escape(j.title)}${draft.resumeOverrides?.[j.id]?`<small class="tiny muted">Resume: ${escape(resumeById(draft.resumeOverrides[j.id])?.label||resumeById(draft.resumeOverrides[j.id])?.filename||'Tailored version')} ${button('Use batch resume','clear-tailored',`data-job-id="${escape(j.id)}"`)}</small>`:''}</span><span class="chip">${escape(j.score??'—')}</span></div>`).join('')}</div>`:'<p>Choose a company or date with eligible roles, or select roles in Explore roles.</p>'}${selection.truncated?`<p class="tiny muted">${selection.truncated} additional eligible roles are outside this group’s limit.</p>`:''}${selection.excluded.length?`<details ${detailAttrs('create-exclusions')}><summary>${selection.excluded.length} excluded role${selection.excluded.length===1?'':'s'}</summary><ul>${selection.excluded.map(({job,reason})=>`<li><strong>${escape(job.company)} · ${escape(job.title)}</strong><br>${escape(reason)}</li>`).join('')}</ul></details>`:''}</div><div class="batch-materials-heading"><h3>Application materials</h3><a class="text-link" href="#resumes">Manage resumes & profile →</a></div><p class="batch-note">Your saved default resume and application details are ready below. Choose another saved version whenever you need it.</p><div class="batch-resume-row"><div class="field grow"><label for="batch-resume">Resume for this batch *</label><select id="batch-resume" data-resume-picker name="resume_id" data-batch-draft="resume_id" required>${resumeOptions(draft.resume_id)}</select>${resumePreview(draft.resume_id)}${button(selection.roles.length===1?'Tailor resume for this role':'Tailor resumes for these roles','tailor-create',!selection.roles.length||!draft.resume_id?'disabled':'')}</div><div class="field"><label for="batch-upload">Add another version (optional)</label><input id="batch-upload" type="file" data-resume-upload data-upload-title="Add a resume" data-upload-hint="PDF, DOCX or TXT · up to 5 MB" accept=".pdf,.docx,.txt" data-batch-upload="create"></div></div>${applicantInputs(draft.applicant)}<p class="batch-note">These details are the starting point for each role. You’ll review the employer’s actual questions, answers and resume before submission.</p><div class="batch-form-footer"><span class="tiny muted">Up to 100 roles per batch</span><button class="button primary" type="submit"${!selection.roles.length||ui.busy?' disabled':''}>Create batch</button></div></form></section>`;
  }
  function statusChip(status,label=labels[status]||status) {
    return `<span class="chip ${status==='submitted'?'green':['needs_input','uncertain','failed'].includes(status)?'amber':''}">${escape(label)}</span>`;
  }
  function renderBatchList() {
    const batches=ui.data?.batches||[];
    if(!batches.length) return '<div class="batch-empty"><h2>Your first batch starts here</h2><p>Group selected roles, a company’s top matches, or a day’s new opportunities. Your review and results stay together.</p></div>';
    return `<nav class="batch-list" aria-label="Saved application batches">${[...batches].reverse().map(b=>{const items=b.items||[],done=items.filter(i=>i.status==='submitted').length,attention=items.filter(i=>['needs_input','uncertain','failed'].includes(i.status)).length;return `<button class="batch-list-item${ui.selected===b.id?' selected':''}" data-batch-open="${escape(b.id)}"${ui.selected===b.id?' aria-current="true"':''}><span><strong>${escape(b.name||'Application batch')}</strong><small>${items.length} roles · ${escape(b.status||'draft')}</small></span><span class="batch-list-count">${done}/${items.length}<small>confirmed</small></span>${attention?`<em>${attention} need attention</em>`:''}</button>`;}).join('')}</nav>`;
  }
  function fieldValue(batch,item,field) {
    const changes=ui.edits[batch.id+'|'+item.job_id];
    if(changes && Object.prototype.hasOwnProperty.call(changes.answers||{},field.key)) return changes.answers[field.key];
    return item.planned_answers?.[field.key] ?? item.answers?.[field.key] ?? '';
  }
  function renderQuestion(batch,item,field,index,canEdit) {
    const id='batch-answer-'+item.job_id+'-'+index,value=fieldValue(batch,item,field),label=escape(field.label||field.key),type=String(field.type||'text').toLowerCase();
    if(type==='file') {
      const resumeId=ui.edits[batch.id+'|'+item.job_id]?.resume_id||item.resume_id||batch.resume_id;
      return `<div class="batch-question"><span>${label}${field.required?' *':''}</span><strong>${value?escape(value):'No attachment'}</strong>${value&&resumeById(resumeId)?.filename===value?resumePreview(resumeId):''}${field.supported===false?'<span class="tiny muted">Codex will check this attachment on the employer page.</span>':!value?'<span class="tiny muted">No file is selected for this optional attachment.</span>':''}</div>`;
    }
    const attrs=`id="${escape(id)}" data-batch-answer="${escape(field.key)}" data-answer-type="${escape(type)}" ${canEdit&&field.supported!==false?'':'disabled'}`;
    let input;
    if(['checkbox','boolean'].includes(type)) input=`<select ${attrs}><option value=""${value===''?' selected':''}>Choose an answer</option><option value="true"${value===true?' selected':''}>Yes / checked</option><option value="false"${value===false?' selected':''}>No / unchecked</option></select>`;
    else if(field.options?.length) {
      const multiple=['multiselect','select-multiple','multi_select','checkbox-group'].includes(type)||Array.isArray(value);
      input=`<select ${attrs}${multiple?' multiple':''}>${multiple?'':'<option value="">Choose an answer</option>'}${field.options.map(option=>{const text=typeof option==='string'?option:String(option.label||option.value||'');return `<option value="${escape(text)}"${(multiple?(Array.isArray(value)?value:[]):[String(value)]).includes(text)?' selected':''}>${escape(text)}</option>`;}).join('')}</select>`;
    } else if(['textarea','longtext'].includes(type)) input=`<textarea ${attrs} maxlength="10000" rows="3">${escape(value)}</textarea>`;
    else input=`<input class="input" ${attrs} type="${['email','url','tel','number','date'].includes(type)?type:'text'}" value="${escape(value)}" maxlength="10000">`;
    return `<div class="field batch-question"><label for="${escape(id)}">${label}${field.required?' <span class="batch-required">Required</span>':''}</label>${input}${field.supported===false?'<span class="tiny muted">Codex will inspect this control and its available choices in the employer form.</span>':''}</div>`;
  }
  function renderRole(batch,item) {
    const key=batch.id+'|'+item.job_id,fields=item.fields||[],canEdit=canEditItem(batch,item),changed=ui.edits[key];
    const url=safeLink(item.url),isOpen=['ready','needs_input','uncertain','failed'].includes(item.status)||Boolean(changed);
    const unsupported=fields.some(field=>field.supported===false);
    return `<article class="batch-role ${['needs_input','uncertain'].includes(item.status)?'needs-attention':''}"><div class="batch-role-heading"><div><h3>${escape(item.company)} <span>· ${escape(item.title)}</span></h3>${statusChip(item.status)}</div><div class="button-group">${canEdit?button('Tailor resume','tailor-role',`data-job-id="${escape(item.job_id)}"`):''}${url?`<a class="text-link" href="${escape(url)}" target="_blank" rel="noopener noreferrer">Employer page ↗</a>`:''}</div></div>${item.message?`<p class="batch-role-message">${escape(item.message)}</p>`:''}${canEdit&&unsupported?'<p class="batch-role-message">Some controls need a fresh inspection in Codex. Open this batch in Codex to review the employer’s current choices and any missing answers.</p>':''}${canEdit&&['needs_input','failed'].includes(item.status)?`<div class="button-group">${button('Record manual submission','reconcile',`data-job-id="${escape(item.job_id)}"`)}</div><p class="tiny muted" style="margin-top:10px">If you completed this application in the employer tab, record its actual confirmation here.</p>`:''}${item.status==='uncertain'?`<div class="batch-uncertain"><p>Check the employer tab or confirmation email before trying again. Jobflow will not automatically resubmit this role.</p>${button('Resolve outcome','reconcile',`data-job-id="${escape(item.job_id)}"`)}</div>`:''}${fields.length||changed?`<details ${detailAttrs('role-review:'+key,isOpen)}><summary>${canEdit?'Review materials'+(fields.length?' and '+fields.length+' form fields':''):'View reviewed materials and answers'}</summary><form data-batch-review="${escape(item.job_id)}" data-batch-id="${escape(batch.id)}"><div class="batch-fields"><div class="field"><label for="resume-${escape(item.job_id)}">Resume for this role</label><select id="resume-${escape(item.job_id)}" data-resume-picker data-batch-role-resume${canEdit?'':' disabled'}>${resumeOptions(changed?.resume_id??(item.resume_override?item.resume_id:''),true)}</select><span class="tiny muted">Batch resume: ${escape(resumeById(batch.resume_id)?.filename||'Unavailable')}</span></div>${fields.map((f,index)=>renderQuestion(batch,item,f,index,canEdit)).join('')}</div>${canEdit?`<div class="batch-form-footer"><span class="tiny muted">Save your changes before continuing in Codex. Codex checks the current form before submission.</span><button type="submit" class="button"${ui.busy||!changed?' disabled':''}>${fields.length?'Save answers':'Save resume'}</button></div>`:''}</form></details>`:`<p class="tiny muted">${item.status==='pending'||item.status==='inspecting'?'Open this batch in Codex to inspect this employer’s form. Answers and progress will appear here.':'No form fields have been recorded yet.'}</p>`}${item.evidence?.text?`<details ${detailAttrs('role-evidence:'+key)}><summary>Recorded confirmation</summary><p class="pre-wrap">${escape(item.evidence.text)}</p>${safeLink(item.evidence.url)?`<a href="${escape(safeLink(item.evidence.url))}" target="_blank" rel="noopener noreferrer">Confirmation page ↗</a>`:''}</details>`:''}</article>`;
  }
  function renderMaterials(batch) {
    const changes=ui.materialEdits[batch.id]||{},applicant={...batch.applicant,...changes.applicant},resume=changes.resume_id??batch.resume_id,disabled=Boolean(reservedRun(batch))||['complete','cancelled'].includes(batch.status)||(batch.items||[]).some(i=>['running','submitting'].includes(i.status));
    return `<details class="batch-materials" ${detailAttrs('materials:'+batch.id)}><summary>Batch materials · ${escape(resumeById(batch.resume_id)?.filename||'Resume unavailable')}</summary><form id="application-batch-materials" data-batch-id="${escape(batch.id)}"><fieldset${disabled?' disabled':''}><legend class="sr-only">Batch application materials</legend><div class="batch-resume-row"><div class="field grow"><label for="batch-edit-resume">Default resume</label><select id="batch-edit-resume" data-resume-picker data-batch-material="resume_id">${resumeOptions(resume)}</select>${resumePreview(resume)}${button('Tailor resumes for this batch','tailor-batch',disabled||!(batch.items||[]).some(i=>editable.has(i.status))?'disabled':'')}</div><div class="field"><label for="batch-edit-upload">Upload another resume</label><input id="batch-edit-upload" type="file" data-resume-upload data-upload-title="Add a resume" data-upload-hint="PDF, DOCX or TXT · up to 5 MB" accept=".pdf,.docx,.txt" data-batch-upload="materials"></div></div>${applicantInputs(applicant,'batch-edit')}${reservedRun(batch)?'<p class="batch-note">This run reserves its materials. Provide missing answers in your Codex chat. An unstarted request can be cancelled above to edit its materials.</p>':''}<div class="batch-form-footer"><span class="tiny muted">Changes apply to roles that can still be edited. Save them before continuing in Codex.</span><button class="button" type="submit"${ui.busy||!Object.keys(changes).length?' disabled':''}>Save materials</button></div></fieldset></form></details>`;
  }
  function renderDetail() {
    const batch=getBatch();
    if(!batch) return '<section class="card batch-empty"><h2>Select a batch</h2><p>Open a saved batch to review forms and follow its progress.</p></section>';
    const items=batch.items||[],submitted=items.filter(i=>i.status==='submitted').length,ready=items.filter(i=>i.status==='ready').length,attention=items.filter(i=>['needs_input','uncertain','failed'].includes(i.status)).length;
    const stale=ui.editRevisions[batch.id]!==undefined&&ui.editRevisions[batch.id]!==batch.revision;
    return `<section class="card batch-detail"><div class="card-head"><div><h2>${escape(batch.name||'Application batch')}</h2><p>${items.length} roles · Created ${escape(formatTime(batch.created_at))}</p></div><span class="chip">${escape(batch.status)}</span></div><div class="card-body"><div class="batch-progress"><div><strong>${submitted}</strong><span>Confirmed</span></div><div><strong>${ready}</strong><span>Ready to review</span></div><div><strong>${attention}</strong><span>Need attention</span></div></div><progress class="batch-meter" max="${Math.max(1,items.length)}" value="${submitted}" aria-label="Confirmed submissions">${submitted} of ${items.length}</progress><div class="batch-controls">${!['cancelled','complete'].includes(batch.status)?button(batch.status==='paused'?'Resume batch':'Pause batch',batch.status==='paused'?'resume':'pause')+button('Cancel remaining','cancel'):''}${button('Refresh progress','reload')}</div>${batch.status==='paused'?'<p class="batch-note">This batch is paused. An application already being sent may still finish; later roles will wait.</p>':''}${stale?`<div class="batch-warning">This batch changed while you were editing. Your unsaved edits are retained; review the latest state before saving. ${button('Discard edits & refresh','discard-edits')}</div>`:''}<section class="batch-codex-actions" data-batch-codex-progress>${codexProgress(batch)}</section>${renderMaterials(batch)}<div class="batch-roles">${items.map(i=>renderRole(batch,i)).join('')}</div></div></section>`;
  }
  function render(workspace) {
    rememberView();
    ui.workspace=workspace;
    const body=ui.loading&&!ui.data?'<div class="loading"><span class="spinner"></span>Loading your batches…</div>':`${codexHelp()}${ui.creating?renderCreate():''}<div class="batch-workspace"><aside>${renderBatchList()}</aside>${renderDetail()}</div>`;
    return `<div class="page-header"><div><h1>Application batches</h1><p>Group roles, choose materials, and follow applications with Codex.</p></div><div class="header-actions">${button('Create a batch','new','',true)}</div></div><div id="batch-live-message" class="batch-live-message" role="status" aria-live="polite">${escape(ui.notice)}</div>${ui.error?`<div class="batch-warning" role="alert">${escape(ui.error)} ${button('Try again','reload')}</div>`:''}${body}`;
  }
  function resetHandoffReview() {ui.handoffReview=null;}
  async function load(force=false,sync=false) {
    if(ui.loading)return;
    ui.loading=true;
    try {
      let syncError='';
      if(sync){
        try{
          const scopeResponse=await fetch('/api/batches',{cache:'no-store'}),scope=await scopeResponse.json();
          if(!scopeResponse.ok)throw new Error(scope.error||'Could not read this workspace.');
          const migrated=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':scope.csrf_token||ui.runtime?.csrfToken||''},body:JSON.stringify({action:'batch',op:'resume_sync',workspace_id:scope.workspace_id})});
          const migration=await migrated.json();if(!migrated.ok||migration.error)throw new Error(migration.error||'Try refreshing shortly.');
        }catch(error){syncError='Saved resume restoration is postponed. '+error.message;}
      }
      const response=await fetch('/api/batches',{cache:'no-store'}),data=await response.json();
      if(!response.ok)throw new Error(data.error||'Could not load application batches.');
      const old=getBatch(),changed=contentFingerprint(ui.data)!==contentFingerprint(data);
      if(ui.data?.workspace_id&&data.workspace_id!==ui.data.workspace_id){ui.edits={};ui.materialEdits={};ui.editRevisions={};ui.draft=null;ui.creating=false;ui.selected='';ui.details={};ui.handoff=null;ui.runtime?.closeDialog?.();resetHandoffReview();}
      ui.data=data;
      if(ui.pendingStart){const ids=ui.pendingStart;ui.pendingStart=null;openBatchStart(ids);}
      if(ui.draft){
        if(!ui.draft.resumeTouched)ui.draft.resume_id=data.default_resume_id||data.resumes?.[0]?.id||ui.draft.resume_id;
        for(const [key,value] of Object.entries(data.applicant||{}))if(!ui.draft.applicantTouched?.[key])ui.draft.applicant[key]=value;
      }
      if(!getBatch())ui.selected=data.batches?.[data.batches.length-1]?.id||'';
      const current=getBatch();
      if(old&&current&&old.revision!==current.revision)resetHandoffReview();
      if(!force&&old&&current&&JSON.stringify(old)!==JSON.stringify(current))ui.notice=hasEdits()?'Progress refreshed. Your unsaved answers are retained.':'Application progress updated.';
      ui.error=syncError;
      if(force||(changed&&!hasEdits()))draw();
      else {updateCodexProgress();if(changed){const message=document.querySelector('#batch-live-message');if(message)message.textContent=ui.notice||'Progress refreshed. Your unsaved answers are retained.';}}
    } catch(error){ui.error=error.message;if(force||!ui.data)draw();}
    finally{ui.loading=false;}
  }
  async function action(payload,notice) {
    if(ui.busy)return null;
    ui.busy=true;ui.error='';
    try {
      const response=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':ui.data?.csrf_token||ui.runtime?.csrfToken||''},body:JSON.stringify({action:'batch',...payload,...(ui.data?.workspace_id?{workspace_id:ui.data.workspace_id}:{})})});
      const data=await response.json();
      if(!response.ok||data.error)throw new Error(data.error||'The batch could not be updated.');
      ui.notice=notice||'Batch updated.';notify(ui.notice);return data;
    }catch(error){ui.error=error.message;notify(error.message,true);const dialogError=document.querySelector('#form-error');if(dialogError)dialogError.textContent=error.message;return null;}
    finally{ui.busy=false;}
  }
  function markEdit(batch) {if(ui.editRevisions[batch.id]===undefined)ui.editRevisions[batch.id]=batch.revision;resetHandoffReview();}
  function clearBatchEdits(batchId) {for(const key of Object.keys(ui.edits))if(key.startsWith(batchId+'|'))delete ui.edits[key];delete ui.materialEdits[batchId];delete ui.editRevisions[batchId];resetHandoffReview();}
  function readAnswer(node) {
    if(node.multiple)return Array.from(node.selectedOptions).map(o=>o.value);
    if(['checkbox','boolean'].includes(node.dataset.answerType))return node.value===''?'':node.value==='true';
    return node.value;
  }
  function refreshResumePreview(select) {
    const field=select.closest('.field');if(!field?.querySelector)return;
    const resume=resumeById(select.value);let link=field.querySelector('.batch-resume-preview');
    if(!resume){if(link)link.hidden=true;return;}
    if(!link){link=document.createElement('a');link.className='text-link batch-resume-preview';link.target='_blank';link.rel='noopener noreferrer';field.appendChild(link);}
    link.href='/api/application-resumes/'+encodeURIComponent(resume.id);link.textContent='Preview '+resume.filename+' ↗';link.hidden=false;
  }
  function capture(event) {
    const node=event.target,batch=getBatch();
    if(!node?.closest)return;
    const create=node.closest('#application-batch-create');
    if(create&&ui.draft) {
      if(node.dataset.batchDraft){ui.draft[node.dataset.batchDraft]=node.value;ui.draft.dirty=true;if(node.dataset.batchDraft==='resume_id')ui.draft.resumeTouched=true;}
      if(node.dataset.batchApplicant){ui.draft.applicant[node.dataset.batchApplicant]=node.value;ui.draft.applicantTouched||={};ui.draft.applicantTouched[node.dataset.batchApplicant]=true;ui.draft.dirty=true;}
      if(event.type==='change'&&['preset','company','count','date'].includes(node.dataset.batchDraft))draw();
    }
    const review=node.closest('[data-batch-review]');
    if(review&&batch&&(node.dataset.batchAnswer||node.hasAttribute('data-batch-role-resume'))) {
      markEdit(batch);const key=batch.id+'|'+review.dataset.batchReview;const changes=ui.edits[key]||(ui.edits[key]={answers:{}});
      if(node.dataset.batchAnswer)changes.answers[node.dataset.batchAnswer]=readAnswer(node);
      else changes.resume_id=node.value;
      const save=review.querySelector?.('button[type=submit]');if(save)save.disabled=false;
      updateCodexProgress();
    }
    if(node.closest('#application-batch-materials')&&batch&&(node.dataset.batchApplicant||node.dataset.batchMaterial)) {
      markEdit(batch);const changes=ui.materialEdits[batch.id]||(ui.materialEdits[batch.id]={applicant:{}});
      if(node.dataset.batchApplicant)changes.applicant[node.dataset.batchApplicant]=node.value;
      else changes[node.dataset.batchMaterial]=node.value;
      const save=node.closest('#application-batch-materials').querySelector?.('button[type=submit]');if(save)save.disabled=false;
      updateCodexProgress();
    }
    if(event.type==='change'&&(node.dataset.batchDraft==='resume_id'||node.dataset.batchMaterial==='resume_id'))refreshResumePreview(node);
    if(event.type==='change'&&node.dataset.batchUpload)upload(node);
  }
  async function upload(input) {
    const file=input.files?.[0];if(!file)return;
    if(file.size>5*1024*1024||!file.size||!(/\.(pdf|docx|txt)$/i).test(file.name)){notify('Choose a PDF, DOCX or TXT resume up to 5 MB.',true);input.value='';return;}
    try {
      const content=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',',2)[1]);reader.onerror=()=>reject(new Error('The resume could not be read.'));reader.readAsDataURL(file);});
      const result=await action({op:'upload_resume',filename:file.name,content_base64:content},'Resume uploaded to your private workspace.');
      if(result?.resume){if(input.dataset.batchUpload==='create'&&ui.draft){ui.draft.resume_id=result.resume.id;ui.draft.resumeTouched=true;ui.draft.dirty=true;}else{const batch=getBatch();if(batch){markEdit(batch);const changes=ui.materialEdits[batch.id]||(ui.materialEdits[batch.id]={applicant:{}});changes.resume_id=result.resume.id;}}await load(true);}else draw();
    }catch(error){notify(error.message,true);}
  }
  async function submit(event) {
    const form=event.target;
    if(!['application-batch-create','application-batch-materials','application-batch-reconcile'].includes(form.id)&&!form.dataset.batchReview)return;
    event.preventDefault();if(ui.busy)return;
    const batch=getBatch();
    if(form.id==='application-batch-create') {
      const draft=ui.draft,selection=selectRoles(ui.workspace,draft,ui.data?.batches,ui.seed);
      if(!selection.roles.length)return;
      const result=await action({op:'create',name:draft.name.trim()||`${draft.preset==='company'?draft.company:draft.preset==='daily'?draft.date:'Selected roles'} applications`,job_ids:selection.roles.map(j=>j.id),resume_id:draft.resume_id,applicant:draft.applicant,answers:{},items:selection.roles.filter(j=>draft.resumeOverrides?.[j.id]).map(j=>({job_id:j.id,resume_id:draft.resumeOverrides[j.id]}))},'Batch created. Open it in Codex when your materials are ready.');
      if(result){ui.selected=result.batch?.id||ui.selected;ui.creating=false;ui.draft=null;resetHandoffReview();}
    } else if(batch&&form.id==='application-batch-materials') {
      const changes=ui.materialEdits[batch.id]||{};
      const result=await action({op:'update',batch_id:batch.id,revision:activeRevision(batch),...changes},'Materials saved. Continue in Codex to inspect the forms.');
      if(result)clearBatchEdits(batch.id);
    } else if(batch&&form.dataset.batchReview) {
      const key=batch.id+'|'+form.dataset.batchReview,changes=ui.edits[key]||{};
      const result=await action({op:'update',batch_id:batch.id,revision:activeRevision(batch),items:[{job_id:form.dataset.batchReview,...changes}]},'Answers saved. Continue reviewing the application in Codex.');
      if(result){delete ui.edits[key];if(!Object.keys(ui.edits).some(k=>k.startsWith(batch.id+'|'))&&!ui.materialEdits[batch.id])delete ui.editRevisions[batch.id];resetHandoffReview();}
    } else if(batch&&form.id==='application-batch-reconcile') {
      const values=Object.fromEntries(new FormData(form));if(values.confirmed!=='on')return;
      const result=await action({op:'reconcile',batch_id:batch.id,revision:Number(form.dataset.revision),job_id:form.dataset.jobId,outcome:values.outcome,confirmed:true,evidence:{url:values.url,text:values.text}},'Outcome recorded.');
      if(result)ui.runtime?.closeDialog?.();
    }
    await load(true);
  }
  function reconcile(batch,id) {
    const item=batch.items.find(i=>i.job_id===id);
    if(!item||!['uncertain','needs_input','failed'].includes(item.status))return;
    const manual=item.status!=='uncertain';
    ui.runtime?.showDialog?.(`<h2>${manual?'Record manual submission':'Resolve application outcome'}</h2><p>${escape(item.company)} · ${escape(item.title)}</p>${manual?'<p>Use this only after you submitted the application in the employer tab and received a confirmation. Resolving a login or CAPTCHA alone is not a completed submission.</p>':''}<form id="application-batch-reconcile" data-job-id="${escape(id)}" data-revision="${escape(batch.revision)}">${manual?'<input type="hidden" name="outcome" value="submitted">':'<div class="field"><label for="batch-outcome">Verified outcome</label><select id="batch-outcome" name="outcome"><option value="submitted">Submission confirmed</option><option value="not_submitted">Confirmed not submitted · review in Codex</option></select></div>'}<div class="field"><label for="batch-evidence-url">Employer / confirmation URL</label><input class="input" type="url" id="batch-evidence-url" name="url" value="${escape(item.url||'')}" required></div><div class="field"><label for="batch-evidence-text">${manual?'Actual submission confirmation':'What you verified'}</label><textarea id="batch-evidence-text" name="text" required maxlength="4000" placeholder="${manual?'Record the actual confirmation message or confirmation email you received.':'Record the confirmation message or evidence that this application was not submitted.'}"></textarea></div><label class="batch-check"><input type="checkbox" name="confirmed" required> ${manual?'I submitted this role and verified its successful submission.':'I checked the actual outcome for this role.'}</label><div class="button-group"><button class="button primary" type="submit">${manual?'Record confirmed submission':'Save verified outcome'}</button></div></form>`);
  }
  function startTailoring(op,jobId='') {
    if(ui.busy||!ui.runtime?.startResumes)return;
    const workspaceId=ui.data?.workspace_id;
    if(op==='tailor-create') {
      const draft=ui.draft;if(!draft)return;
      const ids=selectRoles(ui.workspace,draft,ui.data?.batches,ui.seed).roles.map(j=>j.id);
      if(!ids.length||!draft.resume_id)return;
      draft.resumeTouched=true;
      ui.runtime.startResumes(ids,{resumeId:draft.resume_id,returnLabel:'Back to application',onSaved:async(resume,savedJobId)=>{
        if(!resume?.id||!ids.includes(savedJobId))return;
        await load(true);
        if(ui.data?.workspace_id!==workspaceId||ui.draft!==draft)return;
        if(!selectRoles(ui.workspace,draft,ui.data?.batches,ui.seed).roles.some(j=>j.id===savedJobId))throw new Error('This role is no longer eligible for the selected application batch.');
        if(!resumeById(resume.id))ui.data.resumes.push(resume);
        draft.resumeOverrides||={};draft.resumeOverrides[savedJobId]=resume.id;draft.dirty=true;
        ui.notice='Tailored resumes are selected for their roles. Review your application materials before creating the batch.';
      }});
      return;
    }
    const batch=getBatch();
    if(!batch||['cancelled','complete'].includes(batch.status))return;
    const items=(batch.items||[]).filter(item=>canEditItem(batch,item)&&(!jobId||item.job_id===jobId));
    if(!items.length)return;
    const ids=items.map(item=>item.job_id),item=items[0],resumeId=jobId?(ui.edits[batch.id+'|'+jobId]?.resume_id||item.resume_id||batch.resume_id):(ui.materialEdits[batch.id]?.resume_id||batch.resume_id);
    ui.runtime.startResumes(ids,{resumeId,returnLabel:'Back to application batch',onSaved:async(resume,savedJobId)=>{
      if(!resume?.id||!ids.includes(savedJobId))return;
      await load(true);
      if(ui.data?.workspace_id!==workspaceId)return;
      const current=(ui.data?.batches||[]).find(value=>value.id===batch.id),currentItem=current?.items?.find(value=>value.job_id===savedJobId);
      if(!current||['cancelled','complete'].includes(current.status)||!currentItem||!canEditItem(current,currentItem))throw new Error('This application changed while you were reviewing its resume.');
      if(!resumeById(resume.id))ui.data.resumes.push(resume);
      markEdit(current);const key=current.id+'|'+savedJobId,changes=ui.edits[key]||(ui.edits[key]={answers:{}});
      changes.resume_id=resume.id;ui.details['role-review:'+key]=true;
      ui.notice='Tailored resumes are selected for their roles. Save each role’s materials before continuing in Codex.';
    }});
  }
  async function click(event) {
    const node=event.target.closest?.('[data-batch-action],[data-batch-open]');if(!node)return;
    if(node.dataset.batchOpen){ui.selected=node.dataset.batchOpen;resetHandoffReview();draw();return;}
    const op=node.dataset.batchAction,batch=getBatch();
    if(['tailor-create','tailor-batch','tailor-role'].includes(op)){startTailoring(op,op==='tailor-role'?node.dataset.jobId:'');return;}
    if(op==='clear-tailored'&&ui.draft){delete ui.draft.resumeOverrides?.[node.dataset.jobId];ui.draft.dirty=true;draw();return;}
    if(op==='new'){ui.creating=true;if(!ui.draft){ui.seed=[...(ui.workspace?.selected||[])];ui.draft=newDraft();}draw();return;}
    if(op==='close-create'){ui.creating=false;draw();return;}
    if(op==='reload'){await load(true,true);return;}
    if(op==='codex-review'&&batch){reviewCodex(batch);return;}
    if(op==='codex-prepare'&&batch){
      const review=ui.handoffReview;
      if(!canPrepareCodex(batch)||!review||review.batchId!==batch.id||review.workspaceId!==ui.data.workspace_id||review.revision!==batch.revision){notify('This batch changed. Review its current materials before preparing a request.',true);return;}
      const workspaceId=ui.data.workspace_id;
      const result=await action({op:'codex_prepare',batch_id:batch.id,revision:review.revision,job_ids:review.jobIds},'Codex request prepared. Open it in Codex and press Send to start.');
      if(!result)return;
      await load();
      if(ui.data?.workspace_id===workspaceId){const current=(ui.data.batches||[]).find(value=>value.id===batch.id)?.codex_run;const run=current?.id===result.run?.id?current:result.run;ui.notice=run?.started?'Continue in the original Codex chat.':'Codex request prepared. Open it in Codex and press Send to start.';draw();showCodexHandoff({...result,run},batch.id);}
      return;
    }
    if(op==='codex-discard'&&batch){
      if(ui.busy||batch.codex_run?.status!=='awaiting_start'||batch.codex_run?.started)return;
      const result=await action({op:'codex_discard',batch_id:batch.id,revision:batch.revision},'Prepared request cancelled. You can edit this batch’s materials.');
      if(result){ui.handoff=null;ui.handoffReview=null;ui.runtime?.closeDialog?.();await load(true);}
      return;
    }
    if(op==='codex-copy'){
      if(!ui.handoff?.prompt||ui.handoff.workspaceId!==ui.data?.workspace_id)return;
      const status=document.querySelector('[data-batch-copy-status]');
      try{if(!globalThis.navigator?.clipboard?.writeText)throw new Error('Clipboard unavailable');await navigator.clipboard.writeText(ui.handoff.prompt);if(status)status.textContent='Request copied. Paste it into Codex and press Send to start.';}
      catch{if(status)status.textContent='Copy the prepared request from “View request to copy manually” below.';}
      return;
    }
    if(op==='reconcile'&&batch){reconcile(batch,node.dataset.jobId);return;}
    if(op==='discard-edits'&&batch){clearBatchEdits(batch.id);await load(true);return;}
    if(batch&&['pause','resume','cancel'].includes(op)) {
      if(op==='cancel'&&!window.confirm('Cancel the remaining roles in this batch? Confirmed submissions and uncertain outcomes will be retained.'))return;
      const result=await action({op,batch_id:batch.id,revision:batch.revision},op==='pause'?'Batch paused.':op==='resume'?'Batch resumed.':'Remaining applications cancelled.');
      if(result)resetHandoffReview();await load(true);
    }
  }
  function shouldPoll() {return ui.active&&!ui.workspace?.account?.hosted&&ui.data?.codex?.available!==false;}
  function schedule() {
    clearTimeout(ui.timer);ui.timer=null;
    // Hosted batches have no local Codex worker. Opening the view and explicit
    // actions still refresh it, without repeatedly transferring private records.
    if(!shouldPoll())return;
    ui.timer=setTimeout(async()=>{ui.timer=null;if(!shouldPoll())return;await load();schedule();},5000);
  }
  function mount(runtime) {ui.runtime=runtime;const wasActive=ui.active;ui.active=true;restoreFocus();if(!wasActive){load(true,true);schedule();}}
  function unmount() {ui.active=false;clearTimeout(ui.timer);ui.timer=null;}
  function retainingBatch(jobId) {
    return [...(ui.data?.batches||[])].reverse().find(batch=>(batch.items||[]).some(item=>item.job_id===jobId&&item.status!=='skipped'&&(batch.status!=='cancelled'||['submitting','submitted','uncertain'].includes(item.status))));
  }
  function openBatchStart(ids) {
    const existing=ids.length===1?retainingBatch(ids[0]):null;
    if(existing){ui.selected=existing.id;ui.creating=false;ui.notice='Opened the saved batch for this role.';resetHandoffReview();return;}
    ui.seed=[...ids];ui.creating=true;ui.draft=null;resetHandoffReview();
  }
  function start(ids=[]) {
    if(ids.length!==1){ui.pendingStart=null;openBatchStart(ids);return;}
    // Resolve single-role entry against the next snapshot, including the first
    // visit. Keep any in-progress composer and role edits when opening a batch.
    ui.pendingStart=[...ids];ui.creating=false;
    const existing=retainingBatch(ids[0]);if(existing)ui.selected=existing.id;
    if(ui.active)load(true);
  }
  document.addEventListener('click',click);
  document.addEventListener('input',capture);
  document.addEventListener('change',capture);
  document.addEventListener('submit',submit);
  window.JobflowBatches={render,mount,unmount,start,selectRoles,eligibility};
})();

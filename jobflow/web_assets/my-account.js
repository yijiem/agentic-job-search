/* Account overview. Reads existing records; editing stays in the established workflows. */
(function () {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const ui = {workspace:null, profile:null, library:null, errors:{}, active:false, loading:false, loaded:false, request:0, filter:'applied', query:'', page:0};
  const labels = {saved:'Saved',applied:'Applied',recruiter:'Recruiter screen',technical:'Technical',onsite:'Onsite',offer:'Offer',rejected:'Rejected',withdrawn:'Withdrawn'};
  const submittedStages = new Set(['applied','recruiter','technical','onsite','offer']);
  const hasDate = value => Boolean(value && Number.isFinite(Date.parse(value)));
  const submitted = row => hasDate(row.applied_at) || submittedStages.has(row.stage);
  const records = () => ui.workspace?.applications || [];
  const roleMap = () => new Map((ui.workspace?.jobs || []).map(job => [job.id,job]));
  function date(value) {
    if (!hasDate(value)) return 'Date not recorded';
    try { return new Intl.DateTimeFormat('en-US',{month:'short',day:'numeric',year:'numeric',timeZone:ui.workspace?.timezone || 'America/Los_Angeles'}).format(new Date(value)); }
    catch { return 'Date not recorded'; }
  }
  function link(url, text) {
    try { const parsed = new URL(url); if (['https:','http:'].includes(parsed.protocol)) return `<a href="${esc(parsed.href)}" target="_blank" rel="noopener noreferrer">${esc(text)} ↗</a>`; } catch {}
    return esc(text);
  }
  const missing = '<span class="muted">Not added</span>';
  function errorCard(section) { return ui.errors[section] ? `<div class="my-account-error" role="alert">${esc(ui.errors[section])} <button class="text-link" data-my-account-action="retry">Try again</button></div>` : ''; }
  function loading(text) { return `<p class="my-account-placeholder" role="status">${esc(text)}</p>`; }
  function profileCard() {
    const profile = ui.profile, applicant = ui.library?.applicant;
    const hosted = ui.workspace?.account?.hosted;
    const details = [['Email',applicant?.email],['Phone',applicant?.phone],['Location',applicant?.location],['LinkedIn',applicant?.linkedin],['Website',applicant?.website]];
    return `<section class="card my-account-card" aria-labelledby="my-profile-title"><div class="card-head"><div><h2 id="my-profile-title">Profile & contact</h2><p>The background and details saved in your workspace.</p></div></div><div class="card-body">
      ${errorCard('profile')}${!profile && !ui.errors.profile ? loading('Loading your profile…') : ''}
      ${profile ? `<p class="my-account-headline">${esc(profile.headline || 'Add a professional focus to your profile.')}</p><div class="my-account-skills">${(profile.skills || []).map(skill=>`<span class="chip">${esc(skill)}</span>`).join('') || '<span class="muted">No skills added yet.</span>'}</div>` : ''}
      ${applicant ? `<dl class="my-account-contact"><div><dt>Applicant name</dt><dd>${esc([applicant.first_name,applicant.last_name].filter(Boolean).join(' ')) || missing}</dd></div>${details.map(([label,value])=>`<div><dt>${label}</dt><dd>${value ? (['LinkedIn','Website'].includes(label) ? link(value,value) : esc(value)) : missing}</dd></div>`).join('')}</dl>` : `<p class="muted">${ui.errors.library?'Contact details could not be loaded.':'Loading contact details…'}</p>`}
      ${hosted ? `<p class="my-account-signin">Signed in as ${esc(ui.workspace.account.email)}</p>` : ''}
      <div class="button-group"><a class="button small" href="#resumes">Edit contact details</a>${hosted?'<a class="button small" href="/account?edit=1">Edit background & preferences</a>':''}</div>
      ${profile?.evidence?.length ? `<details class="my-account-evidence"><summary>Experience & accomplishments <span>${profile.evidence.length}</span></summary><ul>${profile.evidence.map(item=>`<li>${item.employer?`<strong>${esc(item.employer)}</strong>`:''}<p>${esc(item.text)}</p>${item.source_url?`<small>${link(item.source_url,'Source')}</small>`:''}</li>`).join('')}</ul></details>` : ''}
    </div></section>`;
  }
  function resumesCard() {
    const library = ui.library, resumes = library?.resumes || [];
    const ordered = [...resumes].sort((a,b)=>Number(b.id===library.default_resume_id)-Number(a.id===library.default_resume_id));
    return `<section class="card my-account-card" aria-labelledby="my-resumes-title"><div class="card-head"><div><h2 id="my-resumes-title">My resumes</h2><p>Saved files and versions for your applications.</p></div><a class="text-link" href="#resumes">Manage resumes →</a></div><div class="card-body">
      ${errorCard('library')}${!library&&!ui.errors.library?loading('Loading your resumes…'):''}
      ${library&&!resumes.length?'<div class="my-account-empty"><h3>No resumes in your library yet</h3><p>Open the library to restore existing uploads or add your current resumes.</p><a class="button" href="#resumes">Open resume library</a></div>':''}
      ${ordered.slice(0,4).map(resume=>`<article class="my-account-resume${resume.id===library.default_resume_id?' is-default':''}"><div class="my-account-file" aria-hidden="true">${esc((resume.filename||'file').split('.').pop().slice(0,5).toUpperCase())}</div><div class="my-account-resume-info"><h3>${esc(resume.label||resume.filename)}</h3><p>${esc(resume.filename)} · Version ${esc(resume.version||1)}</p><small>${hasDate(resume.created_at)?'Saved '+esc(date(resume.created_at)):'Save date not recorded'}</small>${resume.id===library.default_resume_id?'<span class="chip green">Default resume</span>':''}</div><a class="button small" href="/api/application-resumes/${encodeURIComponent(resume.id)}" target="_blank" rel="noopener noreferrer" aria-label="Open ${esc(resume.label||resume.filename)}">Open ↗</a></article>`).join('')}
      ${resumes.length>4?`<a class="text-link my-account-all-resumes" href="#resumes">View all ${resumes.length} resume versions →</a>`:''}
      ${library?.drafts?.some(d=>d.status==='pending')?`<p class="my-account-draft-note">${library.drafts.filter(d=>d.status==='pending').length} draft(s) awaiting review. <a href="#resumes">Continue in Resumes →</a></p>`:''}
    </div></section>`;
  }
  function searchPreferences() {
    const settings=ui.workspace?.settings || {}, search=settings.search || {};
    let target='Not set';
    if (Number(search.target_tc)>0) { try { target=new Intl.NumberFormat('en-US',{style:'currency',currency:search.currency||'USD',maximumFractionDigits:0}).format(search.target_tc); } catch { target=String(search.target_tc); } }
    return `<section class="my-account-preferences" aria-label="Search preferences"><div><span>Preferred locations</span><p>${esc((search.preferred_locations||[]).join(' · ')||'Not set')}</p></div><div><span>Target levels</span><p>${esc((search.allowed_levels||[]).join(' · ')||'Not set')}</p></div><div><span>Target total compensation</span><p>${esc(target)}</p></div></section>`;
  }
  function applicationRows() {
    const jobs=roleMap(), query=ui.query.trim().toLocaleLowerCase();
    return records().filter(row=>ui.filter==='all'||(ui.filter==='applied'?submitted(row):row.stage==='saved'&&!submitted(row))).filter(row=>{
      const job=jobs.get(row.job_id)||{};
      return !query || [job.company||row.company,job.title||row.title,labels[row.stage]||row.stage].join(' ').toLocaleLowerCase().includes(query);
    }).sort((a,b)=>(hasDate(b.applied_at)?Date.parse(b.applied_at):0)-(hasDate(a.applied_at)?Date.parse(a.applied_at):0) || String(a.company||jobs.get(a.job_id)?.company||'').localeCompare(String(b.company||jobs.get(b.job_id)?.company||'')));
  }
  function historyResults() {
    const jobs=roleMap(), rows=applicationRows(), pageSize=10;
    ui.page=Math.min(ui.page,Math.max(0,Math.ceil(rows.length/pageSize)-1));
    if (!rows.length) return `<div class="my-account-empty"><h3>${ui.query?'No matching applications':ui.filter==='applied'?'No submitted applications recorded':ui.filter==='saved'?'No saved roles yet':'No applications recorded yet'}</h3><p>${ui.query?'Try a different company, role, or stage.':ui.filter==='applied'?'Applications you record as submitted will appear here. Saved roles are available in their own view.':'Save a role or record an application from Explore roles.'}</p>${!ui.query?'<a class="button" href="#jobs">Explore roles</a>':''}</div>`;
    return `<div class="table-scroll"><table class="role-table my-account-table"><caption class="sr-only">${ui.filter==='applied'?'Submitted applications':ui.filter==='saved'?'Saved roles':'All tracked applications'}</caption><thead><tr><th scope="col">Company & role</th><th scope="col">Stage</th><th scope="col">Applied on</th><th scope="col">Next step</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead><tbody>${rows.slice(ui.page*pageSize,(ui.page+1)*pageSize).map(row=>{
      const job=jobs.get(row.job_id), company=job?.company||row.company||'Company not recorded', title=job?.title||row.title||'Role not recorded';
      return `<tr><td><strong>${esc(company)}</strong>${job?`<button class="role-title" data-job="${esc(row.job_id)}">${esc(title)}</button>`:`<span class="my-account-role">${esc(title)}</span>`}</td><td><span class="chip${row.stage==='offer'?' green':''}">${esc(labels[row.stage]||row.stage||'Not recorded')}</span></td><td>${submitted(row)?esc(date(row.applied_at)):row.stage==='saved'?'Not applied':'Submission not recorded'}</td><td>${esc(row.next_action||'—')}${row.follow_up_date?`<small>Follow up ${esc(row.follow_up_date)}</small>`:''}</td><td>${job?`<button class="button small" data-track="${esc(row.job_id)}" aria-label="Update ${esc(title)} at ${esc(company)}">Update</button>`:''}</td></tr>`;
    }).join('')}</tbody></table></div><div class="table-footer"><span>${ui.page*pageSize+1}–${Math.min((ui.page+1)*pageSize,rows.length)} of ${rows.length}</span><div class="pagination"><button class="button small" data-my-account-action="previous"${ui.page===0?' disabled':''}>Previous</button><button class="button small" data-my-account-action="next"${(ui.page+1)*pageSize>=rows.length?' disabled':''}>Next</button></div></div>`;
  }
  function historyCard() {
    return `<section class="card my-account-history" aria-labelledby="my-applications-title"><div class="card-head"><div><h2 id="my-applications-title">My applications</h2><p>Your recorded submissions, progress, and saved roles.</p></div><a class="text-link" href="#applications">Open application board →</a></div><div class="my-account-filters"><div class="my-account-tabs" role="group" aria-label="Application view">${[['applied','Applied'],['saved','Saved'],['all','All tracked']].map(([id,label])=>`<button data-my-account-filter="${id}" aria-pressed="${ui.filter===id}">${label}</button>`).join('')}</div><div class="field"><label class="sr-only" for="my-account-search">Search applications by company, role, or stage</label><input class="input" id="my-account-search" type="search" placeholder="Search company, role, or stage" value="${esc(ui.query)}"></div></div><div id="my-account-results" aria-live="polite">${historyResults()}</div></section>`;
  }
  function render(workspace) {
    if (ui.workspace!==workspace) {
      ui.workspace=workspace; ui.request++; ui.profile=null; ui.library=null; ui.loaded=false; ui.loading=false; ui.errors={};
    }
    const applicant=ui.library?.applicant || {}, name=ui.profile?.name || [applicant.first_name,applicant.last_name].filter(Boolean).join(' ');
    const count=ui.library?ui.library.resumes.length:'—';
    const stats=[['Resume versions',count],['Applied roles',records().filter(submitted).length],['In interviews',records().filter(a=>['recruiter','technical','onsite'].includes(a.stage)).length],['Saved roles',records().filter(a=>a.stage==='saved'&&!submitted(a)).length]];
    return `<div class="page-header"><div><h1>My account</h1><p>Your profile, resumes, and application history in one place.</p></div></div><section class="my-account-identity"><div class="my-account-avatar" aria-hidden="true">${esc(name?name.trim().split(/\s+/).slice(0,2).map(part=>part[0]).join('').toUpperCase():'ME')}</div><div><h2>${esc(name || (ui.loaded?'Your workspace':'Loading your account…'))}</h2><p>${esc(ui.profile?.headline || 'Your next chapter starts here.')}</p></div><span class="chip">Private workspace</span></section><dl class="my-account-stats">${stats.map(([label,value])=>`<div><dt>${label}</dt><dd>${value}</dd></div>`).join('')}</dl><div class="my-account-grid">${profileCard()}${resumesCard()}</div>${searchPreferences()}${historyCard()}`;
  }
  function draw() { if (ui.active) { const main=document.querySelector('#main'); if(main) main.innerHTML=render(ui.workspace); } }
  async function read(path) {
    const response=await fetch(path,{cache:'no-store',credentials:'same-origin',headers:{Accept:'application/json'}});
    if(response.status===401) { location.assign('/account'); throw new Error('Please sign in to view your account.'); }
    if(!response.ok) throw new Error('Unable to load this section.');
    return response.json();
  }
  async function load() {
    if(ui.loading)return;
    ui.loading=true; ui.errors={}; const request=++ui.request;
    const results=await Promise.allSettled([read('/api/profile'),read('/api/resumes')]);
    if(request!==ui.request||!ui.active)return;
    results.forEach((result,index)=>{
      const key=index===0?'profile':'library';
      if(result.status==='fulfilled') ui[key]=index===0?result.value.profile:result.value;
      else { ui[key]=null; ui.errors[key]=index===0?'Your profile could not be loaded.':'Your resume library and contact details could not be loaded.'; }
    });
    ui.loaded=true; ui.loading=false; draw();
  }
  function mount() { ui.active=true; if(!ui.loaded)load(); }
  function unmount() { ui.active=false; ui.request++; ui.loaded=false; ui.loading=false; ui.profile=null; ui.library=null; }
  document.addEventListener('click',event=>{
    if(!ui.active)return;
    const node=event.target.closest?.('[data-my-account-action],[data-my-account-filter]'); if(!node)return;
    if(node.dataset.myAccountFilter) { ui.filter=node.dataset.myAccountFilter;ui.page=0;draw(); }
    if(node.dataset.myAccountAction==='retry')load();
    if(['previous','next'].includes(node.dataset.myAccountAction)) { ui.page=Math.max(0,ui.page+(node.dataset.myAccountAction==='next'?1:-1));draw(); }
  });
  document.addEventListener('input',event=>{
    if(!ui.active||event.target.id!=='my-account-search')return;
    ui.query=event.target.value; ui.page=0;
    const results=document.querySelector('#my-account-results'); if(results)results.innerHTML=historyResults();
  });
  window.JobflowMyAccount={render,mount,unmount};
})();

'use strict';

// Presentation layer only. Persistent records and planning rules live behind /api.
const $ = (selector, root = document) => root.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safeURL = value => { try { const u = new URL(value); return ['https:', 'http:'].includes(u.protocol) ? u.href : ''; } catch { return ''; } };
const external = (url, label, cls = '') => safeURL(url) ? `<a class="${esc(cls)}" href="${esc(safeURL(url))}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>` : `<span class="muted">${esc(label)}</span>`;
const icons = {
  overview:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  inbox:'<path d="M4 4h16l2 12v4H2v-4L4 4Z"/><path d="M2 15h6l2 3h4l2-3h6"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  chart:'<path d="M3 3v18h18M7 15l4-5 4 3 6-8"/>',
  map:'<path d="m3 5 6-2 6 3 6-2v15l-6 2-6-3-6 2V5Zm6-2v15m6-12v15"/>',
  applications:'<rect x="3" y="6" width="18" height="15" rx="2"/><path d="M8 6V3h8v3M3 12h18M10 12v3h4v-3"/>',
  connections:'<circle cx="12" cy="5" r="3"/><circle cx="5" cy="18" r="3"/><circle cx="19" cy="18" r="3"/><path d="m10.5 8-4 7m7-7 4 7M8 18h8"/>',
  prep:'<path d="m2 7 10-4 10 4-10 4L2 7Zm4 2v7c3 3 9 3 12 0V9m4-2v10"/>',
  calendar:'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 2v6m10-6v6M3 11h18"/>',
  arrow:'<path d="M5 12h14m-5-5 5 5-5 5"/>',
  check:'<path d="m5 12 4 4L19 6"/>',
  star:'<path d="m12 3 2.7 5.6 6.3.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.5l6.3-.9L12 3Z"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
  plus:'<path d="M12 4v16M4 12h16"/>',
  pin:'<path d="M19 10c0 6-7 11-7 11S5 16 5 10a7 7 0 1 1 14 0Z"/><circle cx="12" cy="10" r="2"/>',
};
const icon = (name, cls = '') => `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.overview}</svg>`;
const pages = [{id:'account',label:'My account',utility:true},{id:'overview',label:'Overview',icon:'overview'},{id:'inbox',label:'Daily inbox',icon:'inbox'},{id:'jobs',label:'Explore roles',icon:'search'},{id:'analytics',label:'Insights & map',icon:'chart'},{id:'applications',label:'Applications',icon:'applications'},{id:'batches',label:'Batches',icon:'applications'},{id:'resumes',label:'Resumes',icon:'applications'},{id:'connections',label:'Connections',icon:'connections'},{id:'prep',label:'Interview prep',icon:'prep'}];
const stages = ['saved','applied','recruiter','technical','onsite','offer','rejected','withdrawn'];
const stageLabels = {saved:'Saved',applied:'Applied',recruiter:'Recruiter screen',technical:'Technical',onsite:'Onsite',offer:'Offer',rejected:'Rejected',withdrawn:'Withdrawn'};
const colors = ['#72b9a9','#d5bc78','#8ca9c1','#a5b796','#b7a0c3'];
const state = {data:null, page:'overview', filters:{q:'',company:'',score:'',availability:'active',location:'',sort:'score'}, selected:new Set(),pageIndex:0,inboxDate:'',prepJob:'',contactCompany:'',contactJob:'saved',city:'',detail:null,detailTab:'overview',busy:false};
state.rolesView = 'companies';
state.roleCompany = '';
state.companySort = 'roles';
const loadedBuild = $('meta[name="jobflow-build"]')?.content || '';
const fmt = n => Number(n || 0).toLocaleString();
const jobs = () => state.data?.jobs || [];
const applications = () => state.data?.applications || [];
const jobById = id => state.jobMap?.get(id);
const appById = id => state.appMap?.get(id);
const today = () => state.data?.today || new Intl.DateTimeFormat('en-CA').format(new Date());
const active = j => j.availability !== 'closed' && j.eligible !== false && Number(j.score || 0) >= Number(state.data?.settings?.search?.min_score || 0);
const dayCache=new Map();
function localDay(value) { if (!value) return '';const cacheKey=(state.data?.timezone||'')+'|'+value;if(dayCache.has(cacheKey))return dayCache.get(cacheKey); if (/^\d{4}-\d\d-\d\d$/.test(value)) return value; try { const parts = new Intl.DateTimeFormat('en-CA',{timeZone:state.data?.timezone || 'America/Los_Angeles',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date(value)); const day=`${parts.find(p=>p.type==='year').value}-${parts.find(p=>p.type==='month').value}-${parts.find(p=>p.type==='day').value}`;dayCache.set(cacheKey,day);return day; } catch { return ''; } }
function prettyDate(value, opts={}) { if (!value) return 'Date not recorded'; try { const d = /^\d{4}-\d\d-\d\d$/.test(value) ? new Date(value+'T12:00:00') : new Date(value); return new Intl.DateTimeFormat('en-US',{month:'short',day:'numeric',...opts,...(/^\d{4}-\d\d-\d\d$/.test(value)?{}:{timeZone:state.data?.timezone || 'America/Los_Angeles'})}).format(d); } catch { return value; } }
const initials = name => String(name || '?').replace(/[^\p{L}\p{N} ]/gu,'').split(/\s+/).slice(0,2).map(v=>v[0] || '').join('').toUpperCase();
const companyLogo = company => {
  const path = window.JobflowCompanyLogos?.[company];
  const local = typeof path === 'string' && /^\/logos\/[a-z0-9_-]+\.(png|jpe?g|ico|webp|svg)$/.test(path);
  return `<span class="company-logo${local ? ' has-image' : ''}" aria-hidden="true"><span>${esc(initials(company))}</span>${local ? `<img src="${esc(path)}" alt="" loading="lazy" decoding="async">` : ''}</span>`;
};
function currency(value, currency='USD') { if (value === null || value === undefined || value === '') return ''; return new Intl.NumberFormat('en-US',{style:'currency',currency:currency || 'USD',maximumFractionDigits:0,notation:Number(value)>=100000?'compact':'standard'}).format(Number(value)); }
function compensation(job, kind='base') { const c=job.compensation || {}; const a=c[`${kind}_min`], b=c[`${kind}_max`]; if (a==null && b==null) return kind==='tc'?'Not verified':'Not recorded'; return `${currency(a ?? b,c.currency)}${a!=null && b!=null && a!==b?' – '+currency(b,c.currency):''}`; }
const scoreBadge = score => `<span class="score ${Number(score)<80?'mid':''}">${icon('star')} ${esc(score ?? '—')}</span>`.replace('<svg class=""','<svg width="10" height="10" class=""');
const availabilityChip = job => job.availability==='missing'?'<span class="chip amber">Recheck listing</span>':job.availability==='closed'?'<span class="chip red">Closed</span>':'';
const empty = (title,text,symbol='↗') => `<div class="empty"><div class="empty-symbol">${symbol}</div><h3>${esc(title)}</h3><p>${esc(text)}</p></div>`;
const button = (label, attrs='', cls='') => `<button class="button ${cls}" ${attrs}>${label}</button>`;
const pageHeader = (eyebrow,title,desc,actions='') => `<div class="page-header"><div><h1>${esc(title)}</h1><p>${esc(desc)}</p></div><div class="header-actions">${actions}</div></div>`;
const companyCounts = (list) => [...list.reduce((m,j)=>m.set(j.company,(m.get(j.company)||0)+1),new Map())].sort((a,b)=>b[1]-a[1] || a[0].localeCompare(b[0]));
const dueApps = () => applications().filter(a=>a.follow_up_date && a.follow_up_date<=today() && !['rejected','withdrawn'].includes(a.stage));
const pendingTasks = () => (state.data?.tasks || []).filter(t=>['todo','doing'].includes(t.status));
function toast(message,error=false) { const node=$('#toast');node.textContent=message;node.className='toast visible'+(error?' error':'');clearTimeout(state.toastTimer);state.toastTimer=setTimeout(()=>node.classList.remove('visible'),5000); }
function setData(data) { state.agentProposal=null; resetPrepGuide(); state.data=data; state.jobMap=new Map((data.jobs||[]).map(j=>[j.id,j]));state.appMap=new Map((data.applications||[]).map(a=>[a.job_id||a.id,a])); if(!state.inboxDate)state.inboxDate=data.today; noticeAppBuild(data.app_build); updateHostedChrome(); }
async function refresh(showNotice=false) {
  // The async bootstrap can arrive before or after app.js. Claim its response
  // once and prevent a late bootstrap from starting a duplicate workspace read.
  window.JobflowStateStarted = true;
  const initial = window.JobflowInitialState;
  delete window.JobflowInitialState;
  try {
    const pending = initial ? await initial : null;
    if (pending?.error) throw pending.error;
    const response = pending?.response || await fetch('/api/state', {cache:'no-store'});
    if (response.status === 401 || response.status === 409) {location.assign('/account');return;}
    if (!response.ok) throw new Error('Unable to read your workspace.');
    setData(await response.json());
    render();
    if (showNotice) toast('Workspace refreshed.');
  } catch (error) {
    if (!state.data) $('#main').innerHTML = pageHeader('Your workspace','Workspace unavailable','The local workspace is not responding.') + empty('Could not load your data',error.message) + button('Try again','data-action="refresh"','primary');
    else toast(error.message,true);
  }
}
async function mutate(payload, message='Workspace updated.') { if(state.busy)return false;state.busy=true;document.querySelectorAll('.action-dialog button[type="submit"]').forEach(b=>b.disabled=true);try { const batches=payload.action==='application'&&payload.job_ids?.length>100?Array.from({length:Math.ceil(payload.job_ids.length/100)},(_,i)=>({...payload,job_ids:payload.job_ids.slice(i*100,(i+1)*100)})):[payload];let completed=0,skipped=0;for(const batch of batches){const response=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':state.data.csrf_token},body:JSON.stringify(batch)});const result=await response.json();if(!response.ok||result.error)throw new Error((completed?`${completed} records updated before the error. `:'')+(result.error||result.message||'The update could not be saved.'));completed+=Number(result.updated||0);skipped+=Number(result.skipped||0);}await refresh();toast(message+(skipped?` ${skipped} existing application stage${skipped===1?' was':'s were'} preserved.`:''));return true;}catch(error){toast(error.message,true);const el=$('#form-error');if(el)el.textContent=error.message;return false;}finally{state.busy=false;document.querySelectorAll('.action-dialog button[type="submit"]').forEach(b=>b.disabled=false);} }
function updateHostedChrome() {
  const hosted = Boolean(state.data?.account?.hosted);
  const accountLink = $('#account-link'), searchButton = $('#cloud-search');
  if (accountLink) accountLink.hidden = false;
  if (searchButton) searchButton.hidden = !hosted;
  const status = $('#background-status');
  if (!status) return;
  const job = state.data?.background_job;
  status.hidden = !hosted || !job;
  status.textContent = job ? (job.status === 'pending' ? 'Your workspace update is queued.' : job.status === 'running' ? 'Updating your matches and daily plan…' : job.message || '') : '';
  status.classList.toggle('needs-attention', Boolean(job && ['failed','partial'].includes(job.status)));
  clearTimeout(state.backgroundTimer);
  if (hosted && job && ['pending','running'].includes(job.status)) state.backgroundTimer = setTimeout(pollBackground, 15000);
}
async function pollBackground() {
  if (!state.data?.account?.hosted) return;
  try {
    const response = await fetch('/api/background', {cache:'no-store'});
    if (response.status === 401) { location.assign('/account'); return; }
    if (!response.ok) throw new Error('Background status is temporarily unavailable.');
    const result = await response.json();
    state.data.background_job = result.background_job;
    if (result.background_job && ['pending','running'].includes(result.background_job.status)) updateHostedChrome();
    else await refresh();
  } catch {
    state.backgroundTimer = setTimeout(pollBackground, 30000);
  }
}
async function startCloudSearch() {
  const button = $('#cloud-search');
  if (!state.data?.account?.hosted || button?.disabled) return;
  button.disabled = true;
  try {
    const response = await fetch('/api/search', {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':state.data.csrf_token}, body:'{}'});
    if (response.status === 401) { location.assign('/account'); return; }
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'The search could not be queued.');
    await refresh();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}
$('#cloud-search')?.addEventListener('click', startCloudSearch);
function parseRoute(hash=location.hash) {
  const [page, query=''] = hash.replace(/^#/, '').split('?', 2);
  const candidate = new URLSearchParams(query).get('job') || '';
  return {page:page || 'overview', prepJob:page === 'prep' && /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(candidate) ? candidate : ''};
}
function routeHash(page) { return '#' + page + (page === 'prep' && state.prepJob ? '?job=' + encodeURIComponent(state.prepJob) : ''); }
function applyRoute() {
  const route = parseRoute();
  if (route.page === 'prep' && route.prepJob !== state.prepJob) {state.agentProposal=null;resetPrepGuide(); state.prepJob = route.prepJob;}
  state.page = route.page;
}
function noticeAppBuild(build) {
  if (!build || !loadedBuild || loadedBuild === '__JOBFLOW_BUILD__' || build === loadedBuild) return;
  state.appUpdateAvailable = true;
  const banner = $('#app-update');
  if (!banner) return;
  banner.hidden = false;
  banner.innerHTML = `<div><strong>An updated Jobflow is ready</strong><span>Save any open edits, then reload to get the latest workspace update. Your selected role stays in the link.</span></div>${button('Reload app','data-action="reload-app"','small primary')}`;
}
async function checkAppVersion() {
  try {
    const response = await fetch('/api/version', {cache:'no-store'});
    if (response.ok) noticeAppBuild((await response.json()).app_build);
  } catch { /* A later focus or periodic check can retry without disrupting work. */ }
}
function reloadApp() {
  if (state.busy || $('#action-dialog')?.open || $('.pdf-review-dialog')?.open) {toast('Save or close your open edit before reloading.'); return;}
  location.reload();
}
function navigate(page, filters) { if(filters){state.filters={...state.filters,...filters};if(page==='jobs'&&filters.company!==undefined)state.roleCompany=filters.company;}state.pageIndex=0;const hash=routeHash(page);if(location.hash===hash){state.page=page;render();}else location.hash=hash; }
function renderNav() {$('#account-link')?.setAttribute?.('aria-current',state.page==='account'?'page':'false');const incoming=jobs().filter(j=>active(j)&&localDay(j.first_seen)===today()).length;$('#navigation').innerHTML=pages.filter(p=>!p.utility).map(p=>`<a class="nav-item ${p.id===state.page?'active':''}" href="${routeHash(p.id)}" ${p.id===state.page?'aria-current="page"':''}>${icon(p.icon,'nav-icon')}<span>${p.label}</span>${p.id==='inbox'&&incoming?`<span class="nav-count">${fmt(incoming)}</span>`:''}</a>`).join('');$('#breadcrumb').textContent=pages.find(p=>p.id===state.page)?.label || 'Source health';$('#timezone-label').textContent=state.data?.timezone||'Local data, your control';}
function render() { if(!state.data)return;window.JobflowTrends?.destroy();window.JobflowMaps?.destroy();renderNav();const views={account:renderMyAccount,overview:renderOverview,inbox:renderJobs,jobs:renderJobs,analytics:renderAnalytics,applications:renderApplications,batches:renderApplicationBatches,resumes:renderResumes,connections:renderConnections,prep:renderPrep,sources:renderSources};$('#main').className=state.page==='overview'?'overview-page':state.page==='analytics'?'analytics-page':'';$('#main').innerHTML=(views[state.page]||renderOverview)();document.title=`${pages.find(p=>p.id===state.page)?.label||'Source health'} · Jobflow`;mountInsights();if(state.page==='batches')window.JobflowBatches?.mount({render,toast,csrfToken:state.data.csrf_token,showDialog:showAction,closeDialog:()=>$('#action-dialog').close(),startResumes:(ids,options)=>startResumeDraft(ids,{onReturn:()=>navigate('batches'),...options})});else window.JobflowBatches?.unmount();if(state.page==='resumes')window.JobflowResumes?.mount({render,toast,csrfToken:state.data.csrf_token});else window.JobflowResumes?.unmount();if(state.page==='account')window.JobflowMyAccount?.mount();else window.JobflowMyAccount?.unmount(); }
function renderMyAccount(){return window.JobflowMyAccount?window.JobflowMyAccount.render(state.data):empty('My account unavailable','Reload Jobflow to load your account page.');}
function renderResumes(){return window.JobflowResumes?window.JobflowResumes.render({jobs:jobs(),applications:applications(),today:today()}):empty('Resume library unavailable','Reload Jobflow to load your saved resume versions.');}
function startResumeDraft(jobIds,options={}){window.JobflowResumes?.start(jobIds,options);$('#detail-dialog')?.close();$('#action-dialog')?.close();navigate('resumes');}
function renderApplicationBatches(){return window.JobflowBatches?window.JobflowBatches.render({jobs:jobs(),applications:applications(),selected:[...state.selected],today:today(),timezone:state.data.timezone,company:state.filters.company,localDay}):empty('Application batches unavailable','Reload Jobflow to load the batch workspace.');}
function startApplicationBatch(ids=[...state.selected]){window.JobflowBatches?.start(ids);$('#action-dialog')?.close();$('#detail-dialog')?.close();navigate('batches');}
function stat(label,value,note,iconName,featured=false){return `<div class="stat ${featured?'featured':''}"><div class="stat-top">${label}<span class="stat-icon">${icon(iconName)}</span></div><div class="stat-value">${fmt(value)}</div><div class="stat-bottom">${note}</div></div>`;}
function renderOverview() {
  const current = jobs().filter(active);
  const newJobs = current.filter(j => localDay(j.first_seen) === today());
  const newest = [...newJobs].sort((a,b) => String(b.first_seen || '').localeCompare(String(a.first_seen || ''))).slice(0,3);
  const strong = current.filter(j => j.score >= 80);
  const apps = applications();
  const applied = apps.filter(a => a.applied_at || ['applied','recruiter','technical','onsite','offer'].includes(a.stage));
  const saved = apps.filter(a => a.stage === 'saved');
  const companies = companyCounts(current);
  const due = dueApps();
  const tasks = pendingTasks().filter(t => t.due_date === today());
  const budget = state.data.settings?.preparation?.daily_minutes || 0;
  const top = current.filter(j => !appById(j.id) || appById(j.id).stage === 'saved')
    .sort((a,b) => b.score-a.score || String(b.first_seen || '').localeCompare(String(a.first_seen || ''))).slice(0,5);
  return pageHeader('', 'Overview', prettyDate(today(),{weekday:'long',year:'numeric'}), button('Explore roles '+icon('arrow'),'data-action="explore"','primary')) +
  `<div class="stats-grid overview-stats">
    ${stat('Matching roles',current.length,fmt(companies.length)+' companies · includes listings to recheck','search')}
    ${stat('New today',newJobs.length,'First discovered in your workspace today','inbox')}
    ${stat('Strong matches',strong.length,'Profile overlap score of 80 or higher','star')}
    ${stat('Applications sent',applied.length,fmt(saved.length)+' saved · '+fmt(due.length)+' follow-ups due','applications')}
  </div>
  <div class="overview-insights-grid">
    <section class="card overview-trend-card" aria-labelledby="overview-trend-title">
      <div class="card-head"><div><h2 id="overview-trend-title">Discovery over time</h2><p>Compare cumulative discoveries across companies.</p></div><a class="text-link" href="#analytics">All insights →</a></div>
      ${renderTrend()}
    </section>
    <section class="card overview-map-card" aria-labelledby="overview-map-title">
      <div class="card-head"><div><h2 id="overview-map-title">Company locations</h2><p>Explore offices and matching roles.</p></div><button class="text-link" data-action="view-map">Expand map →</button></div>
      ${renderMap(current,false)}
    </section>
  </div>
  <section class="overview-matches" aria-labelledby="top-matches-title">
    <div class="section-heading"><div><h2 id="top-matches-title">Top matches</h2><p>High-scoring roles you haven’t applied to yet.</p></div><button class="text-link" data-action="strong">View strong matches →</button></div>
    <div class="card">${roleTable(top,false)}<div class="table-footer"><span>Scores reflect profile overlap, not hiring probability.</span><button class="text-link" data-action="explore">Explore all roles →</button></div></div>
  </section>
  <div class="overview-work-grid">
    <section class="card" aria-labelledby="overview-inbox-title">
      <div class="card-head"><div><h2 id="overview-inbox-title">Daily inbox</h2><p>${fmt(newJobs.length)} matching roles first discovered today</p></div></div>
      <div class="card-body">
        ${newest.length ? `<ul class="overview-role-list">${newest.map(j => `<li><button class="role-title" data-job="${esc(j.id)}">${esc(j.title)}</button><p class="role-subtitle">${esc(j.company)} · ${esc(j.location || 'Location unspecified')} ${availabilityChip(j)}</p></li>`).join('')}</ul>` : '<p class="card-note">No new matching roles today. Earlier discoveries remain in your inbox.</p>'}
        <a href="#inbox" class="text-link">Open inbox →</a>
      </div>
    </section>
    <section class="card" aria-labelledby="overview-apps-title">
      <div class="card-head"><div><h2 id="overview-apps-title">Applications</h2><p>${fmt(saved.length)} saved · ${fmt(due.length)} follow-ups due</p></div></div>
      <div class="card-body">
        ${due.length ? due.slice(0,3).map(a => `<div class="action-row"><span class="action-symbol">${icon('clock','nav-icon')}</span><div><h3>${esc(a.company || jobById(a.job_id)?.company || 'Application')}</h3><p>${esc(a.next_action || 'Follow-up due · review your next step.')}</p></div><button class="text-link" data-job="${esc(a.job_id)}">Review →</button></div>`).join('') : saved.length ? saved.slice(0,3).map(a => `<div class="action-row"><span class="action-symbol">${icon('applications','nav-icon')}</span><div><h3>${esc(a.company || jobById(a.job_id)?.company || 'Saved role')}</h3><p>${esc(jobById(a.job_id)?.title || a.title || 'Ready to review')}</p></div><button class="text-link" data-job="${esc(a.job_id)}">Review →</button></div>`).join('') : '<p class="card-note">No follow-ups due. Save a role to build your application queue.</p>'}
        <a href="#applications" class="text-link">Open applications →</a>
      </div>
    </section>
    <section class="card" aria-labelledby="overview-prep-title">
      <div class="card-head"><div><h2 id="overview-prep-title">Today’s preparation</h2><p>${fmt(tasks.length)} pending tasks · ${fmt(budget)}-minute daily budget</p></div></div>
      <div class="card-body">
        ${tasks.length ? tasks.slice(0,3).map(t => `<div class="action-row"><span class="action-symbol">${icon('prep','nav-icon')}</span><div><h3>${esc(t.title)}</h3><p>${esc(String(t.category || 'Practice').replaceAll('_',' '))} · ${esc(t.minutes || 0)} minutes</p></div><button class="text-link" data-task="${esc(t.id)}">${t.status === 'doing' ? 'Continue' : 'Start'} →</button></div>`).join('') : '<p class="card-note">No pending tasks scheduled today. Your role guides and interview plan are available in preparation.</p>'}
        <a href="#prep" class="text-link">Open preparation →</a>
      </div>
    </section>
  </div>`;
}
function renderTrend() {
  return window.JobflowTrends ? window.JobflowTrends.render(state.data) : empty('Chart unavailable', 'Reload the page to load the discovery chart.');
}
const cities = window.JobflowMaps?.cities || [];
const inCity = (job, city) => window.JobflowMaps?.inCity(job, city) || false;
function locationMatches(job, value) {
  const query = String(value || '').toLowerCase().trim();
  if (!query) return true;
  const city = cities.find(c => c.name.toLowerCase() === query);
  if (city) return inCity(job, city);
  return String(job.location || '').toLowerCase().includes(query) || (query === 'remote' && job.remote);
}
function renderMap(list, large) {
  return window.JobflowMaps ? window.JobflowMaps.render(list, large, state.city, {locations:mapOfficeLocations(list),reviews:state.data.location_reviews || [],scope:state.mapScope,mode:state.mapMode || 'cities',selectedOffice:state.mapOfficeId}) : empty('Map unavailable', 'Reload the page to load the street map.');
}
function mountInsights() {
  const main = $('#main');
  window.JobflowTrends?.mount(main);
  window.JobflowMaps?.mount(main, {onSelect(city) {
    state.city = city;
    state.mapOfficeId = '';
    state.mapSelectedCompany = ''; state.mapCoverageStatus = '';
    state.mapRoleQuery = '';
    if (state.page !== 'analytics') { state.focusMap = true; navigate('analytics'); return; }
    const detail = $('#map-selection');
    if (detail) detail.innerHTML = renderCityDetail(mapJobs());
  }, onOfficeSelect(office) {
    state.mapMode = 'offices'; state.mapOfficeId = office.id; state.city = office.city || ''; state.mapRoleQuery = '';
    state.mapSelectedCompany = ''; state.mapCoverageStatus = '';
    if (state.page !== 'analytics') {state.focusMap = true; navigate('analytics'); return;}
    const detail = $('#map-selection');
    if (detail) detail.innerHTML = renderCityDetail(mapJobs());
  }, onModeChange(mode) {
    state.mapMode = mode; state.mapOfficeId = ''; state.city = ''; state.mapRoleQuery = '';
    state.mapSelectedCompany = ''; state.mapCoverageStatus = '';
    const detail = $('#map-selection');
    if (detail) detail.innerHTML = renderCityDetail(mapJobs());
  }, onCompanySelect(company) {
    state.mapSelectedCompany = company; state.mapOfficeId = ''; state.city = ''; state.mapRoleQuery = ''; state.mapCoverageStatus = '';
    if (state.page !== 'analytics') {state.focusMap=true;navigate('analytics');return;}
    const detail = $('#map-selection');
    if (detail) detail.innerHTML = renderCityDetail(mapJobs());
  }, onScopeChange(scope) {
    state.mapScope = scope;
    state.mapOfficeId='';state.city='';state.mapSelectedCompany='';
    const heading = $('#map-area-title');
    if (heading) heading.textContent = scope === 'all' ? 'Explore all locations' : 'Explore the Bay Area';
    const detail = $('#map-selection');
    if (detail) detail.innerHTML = renderCityDetail(mapJobs());
  }});
}
function roleTable(list,selectable=true) {if(!list.length)return empty('No roles in this view','Try another company, score, location, or discovery date.');return `<div class="table-scroll"><table class="role-table"><thead><tr>${selectable?'<th class="check-cell"><input type="checkbox" id="select-page" aria-label="Select all roles on this page" '+(list.every(j=>state.selected.has(j.id))?'checked':'')+'></th>':''}<th>Company</th><th>Role</th><th class="location-col">Location</th><th>Match</th><th>${selectable?'Progress':'Base salary'}</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>${list.map(j=>{const app=appById(j.id);return `<tr>${selectable?`<td class="check-cell"><input type="checkbox" data-select="${esc(j.id)}" aria-label="Select ${esc(j.title)} at ${esc(j.company)}" ${state.selected.has(j.id)?'checked':''}></td>`:''}<td><div class="company-cell">${companyLogo(j.company)}<span class="company-name">${esc(j.company)}</span></div></td><td><button class="role-title" data-job="${esc(j.id)}">${esc(j.title)}</button><div class="role-subtitle">${esc((j.matched_skills||[]).slice(0,3).join(' · ') || j.department || '')} ${availabilityChip(j)}</div></td><td class="location-col"><div class="location">${esc(j.location||'Location unspecified')}</div></td><td>${scoreBadge(j.score)}</td><td>${selectable?(app?`<span class="chip ${app.stage==='offer'?'green':''}">${esc(stageLabels[app.stage]||app.stage)}</span>`:`<span class="muted tiny">Not saved</span>`):`<span class="tiny muted nowrap">${esc(compensation(j))}</span>`}</td><td><button class="icon-button" data-job="${esc(j.id)}" aria-label="View ${esc(j.title)}">↗</button></td></tr>`;}).join('')}</tbody></table></div>`;}
function filterOptions(groups=null) {
  const companies=[...new Set(jobs().map(j=>j.company))].sort();
  const companyField = groups ? '' : `<div class="field"><label for="company-filter">Company</label><select id="company-filter" data-filter="company"><option value="">All companies</option>${companies.map(c=>`<option ${state.filters.company===c?'selected':''}>${esc(c)}</option>`).join('')}</select></div>`;
  return `<div class="filters"><div class="field grow search-field"><label for="query">Search opportunities</label>${icon('search')}<input class="input search-input" id="query" data-filter="q" placeholder="Role, company, skill…" value="${esc(state.filters.q)}" autocomplete="off"></div>${companyField}<div class="field"><label for="score-filter">Match score</label><select id="score-filter" data-filter="score"><option value="">All matches</option>${[80,70,60,50,40].map(n=>`<option value="${n}" ${state.filters.score===String(n)?'selected':''}>${n}+ score</option>`).join('')}</select></div><div class="field"><label for="location-filter">Location</label><input id="location-filter" class="input" data-filter="location" placeholder="City or remote" value="${esc(state.filters.location)}" style="width:145px"></div><div class="field"><label for="availability-filter">Listing status</label><select id="availability-filter" data-filter="availability">${[['active','Current matches'],['open','Open listings'],['missing','Needs recheck'],['closed','Closed history'],['all','All recorded matches']].map(([v,l])=>`<option value="${v}" ${state.filters.availability===v?'selected':''}>${l}</option>`).join('')}</select></div><button class="clear-button" data-action="clear-filters">Reset</button></div>`;
}
function filteredJobs({ignoreCompany=false,sort=state.filters.sort}={}) {
  const f=state.filters,q=f.q.toLowerCase().trim();
  const list=jobs().filter(j=>(!q||`${j.title} ${j.company} ${j.location} ${(j.matched_skills||[]).join(' ')} ${j.department||''}`.toLowerCase().includes(q))
    &&(ignoreCompany||!f.company||j.company===f.company)&&(!f.score||Number(j.score)>=Number(f.score))&&locationMatches(j,f.location)
    &&(f.availability==='all'||f.availability==='active'&&active(j)||j.availability===f.availability)
    &&(state.page!=='inbox'||localDay(j.first_seen)===state.inboxDate));
  list.sort(sort==='newest'?(a,b)=>String(b.first_seen).localeCompare(String(a.first_seen)):sort==='company'?(a,b)=>a.company.localeCompare(b.company)||b.score-a.score:(a,b)=>b.score-a.score||String(b.first_seen).localeCompare(String(a.first_seen)));
  return list;
}
function exploreCompanyGroups() {
  const groups=new Map();
  for(const job of filteredJobs({ignoreCompany:true,sort:'score'})) {
    if(!groups.has(job.company))groups.set(job.company,[]);
    groups.get(job.company).push(job);
  }
  const ranked=[...groups].map(([company,roles])=>({company,roles,
    topScore:Number(roles[0]?.score||0),
    averageScore:roles.reduce((sum,j)=>sum+Number(j.score||0),0)/roles.length,
    strongMatches:roles.filter(j=>Number(j.score)>=80).length,
  }));
  const compare=state.companySort==='score'?(a,b)=>b.topScore-a.topScore||b.averageScore-a.averageScore||b.roles.length-a.roles.length
    :state.companySort==='average'?(a,b)=>b.averageScore-a.averageScore||b.roles.length-a.roles.length||b.topScore-a.topScore
    :state.companySort==='strong'?(a,b)=>b.strongMatches-a.strongMatches||b.averageScore-a.averageScore||b.roles.length-a.roles.length
    :(a,b)=>b.roles.length-a.roles.length||b.topScore-a.topScore||b.averageScore-a.averageScore;
  return ranked.sort((a,b)=>compare(a,b)||a.company.localeCompare(b.company));
}
function rolesInView(groups) {
  if(state.page!=='jobs'||state.rolesView!=='companies')return filteredJobs();
  groups ||= exploreCompanyGroups();
  if(!groups.some(g=>g.company===state.roleCompany)) {
    state.roleCompany='';
    state.pageIndex=0;
  }
  return groups.find(g=>g.company===state.roleCompany)?.roles||[];
}
function switchRolesView(view) {
  if(!['list','companies'].includes(view)||view===state.rolesView)return;
  clearTimeout(state.searchTimer);
  state.rolesView=view;
  if(view==='companies')state.roleCompany='';
  state.pageIndex=0;
  render();
  document.querySelector(`[data-roles-view="${view}"]`)?.focus();
}
function chooseRoleCompany(company) {
  clearTimeout(state.searchTimer);
  state.roleCompany=company;
  state.pageIndex=0;
  render();
  $('#company-role-heading')?.focus({preventScroll:true});
}
function renderCompanyTiles(groups) {
  if(!groups.length)return `<section class="card">${empty('No companies match these filters','Try another search, score, location, or listing status.')}</section>`;
  return `<section class="company-browser" aria-label="Browse companies"><div class="company-browser-caption"><span><strong>${fmt(groups.length)}</strong> companies · Choose one to see its roles</span><div class="company-sort-control"><label for="company-sort">Rank companies</label><select id="company-sort">${[['roles','Most matching roles'],['score','Highest match score'],['average','Average match score'],['strong','Most strong matches (80+)']].map(([value,label])=>`<option value="${value}" ${state.companySort===value?'selected':''}>${label}</option>`).join('')}</select></div></div><div class="company-tiles">${groups.map(g=>{
    const rankingLabel=state.companySort==='average'?'Average match':state.companySort==='strong'?'80+ matches':'Top match';
    const rankingValue=state.companySort==='average'?g.averageScore.toFixed(1):state.companySort==='strong'?fmt(g.strongMatches):g.topScore;
    return `<button class="company-tile" data-role-company="${esc(g.company)}" aria-label="View ${fmt(g.roles.length)} roles at ${esc(g.company)}">${companyLogo(g.company)}<strong class="company-tile-name">${esc(g.company)}</strong><span class="company-tile-count">${fmt(g.roles.length)} ${g.roles.length===1?'role':'roles'}</span><span class="company-tile-score">${rankingLabel} <strong>${esc(rankingValue)}</strong><span aria-hidden="true">↗</span></span></button>`;
  }).join('')}</div></section>`;
}
function selectionBar(){return state.selected.size?`<div class="selection-bar"><strong>${fmt(state.selected.size)} roles selected</strong>${button('Save roles','data-action="save-selected"')}${button('Review application queue '+icon('arrow'),'data-action="queue"','primary')}<button class="button small" data-action="clear-selection">Clear</button></div>`:'';}
function renderJobs() {
  const daily=state.page==='inbox',byCompany=!daily&&state.rolesView==='companies';
  const groups=byCompany?exploreCompanyGroups():null,list=rolesInView(groups),size=50,pages=Math.max(1,Math.ceil(list.length/size));
  const companyOverview=byCompany&&!state.roleCompany;
  state.pageIndex=Math.max(0,Math.min(state.pageIndex,pages-1));
  state.visibleJobs=list.slice(state.pageIndex*size,(state.pageIndex+1)*size);
  const viewSwitch=daily?'':`<div class="roles-view-switch" role="group" aria-label="Explore roles view"><button data-roles-view="list" aria-pressed="${!byCompany}">${icon('applications')}<span>All roles</span></button><button data-roles-view="companies" aria-pressed="${byCompany}">${icon('overview')}<span>Companies</span></button></div>`;
  const sortControl=byCompany?'<span class="company-ranking-note">Highest score first · newest breaks ties</span>':`<label class="sr-only" for="sort-filter">Sort roles</label><select id="sort-filter" data-filter="sort">${[['score','Highest match'],['newest','Newest first'],['company','Company A–Z']].map(([v,l])=>`<option value="${v}" ${state.filters.sort===v?'selected':''}>${l}</option>`).join('')}</select>`;
  const resultTitle=byCompany&&state.roleCompany?`<div class="company-results-title">${companyLogo(state.roleCompany)}<div><h2 id="company-role-heading" tabindex="-1">${esc(state.roleCompany)}</h2><span><strong>${fmt(list.length)}</strong> matching roles</span></div></div>`:`<span><strong>${fmt(list.length)}</strong> matching roles ${!byCompany&&state.filters.company?`<span class="search-tag">${esc(state.filters.company)}</span>`:''}</span>`;
  const header=pageHeader(daily?'A FRESH PERSPECTIVE':'FIND YOUR FIT',daily?'Daily inbox':'Explore roles',daily?'Roles first discovered on the selected date.':companyOverview?'Choose a company to explore your matching roles.':'Search and filter your matching roles.',daily?`<div class="field filter-date"><label for="inbox-date">Discovery date</label><input type="date" id="inbox-date" value="${esc(state.inboxDate)}" max="${today()}"></div>`:button('View application queue'+(state.selected.size?' · '+state.selected.size:''),'data-action="queue"','primary'))
    +(daily?`<div class="banner"><div><strong>${fmt(list.length)} opportunities first discovered ${state.inboxDate===today()?'today':'on '+prettyDate(state.inboxDate)}</strong><span>Discovery dates use ${esc(state.data.timezone)}. Employer posting dates may differ.</span></div>${button('Select all results','data-action="select-results"','small')}</div>`:`<div class="roles-view-heading">${viewSwitch}<span class="muted">${byCompany?'Browse companies. Open one to see its roles.':'All companies, one ranked list.'}</span></div>`)
    +filterOptions(groups);
  if(companyOverview)return header+renderCompanyTiles(groups)+selectionBar();
  return header+(byCompany?'<button class="text-link company-back" data-action="companies-back">← Back to companies</button>':'')
    +`<section ${byCompany?'id="company-role-panel" aria-labelledby="company-role-heading"':''}><div class="result-heading">${resultTitle}<div class="button-group"><button class="text-link" data-action="select-results" ${list.length?'':'disabled'}>${byCompany?'Select all company roles':'Select all results'}</button>${sortControl}</div></div><div class="card">${roleTable(state.visibleJobs)}<div class="table-footer"><span>${list.length?fmt(state.pageIndex*size+1)+'–'+fmt(Math.min(list.length,(state.pageIndex+1)*size))+' of '+fmt(list.length)+' roles':'No results'} · includes recheck notices</span><div class="pagination"><button class="button small" data-action="previous" aria-label="Previous page" ${state.pageIndex===0?'disabled':''}>←</button><span>${state.pageIndex+1} / ${pages}</span><button class="button small" data-action="next" aria-label="Next page" ${state.pageIndex>=pages-1?'disabled':''}>→</button></div></div></div></section>${selectionBar()}`;
}
function mapJobs() {
  return jobs().filter(j => active(j) && (!state.mapCompany || j.company === state.mapCompany) && (!state.mapScore || Number(j.score) >= Number(state.mapScore)));
}
function mapOfficeLocations(list = mapJobs()) {
  const recorded = [...(state.data.locations || []),...list.flatMap(job => job.office_locations || [])];
  return window.JobflowMaps?.officeRows(recorded,list) || [];
}
function selectedMapOffice(list = mapJobs()) {return mapOfficeLocations(list).find(office => office.id === state.mapOfficeId);}
function mapCompanyDirectory(list) {
  return window.JobflowMaps?.companyRows(list,mapOfficeLocations(list),state.data.location_reviews || []) || [];
}
function renderMapCoverage(list) {
  const directory = mapCompanyDirectory(list), counts = {pin:0,address_only:0,remote_only:0,unverified:0};
  directory.forEach(company => counts[company.status]++);
  return `<div class="map-coverage"><div class="map-coverage-heading"><strong>${directory.length} companies</strong><button class="text-link" data-map-coverage="" aria-pressed="${!state.mapCoverageStatus}">Show all</button></div><div class="map-coverage-filters" role="group" aria-label="Company location coverage">${[['pin','With pins'],['address_only','Address only'],['remote_only','Remote only'],['unverified','Not verified']].map(([status,label])=>`<button type="button" data-map-coverage="${status}" aria-pressed="${state.mapCoverageStatus === status}"><strong>${counts[status]}</strong><span>${label}</span></button>`).join('')}</div></div>`;
}
function renderLocationReview(company) {
  const review = company.review;
  const label = company.status === 'remote_only' ? 'Remote-only company' : 'Office location not verified';
  return `<div class="map-location-review"><strong>${label}</strong><p>${company.status === 'remote_only' ? 'No office pin is shown. Check each posting for eligible work locations.' : 'This company remains in your results. A verified office address is not available yet.'}</p>${review && (review.observed_at || review.sources?.length) ? `<details class="map-office-evidence"><summary>Research details${review.observed_at ? ' · checked ' + esc(prettyDate(review.observed_at)) : ''}</summary><p class="map-office-note">${esc(review.text || '')}</p><div class="map-office-links">${(review.sources || []).map(source => external(source.url,source.title || 'Source ↗')).join('')}</div></details>` : ''}</div>`;
}
function renderOfficeReference(office, showCompany = false) {
  const hasPin = window.JobflowMaps?.hasCoordinates(office);
  const confirmed = office.relationship === 'job_site' && office.worksite_confirmed === true;
  const mapsURL = 'https://www.google.com/maps/search/?api=1&query=' + encodeURIComponent(office.address);
  const observed = /^\d{4}-\d{2}-\d{2}$/.test(office.observed_at || '') ? `Checked ${prettyDate(office.observed_at)}` : '';
  const evidence = [office.text,hasPin ? office.coordinate_note : ''].filter(Boolean);
  return `<article class="map-office-reference${confirmed ? ' is-confirmed' : ''}">
    ${showCompany ? `<strong class="map-office-company">${esc(office.company)}</strong>` : ''}
    <span class="map-office-relationship">${confirmed ? 'Confirmed role location' : 'Company office · role assignment unconfirmed'}</span>
    <p class="map-office-address">${icon('pin')}<span>${esc(office.address)}</span></p>
    <p class="map-office-precision">${hasPin ? office.precision === 'address' ? 'Approximate address pin' : 'Office pin' : 'Verified address · coordinates unavailable'}${observed ? ' · ' + esc(observed) : ''}</p>
    <div class="map-office-links">${hasPin ? `<button class="text-link" data-map-office-focus="${esc(office.id)}">Show on map</button>` : ''}${external(office.source_url,'Address source ↗')}${hasPin && office.coordinate_source_url && office.coordinate_source_url !== office.source_url ? external(office.coordinate_source_url,'Pin source ↗') : ''}${external(mapsURL,'Google Maps ↗')}</div>
    ${evidence.length ? `<details class="map-office-evidence"><summary>Location evidence</summary>${evidence.map(note=>`<p class="map-office-note">${esc(note)}</p>`).join('')}</details>` : ''}
  </article>`;
}
function mapRoleGroups(list) {
  const city = cities.find(c => c.name === state.city);
  const office = selectedMapOffice(list);
  const query = String(state.mapRoleQuery || '').trim().toLocaleLowerCase();
  const grouped = new Map(), seen = new Set();
  const directory = new Map(mapCompanyDirectory(list).map(company => [company.company,company]));
  for (const job of list) {
    if (seen.has(job.id) || (office && job.company !== office.company)) continue;
    if (state.mapSelectedCompany && job.company !== state.mapSelectedCompany) continue;
    if (state.mapCoverageStatus && directory.get(job.company)?.status !== state.mapCoverageStatus) continue;
    if (office?.relationship === 'job_site' && office.worksite_confirmed === true) {
      if (!(office.jobs || []).some(current => current.id === job.id)) continue;
    } else if (office ? !locationMatches(job,office.city) : city && !inCity(job,city)) continue;
    if (query && !`${job.company} ${job.title} ${job.location || ''} ${(directory.get(job.company)?.offices || []).map(office=>office.address).join(' ')}`.toLocaleLowerCase().includes(query)) continue;
    seen.add(job.id);
    if (!grouped.has(job.company)) grouped.set(job.company, []);
    grouped.get(job.company).push(job);
  }
  return [...grouped].map(([company, roles]) => ({company, roles: roles.sort((a,b) => Number(b.score || 0) - Number(a.score || 0) || a.title.localeCompare(b.title))}))
    .sort((a,b) => b.roles.length - a.roles.length || a.company.localeCompare(b.company));
}
function mapGroupKey(company) { return JSON.stringify([state.city || '', company]); }
function renderMapCompanyResults(list) {
  const groups = mapRoleGroups(list);
  const offices = mapOfficeLocations(list), selectedOffice = selectedMapOffice(list);
  const count = groups.reduce((n, group) => n + group.roles.length, 0);
  const companyBrowse = selectedOffice ? `<button class="text-link map-office-browse" data-map-company-view="${esc(selectedOffice.company)}">View all ${esc(selectedOffice.company)} roles →</button>` : '';
  const directory = new Map(mapCompanyDirectory(list).map(company => [company.company,company]));
  return (selectedOffice ? `<div class="map-selected-office">${renderOfficeReference(selectedOffice,true)}${selectedOffice.relationship === 'job_site' && selectedOffice.worksite_confirmed === true ? '<p>Only roles explicitly linked to this address by a posting appear below. Refer to the posting for remote or telecommuting terms.</p>' : ''}${companyBrowse}</div>` : '') + `<div class="map-results-count" role="status">${fmt(count)} matching roles · ${groups.length} ${groups.length === 1 ? 'company' : 'companies'}</div>` +
    (groups.map(({company, roles}, index) => {
      const key = mapGroupKey(company), limit = state.mapRoleLimits?.get(key) || 6;
      const open = state.mapGroupOpen?.has(key) ? state.mapGroupOpen.get(key) : index === 0;
      const displayed = roles.slice(0, limit);
      const companyEntry = directory.get(company);
      const companyOffices = (window.JobflowMaps?.officeSites(offices) || offices).filter(office => office.company === company && (!state.city || office.city === state.city) && !office.records?.some(record => record.id === selectedOffice?.id));
      return `<details class="map-company-group" data-map-group="${esc(company)}" ${open ? 'open' : ''}>
        <summary>${companyLogo(company)}<span class="map-group-name">${esc(company)}<small class="map-company-coverage is-${esc(companyEntry?.status || 'unverified')}">${esc(window.JobflowMaps?.coverageLabels?.[companyEntry?.status] || 'Location not verified')}</small></span><span class="map-group-count">${fmt(roles.length)} ${roles.length === 1 ? 'role' : 'roles'}</span><span class="map-group-chevron" aria-hidden="true">⌄</span></summary>
        <div class="map-company-positions">${companyOffices.length ? `<div class="map-company-offices">${companyOffices.map(office=>renderOfficeReference(office)).join('')}</div>` : companyEntry && !companyEntry.offices.length ? renderLocationReview(companyEntry) : ''}<div class="map-company-caption"><span>Employer-stated role locations</span><button class="text-link" data-company-city="${esc(company)}" data-city-name="${esc(state.city || '')}">Browse company →</button></div>
        ${displayed.map(job => `<article class="map-position"><div class="map-position-heading"><button class="role-title" data-job="${esc(job.id)}">${esc(job.title)}</button>${scoreBadge(job.score)}</div><div class="map-position-location">${icon('pin')}<span>${esc(job.location || 'Location not provided')}</span></div>${(job.office_locations || []).filter(office=>office.relationship === 'job_site' && office.worksite_confirmed === true && (office.job_ids || []).includes(job.id)).map(office=>`<div class="map-position-worksite"><strong>Confirmed role location</strong><span>${esc(office.address)}</span>${/^\d{4}-\d{2}-\d{2}$/.test(office.observed_at || '') ? `<span class="map-office-precision">Checked ${esc(prettyDate(office.observed_at))}</span>` : ''}${external(job.url,'Posting source ↗')}${office.text ? `<details class="map-office-evidence"><summary>Location evidence</summary><p class="map-office-note">${esc(office.text)}</p></details>` : ''}</div>`).join('')}<div class="map-position-meta">${availabilityChip(job)}${external(job.url, 'Employer listing ↗', 'map-employer-link')}</div></article>`).join('')}
        ${roles.length > displayed.length ? `<button class="map-more-roles" data-map-more="${esc(company)}">Show ${Math.min(25, roles.length - displayed.length)} more roles <span>${displayed.length} of ${fmt(roles.length)}</span></button>` : ''}</div>
      </details>`;
    }).join('') || '<p class="map-results-empty">No matching roles in this city or search. Try another company, location, or match score.</p>');
}
function renderCityDetail(list) {
  const selected = cities.find(c => c.name === state.city);
  const office = selectedMapOffice(list), cityName = selected?.name || office?.city || '';
  return `<div class="map-detail map-role-explorer"><div class="map-results-heading"><div><span class="analytics-kicker">POSITIONS BY COMPANY</span><h3>${esc(state.mapSelectedCompany || cityName || 'All locations')}</h3></div>${cityName ? `<button class="text-link" data-city-browse="${esc(cityName)}">Browse roles →</button>` : ''}${cityName || state.mapSelectedCompany ? '<button class="text-link" data-map-clear="true">Clear selection</button>' : ''}</div>
    ${renderMapCoverage(list)}
    <label class="map-role-search">${icon('search')}<input id="map-role-query" type="search" value="${esc(state.mapRoleQuery || '')}" placeholder="Company, address, role…" aria-label="Search companies, addresses and roles" autocomplete="off"></label>
    <p class="map-location-precision">All companies remain browsable, including those without pins. Map area changes the pins shown. Office references do not confirm a role’s worksite.</p>
    <div class="map-company-results" id="map-company-results">${renderMapCompanyResults(list)}</div></div>`;
}
function renderAnalytics() {
  const list = jobs().filter(active), companies = companyCounts(list), mapped = mapJobs();
  const remote = list.filter(j=>j.remote || /remote/i.test(j.location));
  return pageHeader('MAKE SENSE OF THE SEARCH', 'Insights & map', 'Monitor discoveries, compare companies, and explore where your matches are.') +
    `<div class="stats-grid analytics-stats">${stat('Current matching roles',list.length,'Open listings + listings needing recheck','applications')}${stat('Companies in view',companies.length,'With at least one current match','overview')}${stat('Remote opportunities',remote.length,'Based on the recorded location or flag','map')}${stat('Strong-match share',list.length?Math.round(list.filter(j=>j.score>=80).length/list.length*100):0,'Percent of current matches at 80+','star')}</div>
    <section class="card analytics-trend-card"><div class="card-head"><div><span class="analytics-kicker">DISCOVERY MONITOR</span><h2>Discovery over time</h2><p>Cumulative role discoveries · compare companies and inspect exact counts</p></div><span class="analytics-snapshot">Snapshot · ${esc(prettyDate(state.data.generated_at || today()))}</span></div>${renderTrend()}</section>
    <div class="analytics-grid analytics-location-grid"><section class="card large-map"><div class="card-head"><div><span class="analytics-kicker">LOCATION EXPLORER</span><h2 id="map-area-title">${state.mapScope === 'all' ? 'Explore all locations' : 'Explore the Bay Area'}</h2><p>Select a city or office, or browse every company in the directory</p></div></div>
      <div class="map-filter-bar"><div class="field"><label for="map-company-filter">Company</label><select id="map-company-filter"><option value="">All companies</option>${[...companies].sort((a,b)=>a[0].localeCompare(b[0])).map(([c])=>`<option value="${esc(c)}" ${state.mapCompany===c?'selected':''}>${esc(c)}</option>`).join('')}</select></div><div class="field"><label for="map-score-filter">Match score</label><select id="map-score-filter"><option value="">All matches</option>${[80,70,60,50].map(n=>`<option value="${n}" ${state.mapScore===String(n)?'selected':''}>${n}+ score</option>`).join('')}</select></div><span class="map-filter-count">${fmt(mapped.length)} matching roles</span></div>
      ${renderMap(mapped,true)}</section>
      <section class="card analytics-company-card map-positions-card" aria-label="Positions grouped by company"><div id="map-selection">${renderCityDetail(mapped)}</div></section></div>`;
}
function applicationCard(a){const j=jobById(a.job_id)||a;return `<article class="application-card"><div class="company-cell">${companyLogo(j.company)}<span class="company-name">${esc(j.company)}</span></div><button class="role-title" data-job="${esc(a.job_id)}">${esc(j.title||a.title)}</button><p>${a.applied_at?'Applied '+prettyDate(a.applied_at):a.stage==='saved'?'Saved · not recorded as submitted':'Submission date not recorded'}</p>${a.referral_status?`<div style="margin-top:9px"><span class="chip green">Referral ${esc(a.referral_status)}</span></div>`:''}${a.follow_up_date?`<p class="${a.follow_up_date<=today()?'due':''}">Follow-up ${prettyDate(a.follow_up_date)}${a.follow_up_date<=today()?' · due':''}</p>`:''}${a.next_action?`<p>${esc(a.next_action)}</p>`:''}${a.response?`<p>Response: ${esc(a.response).slice(0,180)}</p>`:''}<div class="button-group">${button('Update progress',`data-track="${esc(a.job_id)}"`,'small')}${button('Prep →',`data-prep-job="${esc(a.job_id)}"`,'small ghost')}</div></article>`;}
function renderApplications(){const apps=applications(),groups=[{title:'Saved',stages:['saved']},{title:'Applied',stages:['applied']},{title:'In conversation',stages:['recruiter','technical','onsite']},{title:'Outcomes',stages:['offer','rejected','withdrawn']}];return pageHeader('EVERY MOVE, IN ONE PLACE','Applications','Track saved roles, application stages, and follow-ups.',button('Explore more roles '+icon('plus'),'data-action="explore"','primary'))+(dueApps().length?`<div class="banner amber"><div><strong>${dueApps().length} follow-up${dueApps().length===1?'':'s'} ready for your attention</strong>Review the recorded next action before following up.</div></div>`:'')+(apps.length?`<div class="pipeline">${groups.map(g=>{const rows=apps.filter(a=>g.stages.includes(a.stage));return `<section class="pipeline-column"><h2 class="column-title">${g.title}<span>${rows.length}</span></h2>${rows.map(a=>(g.stages.length>1?`<span class="chip" style="margin-bottom:7px">${esc(stageLabels[a.stage])}</span>`:'')+applicationCard(a)).join('')||'<div class="empty" style="padding:22px 6px;font-size:10px">Room for your next opportunity.</div>'}</section>`;}).join('')}</div>`:`<section class="card">${empty('Your first application starts with a saved role','Explore your matches, choose a role, and save it here. Application status only changes when you record it.')}</section>`)+`<div class="card-note">Opening an employer page does not mark a role as applied. Record a submission after you complete it.</div>`;}
// Relationship evidence is rendered as recorded; a public prospect never creates an edge.
function namedMutuals(contact) {
  if (contact.relationship !== 'mutual') return [];
  const recorded = Array.isArray(contact.mutual_connections) ? contact.mutual_connections : [];
  const candidates = recorded.filter(p => p && typeof p.name === 'string' && p.name.trim());
  if (!candidates.length && typeof contact.via === 'string' && contact.via.trim()) {
    candidates.push({name: contact.via.trim(), profile_url: contact.via_profile_url || ''});
  }
  const seen = new Set();
  return candidates.filter(person => {
    const key = person.name.trim().toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
function connectionKind(contact) {
  if (contact.relationship === 'direct') return 'direct';
  return contact.relationship === 'mutual' && namedMutuals(contact).length ? 'mutual' : 'public';
}
function connectionSource(contact) {
  return contact.relationship_source === 'linkedin_signed_in_observation'
    ? 'Observed in your signed-in LinkedIn account'
    : 'From your contact record';
}
function contactJobIds(contact) {
  return Array.isArray(contact.related_job_ids) ? contact.related_job_ids.filter(id => typeof id === 'string')
    : contact.job_id ? [contact.job_id] : [];
}
function relevantToRole(contact, job) {
  if (!job) return false;
  const ids = contactJobIds(contact);
  return ids.length ? ids.includes(job.id) : String(contact.company || '').toLowerCase() === String(job.company || '').toLowerCase();
}
function contactCard(contact) {
  const kind = connectionKind(contact), mutuals = namedMutuals(contact);
  const url = contact.profile_url || contact.evidence_url;
  const related = contactJobIds(contact).map(jobById).filter(Boolean);
  const observed = contact.observed_at || contact.source_date;
  const label = kind === 'direct' ? 'Direct connection · 1 degree'
    : kind === 'mutual' ? `Named mutual path · 2 degrees` : 'Prospect · no recorded connection path';
  return `<article class="contact-card">
    <span class="contact-avatar">${esc(initials(contact.name))}</span>
    <div class="contact-info">
      <h3>${external(url, contact.name)}</h3>
      <p>${esc(contact.title || 'Professional contact')} · ${esc(contact.company)}</p>
      <div class="contact-badges"><span class="chip ${kind === 'direct' ? 'green' : kind === 'mutual' ? 'blue' : 'amber'}">${label}</span></div>
      ${kind === 'mutual' ? `<ul class="contact-routes" aria-label="Named mutual connection paths">${mutuals.map(person => `<li>You <span aria-hidden="true">→</span> ${external(person.profile_url, person.name)} <span aria-hidden="true">→</span> ${esc(contact.name)}</li>`).join('')}</ul>` : ''}
      ${contact.connection_degree === 2 && kind === 'public' ? '<p class="notes">LinkedIn displayed 2nd degree, but a named mutual was not recorded. No introduction path is drawn.</p>' : ''}
      ${related.length ? `<div class="contact-role-context"><strong>Role context</strong>${related.map(job => {
        const reason = contact.role_relevance && typeof contact.role_relevance === 'object' ? contact.role_relevance[job.id] : '';
        return `<div><button class="text-link" data-job="${esc(job.id)}">${esc(job.title)} ↗</button>${typeof reason === 'string' && reason ? `<p>${esc(reason)}</p>` : ''}</div>`;
      }).join('')}</div>` : ''}
      ${contact.notes ? `<p class="notes">${esc(contact.notes)}</p>` : ''}
      <p class="connection-scope">Hiring responsibility for an individual opening is unconfirmed unless specifically documented in the evidence.</p>
      <p class="note-source">${esc(connectionSource(contact))}<br>Observed ${observed ? esc(prettyDate(observed, {year:'numeric'})) : 'date not recorded'} ${safeURL(contact.evidence_url) ? '· ' + external(contact.evidence_url, 'View evidence ↗') : ''}</p>
    </div>
  </article>`;
}
function connectionGraph(contacts) {
  const orderedContacts = [...contacts].sort((a, b) =>
    Number(connectionKind(b) === 'direct') - Number(connectionKind(a) === 'direct') ||
    String(a.company || '').localeCompare(String(b.company || '')) ||
    String(a.name || '').localeCompare(String(b.name || '')));
  const paths = orderedContacts.flatMap(contact => {
    const kind = connectionKind(contact);
    return kind === 'direct' ? [{contact, via:null}] : kind === 'mutual'
      ? namedMutuals(contact).map(via => ({contact, via})) : [];
  });
  if (!paths.length) return empty('No supported paths in this view', 'Direct connections and named mutuals appear here when their relationship evidence is recorded. Public affiliation alone does not create a connection.', '⌘');
  return `<div class="connection-graph" aria-label="Recorded professional connection paths">
    <div class="path-legend"><span><i class="legend-dot" style="background:#416d52"></i> Direct</span><span><i class="legend-dot" style="background:#7b9eae"></i> Via a named mutual</span><span>${fmt(paths.length)} recorded path${paths.length === 1 ? '' : 's'}</span></div>
    <ol class="connection-paths">${paths.map(({contact,via}) => `<li class="connection-path ${via ? 'mutual' : 'direct'}" aria-label="${esc('You → ' + (via ? via.name + ' → ' : '') + contact.name)}">
      <div class="path-node path-you"><span class="path-avatar">You</span></div>
      <span class="path-edge" aria-hidden="true">→</span>
      ${via ? `<div class="path-node path-mutual"><span class="path-avatar">${esc(initials(via.name))}</span><div>${external(via.profile_url, via.name)}<small>Named mutual</small></div></div><span class="path-edge" aria-hidden="true">→</span>` : ''}
      <div class="path-node path-target"><span class="path-avatar">${esc(initials(contact.name))}</span><div>${external(contact.profile_url || contact.evidence_url, contact.name)}<small>${esc(contact.company)}</small></div></div>
    </li>`).join('')}</ol>
  </div>`;
}
function renderConnections() {
  const allContacts = state.data.contacts || [];
  const savedRoles = applications().filter(a => a.stage === 'saved').map(a => jobById(a.job_id) || {id:a.job_id, company:a.company, title:a.title});
  const companies = [...new Set([...allContacts.map(c => c.company), ...savedRoles.map(j => j.company)].filter(Boolean))].sort();
  const focusRole = savedRoles.find(job => job.id === state.contactJob);
  let contacts = allContacts.filter(contact => !state.contactCompany || contact.company === state.contactCompany);
  if (state.contactJob === 'saved') contacts = contacts.filter(contact => savedRoles.some(job => relevantToRole(contact, job)));
  else if (state.contactJob) contacts = contacts.filter(contact => relevantToRole(contact, focusRole));
  const direct = contacts.filter(c => connectionKind(c) === 'direct');
  const mutual = contacts.filter(c => connectionKind(c) === 'mutual');
  const prospects = contacts.filter(c => connectionKind(c) === 'public');
  const observations = contacts.filter(c => c.relationship_source === 'linkedin_signed_in_observation');
  return pageHeader('PEOPLE MAKE THE DIFFERENCE', 'Connections', 'Recorded connections and relationship evidence for your saved roles.') +
    `<div class="filters connection-filters">
      <div class="field grow"><label for="contact-role">Role focus</label><select id="contact-role"><option value="saved" ${state.contactJob === 'saved' ? 'selected' : ''}>All saved roles</option><option value="" ${state.contactJob === '' ? 'selected' : ''}>All recorded contacts</option>${savedRoles.map(job => `<option value="${esc(job.id)}" ${state.contactJob === job.id ? 'selected' : ''}>${esc(job.company)} · ${esc(job.title)}</option>`).join('')}</select></div>
      <div class="field"><label for="contact-company">Company</label><select id="contact-company"><option value="">All companies</option>${companies.map(company => `<option value="${esc(company)}" ${state.contactCompany === company ? 'selected' : ''}>${esc(company)}</option>`).join('')}</select></div>
    </div>
    <div class="stats-grid connection-stats">${stat('Direct target contacts', direct.length, 'First-degree contacts in this view', 'connections')}${stat('Targets via mutuals', mutual.length, 'At least one named mutual recorded', 'connections')}${stat('Other prospects', prospects.length, 'No recorded introduction path', 'search')}</div>
    ${observations.length ? `<div class="banner"><div><strong>Relationship evidence from your LinkedIn account</strong>${fmt(observations.length)} contact${observations.length === 1 ? '' : 's'} in this view have dated signed-in observations. This is a saved research snapshot, not a continuously synced network.</div></div>` : ''}
    <section class="card"><div class="card-head"><div><h2>Your connection paths</h2><p>Every displayed edge comes from a contact record or dated account observation.</p></div></div>${connectionGraph(contacts)}<div class="card-body" style="padding-top:0"><p class="card-note">A connection path does not establish referral willingness or ownership of an opening. LinkedIn may show only part of your network; absence here does not mean no path exists.</p></div></section>
    <div class="network-grid connection-evidence-grid"><section class="card"><div class="card-head"><div><h2>People you can reach through your network</h2><p>Profile links, named mutuals, relevant roles, and the evidence date</p></div></div><div class="card-body">${[...direct,...mutual].map(contactCard).join('') || empty('No direct or named mutual paths in this view', 'Choose another role or company, or review the recorded prospects. No relationship is inferred from company affiliation.')}</div></section>
    <section class="card"><div class="card-head"><div><h2>Prospects to research further</h2><p>Relationship or named introduction path not established</p></div></div><div class="card-body">${prospects.map(contactCard).join('') || empty('No other prospects in this view', 'Contacts without a supported personal path remain separate from the network graph.')}</div></section></div>`;
}
function resourceCard(r){const kind=r.kind||r.role||'Research note';const title=r.title||kind.replaceAll('_',' ');return `<article class="resource-card"><div class="research-type">${esc(kind.replaceAll('_',' '))} · ${esc(r.observed_at||r.retrieved_at?.slice(0,10)||'Date not recorded')}</div><h3 style="margin-top:6px">${esc(title)}</h3><p>${esc(r.text||r.summary||'')}</p><ul class="source-list">${(r.sources||[]).map(s=>`<li>${external(typeof s==='string'?s:s.url,typeof s==='string'?s:s.title||s.url)} ↗</li>`).join('')}</ul></article>`;}
function taskRow(t){return `<div class="task-row"><button class="task-check ${t.status==='done'?'done':''}" data-task="${esc(t.id)}" aria-label="${t.status==='done'?'View completed task':'Update task'}: ${esc(t.title)}">${t.status==='done'?'✓':t.status==='doing'?'•':''}</button><div class="task-info"><h3>${esc(t.title)}</h3><p>${esc((t.category||'Practice').replaceAll('_',' '))} · ${esc(t.company||'General preparation')} ${t.status==='done'?'· Completed':t.status==='doing'?'· In progress':''}</p>${t.notes?`<details><summary>Practice notes & context</summary><p>${esc(t.notes)}</p>${safeURL(t.source_url)?`<p>${external(t.source_url,'Open source material ↗')}</p>`:''}</details>`:safeURL(t.source_url)?`<p>${external(t.source_url,'Open source material ↗')}</p>`:''}</div><span class="task-minutes">${t.actual_minutes!==undefined?`${t.actual_minutes} min logged`:`${t.minutes||0} min planned`}</span></div>`;}
function resetPrepGuide() {
  state.prepGuideRequest = (state.prepGuideRequest || 0) + 1;
  state.prepGuideFor = '';
  state.prepGuide = null;
  state.prepGuideError = '';
}
async function loadPrepGuide(jobId, force=false) {
  if (!jobId || (!force && state.prepGuideFor === jobId)) return;
  const request = (state.prepGuideRequest || 0) + 1;
  state.prepGuideRequest = request;
  state.prepGuideFor = jobId;
  state.prepGuide = null;
  state.prepGuideError = '';
  try {
    const response = await fetch('/api/jobs/' + encodeURIComponent(jobId), {cache:'no-store'});
    const result = await response.json();
    if (!response.ok || result.error) throw new Error(result.error || 'The role guide could not be loaded.');
    if (!result.preparation_guide) throw new Error('The role guide is unavailable. Restart the local app after updating it, then retry.');
    if (request !== state.prepGuideRequest || state.prepJob !== jobId) return;
    state.prepGuide = result.preparation_guide;
    if (result.job?.id === jobId) state.prepRole = result.job;
  } catch (error) {
    if (request !== state.prepGuideRequest || state.prepJob !== jobId) return;
    state.prepGuideError = error.message;
  }
  if (state.page === 'prep') render();
}
function prepRoleChoices() {
  const tracked = applications().map(a => jobById(a.job_id) || {id:a.job_id,...a}).filter(j => j.id);
  const trackedIds = new Set(tracked.map(j => j.id));
  const suggested = jobs().filter(j => active(j) && !trackedIds.has(j.id)).slice(0,30);
  const selected = jobById(state.prepJob) || (state.prepRole?.id === state.prepJob ? state.prepRole : null) || (state.detail?.job.id === state.prepJob ? state.detail.job : null) || (state.prepJob ? {id:state.prepJob,company:'Selected role',title:state.prepGuideError ? 'Role unavailable' : 'Loading role details'} : null);
  if (selected && !trackedIds.has(selected.id) && !suggested.some(j => j.id === selected.id)) suggested.unshift(selected);
  return {tracked, suggested, selected};
}
function sharedPrepTask(task) {
  return !task.job_id && (task.target_key === 'general' ||
    String(task.company || '').toLowerCase() === 'general preparation' || (!task.company && !task.target_key));
}
function prepTaskMatches(task, job) {
  if (!state.prepJob) return true;
  const company = String(job?.company || '').toLowerCase();
  return task.job_id === state.prepJob || sharedPrepTask(task) || Boolean(company &&
    (task.target_key === 'application:' + company ||
      (!task.job_id && String(task.company || '').toLowerCase() === company)));
}
function prepTaskRow(task) {
  const label = sharedPrepTask(task) ? 'Shared daily practice' :
    String(task.target_key || '').startsWith('application:') ? 'Company preparation' : '';
  return `<div class="prep-task">${label ? `<div class="prep-task-scope">${esc(label)}</div>` : ''}${taskRow(task)}${task.agent_guidance?`<details class="prep-agent-focus"><summary>Role focus: ${esc(task.agent_title||'Suggested practice')}</summary><p>${esc(task.agent_guidance)}</p><p class="muted">${esc(task.agent_reason||'')}</p>${roleEvidence(task.agent_sources)}</details>`:''}</div>`;
}
function roleEvidence(evidence) {
  if (!Array.isArray(evidence) || !evidence.length) return '';
  const labels = {title:'Role title', description:'Recorded description', matched_skills:'Recorded skill overlap'};
  return `<details class="prep-evidence"><summary>Role evidence behind this choice</summary><ul>${evidence.map(item => `<li><strong>${esc(labels[item.field] || 'Recorded role context')}:</strong> ${esc(item.text)}${safeURL(item.url) ? ` ${external(item.url,'Role source ↗')}` : ''}</li>`).join('')}</ul></details>`;
}
function renderLeetCode(guide) {
  const problems = guide.leetcode_problems || [];
  if (!problems.length && !guide.leetcode_note) return '';
  return `<section class="prep-leetcode"><div class="section-heading"><div><h3>LeetCode practice for this role</h3><p>Focused algorithm practice to use within your coding blocks</p></div>${external('https://leetcode.com/','Open LeetCode ↗','text-link')}</div><p class="prep-guide-caption">${esc(guide.leetcode_note || 'Chosen for role-relevant skills. These are not claims about questions asked by this employer.')}</p><div class="prep-problem-grid">${problems.map(problem => `<article class="prep-problem"><div class="prep-problem-heading"><h4>${external(problem.url,problem.title)}</h4><span class="chip">${esc(problem.difficulty)}</span></div><div class="skill-chips">${(problem.topics || []).map(topic => `<span class="chip">${esc(topic)}</span>`).join('')}</div><p class="prep-choice-reason"><strong>Why this problem</strong>${esc(problem.why_chosen)}</p>${roleEvidence(problem.role_evidence)}<p class="prep-problem-practice">${esc(problem.minutes)} min suggested · ${esc(problem.practice_note || 'Skill practice, not a reported company question.')}</p><p class="note-source">${esc(problem.access || '')}${problem.observed_at ? ` Verified ${esc(prettyDate(problem.observed_at,{year:'numeric'}))}.` : ''}</p></article>`).join('')}</div></section>`;
}
function renderAccountContext() {
  const services = state.data.services || [];
  if (!services.length) return '';
  const accessLabels = {signed_in:'Signed in when checked',sign_in_required:'Sign-in required when checked',public_only:'Public pages only when checked',unknown:'Access not established'};
  const links = sources => (sources || []).map(source => {
    const url = typeof source === 'string' ? source : source.url;
    return `<li>${external(url, typeof source === 'string' ? url : source.title || url)} ↗</li>`;
  }).join('');
  return `<details class="card prep-accounts"><summary><span><strong>Account context</strong><small>Browser access and saved context · No background sync</small></span><span class="chip">${services.length} services</span></summary><div class="card-body"><p class="prep-guide-caption">Open a service to use its browser sign-in. Jobflow shows the dated context saved below; opening a site does not establish an automatic connection.</p><div class="prep-account-grid">${services.map(service => `<article class="prep-account"><div class="prep-problem-heading"><h3>${esc(service.name)}</h3>${external(service.profile_url || service.url,`Open ${service.name} ↗`,'text-link')}</div><p>${esc(service.summary)}</p><div class="skill-chips"><span class="chip">Browser assisted</span><span class="chip">${esc(accessLabels[service.access_status] || 'Access not checked')}</span></div>${service.account_handle ? `<p class="prep-account-handle">Account observed: ${esc(service.account_handle)}</p>` : ''}<p class="note-source">Browser access last checked: ${service.last_observed_at ? esc(prettyDate(service.last_observed_at,{year:'numeric'})) : 'Not recorded'}</p><p class="prep-guide-caption">${esc(service.capability_note)}</p>${(service.context || []).length ? `<details class="prep-evidence"><summary>Saved context · ${service.context.length} records</summary>${service.context.map(note => `<div class="prep-account-note"><p>${esc(note.text)}</p><p class="note-source">Observed ${note.observed_at ? esc(prettyDate(note.observed_at,{year:'numeric'})) : 'date not recorded'}</p><ul class="source-list">${links(note.sources)}</ul></div>`).join('')}</details>` : '<p class="prep-guide-caption">No account context has been saved yet.</p>'}${(service.documentation_sources || []).length ? `<details class="prep-evidence"><summary>Supported access</summary><ul class="source-list">${links(service.documentation_sources)}</ul></details>` : ''}</article>`).join('')}</div></div></details>`;
}
function renderRoleGuide(choices) {
  if (!state.prepJob) {
    const candidates = choices.tracked.filter(j => !['rejected','withdrawn','offer'].includes(appById(j.id)?.stage)).concat(choices.suggested).slice(0,3);
    return `<section class="card prep-guide"><div class="card-head"><div><div class="eyebrow">START BEFORE THE INVITATION</div><h2>Choose a role to prepare for</h2><p>Every role has a company and skills guide, even before you save it or schedule an interview.</p></div></div><div class="card-body"><div class="prep-guide-choices">${candidates.map(j => button(`<strong>${esc(j.company)}</strong><span>${esc(j.title)}</span>`, `data-prep-job="${esc(j.id)}"`, 'prep-guide-choice')).join('')}</div><p class="prep-guide-caption">Choose a role above, or open any role in Explore roles and select Interview prep. Your shared daily schedule is below.</p></div></section>`;
  }
  loadPrepGuide(state.prepJob);
  const job = choices.selected || choices.tracked.find(j => j.id === state.prepJob);
  if (state.prepGuideError) return `<section class="card prep-guide"><div class="card-body" role="status"><h2>Could not load this role’s guide</h2><p class="prep-guide-caption">${esc(state.prepGuideError)}</p>${button('Retry guide','data-action="retry-prep-guide"','small')}</div></section>`;
  const guide = state.prepGuide;
  if (!guide) return `<section class="card prep-guide"><div class="card-body prep-guide-loading" role="status"><span class="spinner" aria-hidden="true"></span> Preparing the guide for ${esc(job?.company || 'this role')}…</div></section>`;
  const process = guide.process || {};
  return `<section class="card prep-guide"><div class="card-head"><div><div class="eyebrow">ROLE & COMPANY GUIDE</div><h2>${esc(guide.company)} · ${esc(guide.title)}</h2><p>${esc(guide.summary)}</p></div>${button('View role',`data-job="${esc(state.prepJob)}"`,'small')}</div><div class="card-body"><div class="skill-chips">${(guide.focus || []).map(topic => `<span class="chip green">${esc(topic)}</span>`).join('')}</div><div class="prep-guide-layout"><div><h3 class="prep-guide-heading">What to work on</h3><p class="prep-guide-caption">Use these suggestions within your daily practice blocks. They do not add time to your schedule.</p><div class="prep-guide-steps">${(guide.steps || []).map((step,index) => `<details class="prep-guide-step" ${index === 0 ? 'open' : ''}><summary><span class="prep-step-number">${index + 1}</span><span>${esc(step.title)}<small>${esc(String(step.category || 'Practice').replaceAll('_',' '))} · ${esc(step.minutes)} min suggested</small>${step.why_chosen ? `<span class="prep-step-rationale"><strong>Why this exercise</strong>${esc(step.why_chosen)}</span>` : ''}</span></summary><p>${esc(step.prompt)}</p>${roleEvidence(step.role_evidence)}</details>`).join('')}</div></div><aside class="prep-process"><h3 class="prep-guide-heading">Company interview guidance</h3><p>${esc(process.text || 'The company’s interview process has not been verified.')}</p>${process.observed_at ? `<p class="note-source">Guidance observed ${esc(prettyDate(process.observed_at,{year:'numeric'}))}. Confirm the current format when an interview is arranged.</p>` : ''}<ul class="source-list">${(process.sources || []).map(source => `<li>${external(source.url, source.title || source.url)} ↗</li>`).join('')}</ul><p class="prep-guide-caption">${esc(guide.disclaimer || 'Suggested exercises are original practice, not reported employer interview questions.')}</p></aside></div>${renderLeetCode(guide)}</div></section>`;
}
async function previewAgentPlan() {
  if (state.busy) return;
  state.busy = true;
  state.agentLoading = true;
  state.agentProposal = null;
  state.agentError = '';
  const requestedJob = state.prepJob;
  render();
  try {
    const response = await fetch('/api/action', {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':state.data.csrf_token}, body:JSON.stringify({action:'agent',operation:'preview',job_id:requestedJob})});
    const result = await response.json();
    if (!response.ok || result.error || !result.proposal) throw new Error(result.error || 'Your plan could not be prepared.');
    if (state.prepJob === requestedJob) state.agentProposal = result.proposal;
  } catch (error) { state.agentError=error.message; toast(error.message, true); }
  finally { state.busy=false; state.agentLoading=false; render(); }
}
async function applyAgentPlan() {
  const proposal = state.agentProposal;
  if (!proposal || state.busy) return;
  const ok = await mutate({action:'agent',operation:'apply',job_id:proposal.job_id,fingerprint:proposal.fingerprint}, 'Reviewed preparation plan saved.');
  if (!ok) {state.agentProposal=null;state.agentError='The plan was not confirmed as saved. Generate a fresh preview to review the current workspace before trying again.';render();}
}
function renderAgentPlanner() {
  if (state.agentLoading) return '<section class="card prep-agent-card" role="status"><h2>Planning your next steps…</h2><p>Checking your recorded role context, upcoming interviews, existing practice, and available time.</p></section>';
  const proposal = state.agentProposal;
  if (!proposal) return `${state.agentError?`<p class="notice" role="alert">${esc(state.agentError)}</p>`:''}<section class="card prep-agent-card"><div><span class="chip">Preparation agent</span><h2>A plan that starts with your next move</h2><p>Review role-specific guidance and a proposed schedule based on your saved context. Apply the plan when it looks right, and edit practice tasks as you go.</p><p class="note-source">Local evidence-based planning · shared daily budget · changes reviewed before saving</p></div>${button('Plan my next steps','data-action="agent-preview"','primary')}</section>`;
  const steps = proposal.next_steps || [], changes = proposal.changes || [];
  return `<section class="card prep-agent-card prep-agent-review" aria-label="Review your preparation plan"><div class="prep-agent-heading"><div><span class="chip">Review proposed plan</span><h2>${esc(proposal.company ? proposal.company + ' · ' + proposal.title : 'Your next steps')}</h2><p>${esc(proposal.summary)}</p></div>${button('Dismiss','data-action="agent-dismiss"')}</div><ol class="prep-agent-steps">${steps.map(step=>`<li><h3>${esc(step.title)}</h3><p>${esc(step.detail)}</p>${step.prompt?`<details><summary>Practice prompt</summary><p>${esc(step.prompt)}</p>${roleEvidence(step.sources)}</details>`:''}${step.kind==='resume'&&step.job_id?button('Review saved resume',`data-resume-role="${esc(step.job_id)}"`,'small'):step.kind==='application'&&step.job_id?button('Review application',`data-track="${esc(step.job_id)}"`,'small'):''}</li>`).join('')}</ol><div class="prep-agent-summary"><strong>${fmt(proposal.new_count)} new blocks · ${fmt(proposal.rescheduled_count)} rescheduled · ${fmt(proposal.focused_count)} role-focused</strong><span>${fmt(proposal.completed_preserved)} completed tasks preserved · ${esc(proposal.budget)} minutes per day</span></div><details class="prep-agent-schedule" open><summary>Review schedule changes · ${changes.length}</summary>${changes.length?`<ul>${changes.map(change=>`<li><div><strong>${esc(change.title)}</strong><p>${esc(change.kind)} · ${change.date?esc(prettyDate(change.date,{weekday:'short'})):'Backlog'}${change.previous_date&&change.previous_date!==change.date?' (from '+esc(prettyDate(change.previous_date))+')':''} · ${esc(change.minutes)} min${change.focus?' · Focus: '+esc(change.focus):''}</p>${change.guidance?`<details><summary>Role practice guidance</summary><p>${esc(change.guidance)}</p></details>`:''}</div>${(state.data.tasks||[]).some(t=>t.id===change.id)?button('Edit task',`data-task="${esc(change.id)}"`,'small'):''}</li>`).join('')}</ul>`:'<p>Your schedule already fits the current context.</p>'}</details><details><summary>Daily capacity · ${esc(proposal.timezone)}</summary><div class="prep-agent-days">${(proposal.days||[]).map(day=>`<span>${esc(prettyDate(day.date,{weekday:'short'}))}<strong>${esc(day.minutes)} / ${esc(proposal.budget)} min</strong></span>`).join('')}</div></details>${(proposal.notes||[]).length?`<details><summary>Scheduling explanations</summary><ul>${proposal.notes.map(note=>`<li>${esc(note)}</li>`).join('')}</ul></details>`:''}<p class="note-source">${esc(proposal.evidence_note)}</p><div class="button-group">${button('Generate again','data-action="agent-preview"')}${button('Apply reviewed plan','data-action="agent-apply"','primary')}</div></section>`;
}
function renderPrep() {
  const choices = prepRoleChoices(), selected = choices.selected || choices.tracked.find(j => j.id === state.prepJob);
  const allTasks = state.data.tasks || [], tasks = allTasks.filter(t => prepTaskMatches(t, selected));
  const scheduled = tasks.filter(t => t.due_date && !['unscheduled','skipped'].includes(t.status));
  const backlog = tasks.filter(t => t.status === 'unscheduled' || (!t.due_date && !['done','skipped'].includes(t.status)));
  const dates = [...new Set(scheduled.map(t => t.due_date))].sort();
  const currentDates = dates.filter(d => d >= today()), pastDates = dates.filter(d => d < today());
  const budget = state.data.settings?.preparation?.daily_minutes || 0;
  const company = String(selected?.company || '').toLowerCase();
  const notes = state.data.plan?.research_notes || state.data.plan?.notes || [];
  const sharedResource = r => !r.job_id && !r.company &&
    ['learning_resource','generic_practice','general_practice'].includes(String(r.kind || '').toLowerCase());
  const relevantNote = r => !state.prepJob || r.job_id === state.prepJob || sharedResource(r) ||
    (!r.job_id && Boolean(company) && String(r.company || '').toLowerCase() === company);
  const researchKind = r => sharedResource(r) || /interview|question|prepar|learning|process|live_research|research_status/i.test(r.kind || r.role || '');
  const planEvidence = r => researchKind(r) || (Array.isArray(r.sources) && r.sources.length > 0);
  const research = [...(state.data.research || []).filter(r => relevantNote(r) && (researchKind(r) || Boolean(state.prepJob && r.job_id === state.prepJob))),
    ...notes.filter(r => r && typeof r === 'object' && relevantNote(r) && planEvidence(r))];
  const seenResearch = new Set();
  const resources = research.filter(r => {const key = r.id || [r.kind,r.company,r.text].join('|');if(seenResearch.has(key))return false;seenResearch.add(key);return true;});
  const schedulingNotes = notes.filter(r => typeof r === 'string' ||
    (r && !planEvidence(r) && (relevantNote(r) || (!r.job_id && !r.company))));
  const allInterviews = (state.data.interviews || []).filter(i => !state.prepJob || i.job_id === state.prepJob).sort((a,b) => String(a.starts_at).localeCompare(String(b.starts_at)));
  const upcoming = i => !['cancelled','canceled','completed'].includes(String(i.status || '').toLowerCase()) &&
    !['rejected','withdrawn','offer','accepted'].includes(appById(i.job_id)?.stage) && Date.parse(i.starts_at) >= Date.now();
  const interviews = allInterviews.filter(upcoming), pastInterviews = allInterviews.filter(i => !upcoming(i));
  const interviewRow = i => `<div class="interview-row"><div class="interview-date">${prettyDate(i.starts_at,{weekday:'short',hour:'numeric',minute:'2-digit',timeZoneName:'short'})}</div><h3>${esc(i.round)}</h3><p>${esc(i.company || jobById(i.job_id)?.company || '')} · ${esc(jobById(i.job_id)?.title || '')}${i.status ? ' · ' + esc(i.status) : ''}</p>${i.notes ? `<p>${esc(i.notes)}</p>` : ''}</div>`;
  const dayGroup = day => {
    const total = allTasks.filter(t => t.due_date === day && !['skipped','unscheduled'].includes(t.status)).reduce((sum,t) => sum + Number(t.minutes || 0),0);
    return `<div class="prep-day-heading"><h3 class="task-group-title">${day === today() ? 'Today · ' : ''}${prettyDate(day,{weekday:'long'})}</h3><span>${total} / ${budget} min across all roles</span></div>${scheduled.filter(t => t.due_date === day).map(prepTaskRow).join('')}`;
  };
  const option = j => `<option value="${esc(j.id)}" ${state.prepJob === j.id ? 'selected' : ''}>${esc(j.company)} · ${esc(j.title)}</option>`;
  return pageHeader('PREPARE WITH PURPOSE','Interview preparation','Role guides, daily practice, and confirmed interviews.',button('Plan my next steps','data-action="agent-preview"','primary') + button('Review schedule','data-action="rebuild-prep"') + button(icon('plus') + ' Add interview','data-action="interview"','primary')) +
    `<div class="filters"><div class="field grow"><label for="prep-job">Role and company focus</label><select id="prep-job"><option value="">All roles & shared practice</option>${choices.tracked.length ? `<optgroup label="Saved roles & applications">${choices.tracked.map(option).join('')}</optgroup>` : ''}${choices.suggested.length ? `<optgroup label="Current matches">${choices.suggested.map(option).join('')}</optgroup>` : ''}</select></div><div class="date-pill">${icon('clock','nav-icon')} ${budget} minutes per day</div><span class="chip">${tasks.filter(t => t.status === 'done').length} tasks completed</span></div>${renderAgentPlanner()}${renderRoleGuide(choices)}${renderAccountContext()}
    <div class="prep-layout"><section class="card"><div class="card-head"><div><h2>Your preparation schedule</h2><p>${state.prepJob ? 'Role and company tasks, plus shared daily practice' : 'Daily practice across your roles and companies'}</p></div></div><div class="card-body"><p class="prep-schedule-note">One ${budget}-minute daily budget across all roles. Shared practice appears in each role’s view and is counted once. Confirmed interviews shape the schedule when recorded.</p>${currentDates.length ? currentDates.map(dayGroup).join('') : empty('Keep preparing before an interview is scheduled','Rebuild the schedule to plan daily practice within your budget. The role guide is available independently.','✧')}${pastDates.length ? `<details class="details-toggle"><summary>Past schedule & completion history · ${pastDates.length} days</summary>${pastDates.map(dayGroup).join('')}</details>` : ''}${backlog.length ? `<details class="details-toggle"><summary>Unscheduled backlog · ${backlog.length} tasks</summary>${backlog.map(prepTaskRow).join('')}</details>` : ''}${schedulingNotes.length ? `<details class="details-toggle"><summary>Scheduling notes</summary><ul class="prep-scheduling-notes">${schedulingNotes.map(n => `<li>${esc(typeof n === 'string' ? n : n.text || '')}</li>`).join('')}</ul></details>` : ''}</div></section>
    <div><section class="card"><div class="card-head"><div><h2>Upcoming interviews</h2><p>Confirmed dates from your interview records</p></div></div><div class="card-body">${interviews.length ? interviews.map(interviewRow).join('') : empty('No interview scheduled yet','Start with the role guide and daily practice. Add a date when an interview is confirmed.','◷')}${pastInterviews.length ? `<details class="details-toggle"><summary>Past & inactive interviews · ${pastInterviews.length}</summary>${pastInterviews.map(interviewRow).join('')}</details>` : ''}</div></section><section class="card" style="margin-top:22px"><div class="card-head"><div><h2>Research & learning materials</h2><p>Recorded evidence, with sources and observation dates</p></div></div><div class="card-body resource-scroll">${resources.length ? resources.map(resourceCard).join('') : empty('Build on the preparation guide','Company research and learning resources appear here when recorded. Suggested practice does not establish actual interview questions.','⌘')}</div></section></div></div>`;
}

function renderSources(){const sources=state.data.source_health||[],problems=sources.filter(s=>!['ok','success','healthy','manual'].includes(s.status)),runs=state.data.runs||[];return pageHeader('A CLEAR VIEW OF THE INPUTS','Source health','Review feed coverage and recent runs. A failed scan does not mean a role has closed.')+(problems.length?`<div class="banner amber"><div><strong>${problems.length} sources need attention</strong>These issues can affect coverage. Existing results remain available for review.</div></div>`:'')+`<section class="card"><div class="card-head"><div><h2>Latest search activity</h2><p>Recorded workflow runs</p></div></div><div class="card-body">${runs.slice(0,5).map(r=>`<div class="health-row"><strong>${prettyDate(r.started_at,{hour:'numeric',minute:'2-digit'})}</strong><span class="chip ${r.status==='completed'?'green':'amber'}">${esc(r.status)}</span><p>${r.finished_at?'Finished '+prettyDate(r.finished_at,{hour:'numeric',minute:'2-digit'}):'Finish time not recorded'}</p></div>`).join('')||empty('No runs recorded','Run the existing search workflow to collect fresh results.')}</div></section><section class="card" style="margin-top:22px"><div class="card-head"><div><h2>Source coverage</h2><p>${sources.length} recorded sources · manual sources need public research or imports</p></div></div><div class="card-body">${[...sources].sort((a,b)=>(['ok','success','healthy','manual'].includes(a.status)?1:0)-(['ok','success','healthy','manual'].includes(b.status)?1:0)||a.id.localeCompare(b.id)).map(s=>`<div class="health-row"><div><strong>${esc(s.company||s.id)}</strong><p>${s.checked_at?prettyDate(s.checked_at,{hour:'numeric',minute:'2-digit'}):'No check date'}</p></div><div><span class="chip ${['ok','success','healthy'].includes(s.status)?'green':s.status==='manual'?'':'amber'}">${esc(s.status)}</span></div><p>${esc(s.message||s.error||'')}${s.url?'<br>'+external(s.url,'Source page ↗'):''}</p></div>`).join('')}</div></section>`;}

async function openJob(id) {const dialog=$('#detail-dialog');state.detail=null;state.detailTab='overview';$('#detail-content').innerHTML='<h2 id="detail-title" class="sr-only">Role details</h2><div class="loading"><span class="spinner"></span>Opening role…</div>';if(!dialog.open)dialog.showModal();try{const response=await fetch('/api/jobs/'+encodeURIComponent(id),{cache:'no-store'});if(!response.ok)throw new Error('This role could not be loaded.');state.detail=await response.json();renderDetail();}catch(error){$('#detail-content').innerHTML=`<div class="action-content"><button class="icon-button dialog-close" data-action="close-detail" aria-label="Close">×</button><h2 id="detail-title" class="sr-only">Role details unavailable</h2>${empty('Could not open role',error.message)}</div>`;}}
function renderDetail(){const d=state.detail;if(!d)return;const j=d.job,a=d.application||appById(j.id),packet=d.packet;const tab=state.detailTab;$('#detail-content').innerHTML=`<header class="dialog-header"><button class="icon-button dialog-close" data-action="close-detail" aria-label="Close role details">×</button><div class="company-cell">${companyLogo(j.company)}<strong>${esc(j.company)}</strong>${scoreBadge(j.score)}${availabilityChip(j)}</div><h2 id="detail-title">${esc(j.title)}</h2><div class="button-group">${(!a||a.stage==='saved')?button('Apply with Codex',`data-batch-role="${esc(j.id)}"`,'primary'):''}${external(j.url,'Open employer application ↗','button')}${button(a?'Update application':'Save role',a?`data-track="${esc(j.id)}"`:`data-save="${esc(j.id)}"`)}${button('Tailor resume',`data-resume-role="${esc(j.id)}"`)}${button('Interview prep',`data-prep-job="${esc(j.id)}"`,'ghost')}</div>${j.availability==='missing'?'<div class="inline-alert">This listing was missing from a scan. Recheck the employer page before applying; it is not confirmed closed.</div>':j.availability==='closed'?'<div class="inline-alert">This listing is recorded as closed. Verify the employer’s current page for any change.</div>':''}</header><div class="dialog-body detail-layout"><aside class="detail-facts" aria-label="Role facts">
  <div class="meta-item"><span>Location</span><strong>${esc(j.location||'Location unspecified')}</strong></div>
  ${j.department?`<div class="meta-item"><span>Department</span><strong>${esc(j.department)}</strong></div>`:''}
  <div class="meta-item"><span>Advertised base pay</span><strong>${esc(compensation(j,'base'))}</strong></div>
  <div class="meta-item"><span>Total compensation</span><strong>${esc(compensation(j,'tc'))} · ${esc(j.compensation?.tc_status||'unknown')}</strong></div>
  <div class="meta-item"><span>First discovered</span><strong>${prettyDate(j.first_seen,{year:'numeric'})}</strong></div>
  <div class="meta-item"><span>Last seen in source</span><strong>${prettyDate(j.last_seen,{year:'numeric'})}</strong></div>
</aside><div class="detail-main"><div class="detail-tabs" role="tablist" aria-label="Role information">${[['overview','Overview'],['packet','Application packet'],['research','Research & contacts']].map(([v,l])=>`<button role="tab" aria-selected="${tab===v}" class="${tab===v?'active':''}" data-detail-tab="${v}">${l}</button>`).join('')}</div>${tab==='overview'?`${j.salary_reference?`<section class="detail-section"><h3>Compensation reference</h3><p>${external(j.salary_reference.url,j.salary_reference.label||'Levels.fyi salary reference ↗')}</p><p>${esc(j.salary_reference.note||'Reported compensation benchmark, separate from employer-advertised pay.')}</p></section>`:''}<section class="detail-section"><h3>Your profile overlap</h3><div class="skill-chips">${(j.matched_skills||[]).map(s=>`<span class="chip green">${esc(s)}</span>`).join('')||'<span class="muted tiny">No explicit skill overlap recorded.</span>'}</div>${(j.reasons||[]).length?`<ul>${j.reasons.map(s=>`<li>${esc(s)}</li>`).join('')}</ul>`:''}${(j.gaps||[]).length?`<details><summary>Things to clarify</summary><ul>${j.gaps.map(s=>`<li>${esc(s)}</li>`).join('')}</ul></details>`:''}</section>${a?`<section class="detail-section"><h3>Your application</h3><p><span class="chip green">${esc(stageLabels[a.stage]||a.stage)}</span> ${a.applied_at?'Applied '+prettyDate(a.applied_at,{year:'numeric'}):a.stage==='saved'?'Not recorded as submitted':'Submission date unknown'}</p>${a.referral_status?`<p>Referral: ${esc(a.referral_status)}${a.referred_date?' · '+prettyDate(a.referred_date):''}</p>`:''}${a.notes?`<p class="pre-wrap" style="margin-top:10px">${esc(a.notes)}</p>`:''}${a.next_action?`<p>Next action: ${esc(a.next_action)}</p>`:''}${a.follow_up_date?`<p>Follow-up: ${prettyDate(a.follow_up_date)}</p>`:''}${a.response?`<p>Response: ${esc(a.response)}</p>`:''}<details><summary>All recorded application metadata</summary><pre class="detail-description">${esc(JSON.stringify(a,null,2))}</pre></details></section>`:''}<section class="detail-section"><h3>About the role</h3><div class="detail-description">${esc(j.description||'No description was recorded. Open the employer page for details.')}</div></section><details class="details-toggle"><summary>Full recorded role data</summary><pre class="detail-description">${esc(JSON.stringify(j,null,2))}</pre></details>`:tab==='packet'?`<section class="detail-section"><div class="button-group" style="margin-bottom:18px">${button('Tailor saved resume',`data-resume-role="${esc(j.id)}"`,'primary')}${button(packet?'Regenerate review packet':'Generate review packet',`data-personalize="${esc(j.id)}"`)}</div>${packet?`${packet.needs_profile_review?'<p class="notice">Your profile or preferences changed after this draft was generated. Regenerate it before using it.</p>':''}<p>${esc(packet.summary||'')}</p><p class="note-source">Generated ${prettyDate(packet.generated_at,{year:'numeric'})}. Drafts are for your review.</p>${packet.outreach?.email?`<details open><summary>Outreach email draft</summary><p><strong>${esc(packet.outreach.subject||'')}</strong></p><p>${esc(packet.outreach.email)}</p></details>`:''}${packet.outreach?.intro?`<details><summary>Introduction draft</summary><p>${esc(packet.outreach.intro)}</p></details>`:''}${packet.resume_markdown?`<details><summary>Resume review draft</summary><p>${esc(packet.resume_markdown)}</p></details>`:''}${(packet.resume_suggestions||[]).length?`<details><summary>Evidence-backed resume suggestions · ${packet.resume_suggestions.length}</summary>${packet.resume_suggestions.map(s=>`<p><strong>${esc(s.proposed||s.original||'')}</strong><br>${esc(s.reason||'')}${s.source_url?'<br>'+external(s.source_url,'Original evidence ↗'):''}</p>`).join('')}</details>`:''}${(packet.cautions||[]).length?`<details><summary>Review cautions</summary><p>${packet.cautions.map(esc).join('\n')}</p></details>`:''}${packet.markdown?`<details><summary>Full review packet</summary><p>${esc(packet.markdown)}</p></details>`:''}`:empty('Make this application your own','Generate a packet from your recorded experience, source research, and contacts. The draft keeps your original evidence intact.','✧')}</section>`:`<section class="detail-section"><h3>Sourced contacts</h3>${(d.contacts||[]).map(contactCard).join('')||'<p>No sourced contacts for this role or company.</p>'}</section><section class="detail-section"><h3>Research notes</h3>${(d.research||[]).map(resourceCard).join('')||'<p>No research has been associated with this role yet.</p>'}</section>`}</div></div>`;}
function showAction(html){$('#action-content').innerHTML=`<div class="action-content"><button class="icon-button dialog-close" data-action="close-action" aria-label="Close dialog">×</button>${html}<div id="form-error" role="alert" class="error-text"></div></div>`;$('#action-dialog').showModal();}
function trackingForm(id){const j=jobById(id)||state.detail?.job,a=appById(id)||{};if(!j)return;showAction(`<h2>Update application</h2><p>${esc(j.company)} · ${esc(j.title)}</p><form id="tracking-form" data-id="${esc(id)}"><div class="form-row"><div class="field"><label for="track-stage">Application stage</label><select id="track-stage" name="stage">${stages.map(s=>`<option value="${s}" ${(a.stage||'saved')===s?'selected':''} ${s==='saved'&&a.stage&&a.stage!=='saved'?'disabled':''}>${stageLabels[s]}</option>`).join('')}</select></div><div class="field"><label for="track-followup">Follow-up date</label><input id="track-followup" name="follow_up_date" type="date" value="${esc(a.follow_up_date||'')}"></div></div><div class="field"><label for="track-next">Next action</label><input class="input" id="track-next" name="next_action" value="${esc(a.next_action||'')}" placeholder="e.g. Check in with the recruiter"></div><div class="field"><label for="track-response">Latest response</label><textarea id="track-response" name="response" placeholder="Record a response you received">${esc(a.response||'')}</textarea></div><div class="field"><label for="track-notes">Application notes</label><textarea id="track-notes" name="notes">${esc(a.notes||'')}</textarea></div><p class="tiny muted">Choose “Applied” after you have submitted the employer’s application. This records your progress; it does not submit a form or send a message.</p><div class="button-group">${button('Cancel','type="button" data-action="close-action"')}${button('Save progress','type="submit"','primary')}</div></form>`);}
function queueDialog(){const selected=[...state.selected].map(jobById).filter(Boolean);if(!selected.length){showAction(`<h2>Application queue</h2><p>Choose roles from Explore roles or the Daily inbox. Select all results after filtering by company and match score to build a focused batch.</p>${button('View saved application batches','data-action="batch-open"','primary')}${button('Explore roles →','data-action="queue-explore"')}`);return;}showAction(`<h2>Application queue</h2><p>${selected.length} selected roles. Create a batch, review your materials, and open the prepared application request in Codex.</p><div class="button-group" style="margin-bottom:18px">${button('Create application batch','data-action="batch-new"','primary')}${button('View saved batches','data-action="batch-open"')}</div><div class="queue-list">${selected.map(j=>`<div class="queue-item">${companyLogo(j.company)}<span><strong>${esc(j.company)}</strong><small>${esc(j.title)}</small>${availabilityChip(j)}${appById(j.id)?`<small>Stage: ${esc(stageLabels[appById(j.id).stage])}</small>`:''}</span>${external(j.url,'Apply ↗','button small')}</div>`).join('')}</div><div class="button-group" style="justify-content:flex-start;margin-bottom:18px">${button('Save to applications','data-action="queue-save"')}${button(selected.length>10?'Prepare next 10 review packets':'Prepare review packets','data-action="queue-packets"')}</div><details><summary>Record applications submitted elsewhere</summary><form id="batch-form"><div class="field"><label><input type="checkbox" name="confirmed" required> I submitted all selected applications and want to record them as applied.</label></div><div class="button-group">${button('Close','type="button" data-action="close-action"')}${button('Record completed submissions','type="submit"','primary')}</div></form></details>`);}
function taskForm(id){const t=(state.data.tasks||[]).find(t=>t.id===id);if(!t)return;showAction(`<h2>Practice progress</h2><p>${esc(t.title)}</p><form id="task-form" data-id="${esc(id)}"><div class="form-row"><div class="field"><label for="task-status">Progress</label><select id="task-status" name="status">${[['todo','Planned'],['doing','In progress'],['done','Completed'],['skipped','Skipped'],['unscheduled','Backlog']].map(([s,l])=>`<option value="${s}" ${t.status===s?'selected':''}>${l}</option>`).join('')}</select></div><div class="field"><label for="task-actual">Actual minutes spent</label><input class="input" id="task-actual" name="actual_minutes" type="number" min="0" max="1440" step="1" value="${esc(t.actual_minutes??'')}" placeholder="Leave blank if unknown"></div></div><div class="field"><label for="task-date">Task date</label><input id="task-date" name="due_date" type="date" value="${esc(t.due_date||today())}" required></div><div class="field"><label for="task-notes">Practice notes</label><textarea id="task-notes" name="notes">${esc(t.notes||'')}</textarea></div><p class="tiny muted">${t.minutes||0} minutes planned. Actual time is recorded separately from estimates.</p><div class="button-group">${button('Cancel','type="button" data-action="close-action"')}${button('Save progress','type="submit"','primary')}</div></form>`);}
function interviewForm(){const opts=applications().map(a=>jobById(a.job_id)||{id:a.job_id,...a});if(!opts.length){showAction('<h2>Add your first application.</h2><p>Save or record an application before adding its confirmed interview.</p>'+button('Explore roles','data-action="queue-explore"','primary'));return;}showAction(`<h2>Add interview</h2><p>Record a confirmed interview to shape your preparation timeline.</p><form id="interview-form"><div class="field"><label for="interview-job">Role</label><select id="interview-job" name="job_id">${opts.map(j=>`<option value="${esc(j.id)}" ${state.prepJob===j.id?'selected':''}>${esc(j.company)} · ${esc(j.title)}</option>`).join('')}</select></div><div class="form-row"><div class="field"><label for="interview-round">Round</label><input class="input" id="interview-round" name="round" placeholder="e.g. Technical screen" required></div><div class="field"><label for="interview-at">Date & time</label><input class="input" id="interview-at" name="starts_at" type="datetime-local" required></div></div><p class="tiny muted">Enter the time in your browser timezone: ${esc(Intl.DateTimeFormat().resolvedOptions().timeZone)}.</p><div class="field"><label for="interview-notes">Notes</label><textarea id="interview-notes" name="notes" placeholder="Format, interviewer details, or topics shared by the recruiter"></textarea></div><div class="button-group">${button('Cancel','type="button" data-action="close-action"')}${button('Add interview','type="submit"','primary')}</div></form>`);}
async function saveSelected(){const ids=[...state.selected];if(!ids.length)return;await mutate({action:'application',job_ids:ids,stage:'saved'},'Roles saved. Existing application stages were preserved.');}
async function personalize(ids){if(!ids.length)return;toast(`Preparing ${ids.length===1?'a review packet':ids.length+' review packets'}…`);const ok=await mutate({action:'personalize',job_ids:ids},'Review packets are ready.');if(ok&&state.detail&&ids.includes(state.detail.job.id)){const response=await fetch('/api/jobs/'+encodeURIComponent(state.detail.job.id));if(response.ok){state.detail=await response.json();state.detailTab='packet';renderDetail();}}}

// Event delegation keeps large role lists inexpensive and avoids executable record content.
document.addEventListener('click',async event=>{const el=event.target.closest('[data-action],[data-job],[data-track],[data-save],[data-task],[data-personalize],[data-prep-job],[data-company],[data-city],[data-city-browse],[data-company-city],[data-detail-tab],[data-batch-role],[data-map-more],[data-map-office-focus],[data-map-coverage],[data-map-clear],[data-map-company-view],[data-resume-role],[data-roles-view],[data-role-company]');if(!el)return;
  if(el.dataset.rolesView){switchRolesView(el.dataset.rolesView);return;}
  if(el.dataset.roleCompany!==undefined){chooseRoleCompany(el.dataset.roleCompany);return;}
  if(el.dataset.mapCompanyView){window.JobflowMaps?.focusCompany(el.dataset.mapCompanyView);return;}
  if(el.dataset.mapCoverage !== undefined || el.dataset.mapClear){
    state.mapCoverageStatus = el.dataset.mapCoverage || ''; state.mapSelectedCompany=''; state.mapOfficeId=''; state.city='';
    window.JobflowMaps?.clearSelection();
    const detail=$('#map-selection');if(detail)detail.innerHTML=renderCityDetail(mapJobs());return;
  }
  if(el.dataset.resumeRole){startResumeDraft(el.dataset.resumeRole);return;}
  if(el.dataset.mapOfficeFocus){window.JobflowMaps?.focusOffice(el.dataset.mapOfficeFocus);return;}
  if(el.dataset.batchRole){startApplicationBatch([el.dataset.batchRole]);return;}
  if(el.dataset.job){openJob(el.dataset.job);return;}
  if(el.dataset.track){trackingForm(el.dataset.track);return;}
  if(el.dataset.save){if(await mutate({action:'application',job_ids:[el.dataset.save],stage:'saved'},'Role saved to your applications.')){if(state.detail){state.detail.application=appById(el.dataset.save);renderDetail();}}return;}
  if(el.dataset.task){taskForm(el.dataset.task);return;}
  if(el.dataset.personalize){await personalize([el.dataset.personalize]);return;}
  if(el.dataset.prepJob){resetPrepGuide();state.prepJob=el.dataset.prepJob;$('#detail-dialog').close();navigate('prep');return;}
  if(el.dataset.detailTab){state.detailTab=el.dataset.detailTab;renderDetail();return;}
  if(el.dataset.company){navigate('jobs',{company:el.dataset.company,q:'',location:'',availability:'active'});return;}
  if(el.dataset.companyCity){navigate('jobs',{company:el.dataset.companyCity,location:el.dataset.cityName,score:state.mapScore||'',q:'',availability:'active'});return;}
  if(el.dataset.mapMore){
    state.mapRoleLimits ||= new Map(); state.mapGroupOpen ||= new Map();
    const key=mapGroupKey(el.dataset.mapMore), previous=state.mapRoleLimits.get(key)||6;
    state.mapRoleLimits.set(key,previous+25); state.mapGroupOpen.set(key,true);
    $('#map-company-results').innerHTML=renderMapCompanyResults(mapJobs());
    const group=[...document.querySelectorAll('.map-company-group')].find(node=>node.dataset.mapGroup===el.dataset.mapMore);
    group?.querySelectorAll('.map-position .role-title')[previous]?.focus();
    return;
  }
  if(el.dataset.cityBrowse){navigate('jobs',{location:el.dataset.cityBrowse,company:state.mapCompany||'',score:state.mapScore||'',q:'',availability:'active'});return;}
  if(el.dataset.city){state.city=el.dataset.city;navigate('analytics');return;}
  switch(el.dataset.action){
    case 'refresh':await refresh(true);break;
    case 'reload-app':reloadApp();break;
    case 'explore':navigate('jobs');break;
    case 'strong':navigate('jobs',{score:'80',company:'',location:'',q:'',availability:'active'});break;
    case 'view-map':state.focusMap=true;navigate('analytics');break;
    case 'view-apps':navigate('applications');break;
    case 'previous':state.pageIndex=Math.max(0,state.pageIndex-1);render();break;
    case 'next':state.pageIndex++;render();break;
    case 'clear-filters':state.filters={q:'',company:'',score:'',availability:'active',location:'',sort:'score'};state.pageIndex=0;render();break;
    case 'companies-back':{const company=state.roleCompany;state.roleCompany='';state.pageIndex=0;render();const tile=[...document.querySelectorAll('[data-role-company]')].find(el=>el.dataset.roleCompany===company);tile?.focus();break;}
    case 'select-results':rolesInView().forEach(j=>state.selected.add(j.id));render();toast(`${fmt(state.selected.size)} roles selected.`);break;
    case 'clear-selection':state.selected.clear();render();break;
    case 'save-selected':await saveSelected();break;
    case 'queue':queueDialog();break;
    case 'batch-new':startApplicationBatch();break;
    case 'batch-open':$('#action-dialog').close();navigate('batches');break;
    case 'queue-explore':$('#action-dialog').close();navigate('jobs');break;
    case 'queue-save':await saveSelected();break;
    case 'queue-packets':{const prepared=new Set((state.data.packets||[]).map(p=>p.job_id));const pending=[...state.selected].filter(id=>!prepared.has(id));await personalize((pending.length?pending:[...state.selected]).slice(0,10));break;}
    case 'close-detail':$('#detail-dialog').close();break;
    case 'close-action':$('#action-dialog').close();break;
    case 'interview':interviewForm();break;
    case 'retry-prep-guide':loadPrepGuide(state.prepJob,true);render();break;
    case 'rebuild-prep':case 'agent-preview':await previewAgentPlan();break;
    case 'agent-apply':await applyAgentPlan();break;
    case 'agent-dismiss':state.agentProposal=null;render();break;
  }
});
document.addEventListener('change',event=>{const el=event.target;if(el.id==='company-sort'){if(!['roles','score','average','strong'].includes(el.value))return;state.companySort=el.value;render();$('#company-sort')?.focus();}else if(el.matches('[data-filter]')){clearTimeout(state.searchTimer);state.filters[el.dataset.filter]=el.value;state.pageIndex=0;render();}else if(el.matches('[data-select]')){el.checked?state.selected.add(el.dataset.select):state.selected.delete(el.dataset.select);const previous=$('.selection-bar');if(previous)previous.remove();$('#main').insertAdjacentHTML('beforeend',selectionBar());const all=$('#select-page');if(all)all.checked=state.visibleJobs.every(j=>state.selected.has(j.id));}else if(el.id==='select-page'){state.visibleJobs.forEach(j=>el.checked?state.selected.add(j.id):state.selected.delete(j.id));render();}else if(el.id==='inbox-date'){state.inboxDate=el.value||today();state.pageIndex=0;render();}else if(el.id==='prep-job'){state.agentProposal=null;resetPrepGuide();state.prepJob=el.value;navigate('prep');}else if(el.id==='contact-company'){state.contactCompany=el.value;render();}else if(el.id==='contact-role'){state.contactJob=el.value;render();}else if(el.id==='map-company-filter'){state.mapCompany=el.value;state.mapSelectedCompany='';state.mapOfficeId='';state.city='';render();}else if(el.id==='map-score-filter'){state.mapScore=el.value;state.mapSelectedCompany='';state.mapOfficeId='';state.city='';render();}});
document.addEventListener('input',event=>{const el=event.target;if(el.id==='map-role-query'){state.mapRoleQuery=el.value;const results=$('#map-company-results');if(results){results.innerHTML=renderMapCompanyResults(mapJobs());results.scrollTop=0;}return;}if(!el.matches('input[data-filter]'))return;clearTimeout(state.searchTimer);const key=el.dataset.filter,value=el.value;state.filters[key]=value;state.pageIndex=0;state.searchTimer=setTimeout(()=>{const stillFocused=document.activeElement===el;const start=el.selectionStart;render();if(stillFocused){const input=document.querySelector(`input[data-filter="${key}"]`);input?.focus();input?.setSelectionRange(start,start);}},220);});
document.addEventListener('toggle',event=>{if(event.target.matches?.('.map-company-group')){state.mapGroupOpen ||= new Map();state.mapGroupOpen.set(mapGroupKey(event.target.dataset.mapGroup),event.target.open);}},true);
document.addEventListener('error',event=>{const img=event.target;if(img.matches?.('.company-logo img')){img.hidden=true;img.parentElement.classList.remove('has-image');}},true);
document.addEventListener('keydown',event=>{const marker=event.target.closest('.map-marker');if(marker&&['Enter',' '].includes(event.key)){event.preventDefault();state.city=marker.dataset.city;navigate('analytics');}});
document.addEventListener('submit',async event=>{const form=event.target;if(!['tracking-form','task-form','interview-form','batch-form'].includes(form.id))return;event.preventDefault();if(state.busy)return;const values=Object.fromEntries(new FormData(form));let payload,message;
  if(form.id==='tracking-form'){payload={action:'application',job_ids:[form.dataset.id],...values};message='Application progress saved.';}
  else if(form.id==='task-form'){payload={action:'task',id:form.dataset.id,status:values.status,notes:values.notes};const currentTask=(state.data.tasks||[]).find(t=>t.id===form.dataset.id);if(values.status!=='unscheduled'||currentTask?.due_date)payload.due_date=values.due_date;if(values.actual_minutes!=='')payload.actual_minutes=Number(values.actual_minutes);message='Practice progress saved.';}
  else if(form.id==='interview-form'){payload={action:'interview',...values,starts_at:new Date(values.starts_at).toISOString()};message='Interview recorded and preparation schedule updated.';}
  else{if(values.confirmed!=='on')return;payload={action:'application',job_ids:[...state.selected],stage:'applied'};message='Completed submissions recorded.';}
  const ok=await mutate(payload,message);if(ok){$('#action-dialog').close();if(state.detail&&form.id==='tracking-form'){state.detail.application=appById(form.dataset.id);renderDetail();}}
});
function setMenuOpen(open) {
  $('.sidebar').classList.toggle('open', open);
  $('#menu-toggle').setAttribute('aria-expanded', String(open));
}
$('#refresh').addEventListener('click',()=>refresh(true));
$('#menu-toggle').addEventListener('click',()=>setMenuOpen(!$('.sidebar').classList.contains('open')));
document.addEventListener('click',event=>{if(!event.target.closest('.sidebar,#menu-toggle'))setMenuOpen(false);});
window.addEventListener('hashchange',()=>{applyRoute();state.pageIndex=0;setMenuOpen(false);render();if(state.page==='analytics'&&state.focusMap){$('.analytics-location-grid')?.scrollIntoView?.({block:'start'});}else window.scrollTo(0,0);state.focusMap=false;});
for(const dialog of document.querySelectorAll('dialog'))dialog.addEventListener('click',event=>{if(event.target===dialog){const r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)dialog.close();}});
// Bootstrap browser session.
applyRoute();
refresh();
window.addEventListener('focus', checkAppVersion);
setInterval(checkAppVersion, 60000);

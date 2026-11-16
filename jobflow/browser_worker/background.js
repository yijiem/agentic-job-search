/* MV3 worker. All Jobflow requests execute in the connected Jobflow tab's
 * isolated world, preserving its authenticated session and normal CSRF checks.
 * There is no externally_connectable endpoint or page-message command bridge. */
'use strict';
const WORKER_ALARM = 'jobflow-next-application';
const CONTROL_ALARM = 'jobflow-worker-control';
const STORE_KEY = 'jobflowWorker';
const RUNTIME_INSTANCE = crypto.randomUUID();
let busy = false;
let pumpPending = null;
let controlBusy = false, controlCount = 0, controlQueue = Promise.resolve();

function withControl(operation) {
  controlCount++; controlBusy = true;
  const next = controlQueue.then(operation);
  controlQueue = next.catch(() => {}).finally(() => { if (--controlCount === 0) controlBusy = false; });
  return controlQueue.then(() => next);
}
function workerVersion() { return chrome.runtime.getManifest().version; }
function sameWorkspace(left, right) {
  return Boolean(left && right && left.origin === right.origin && left.workspace_id === right.workspace_id);
}

function isPublicHttps(value) {
  try {
    const url = new URL(value), host = url.hostname.toLowerCase().replace(/\.$/, '');
    return url.protocol === 'https:' && !url.username && !url.password && (!url.port || url.port === '443') &&
      host.includes('.') && !host.endsWith('.localhost') && !host.endsWith('.local') && !host.endsWith('.internal') &&
      !/^[\d.]+$/.test(host) && !host.includes(':') && host !== 'localhost';
  } catch { return false; }
}
function workspaceURL(value) {
  try { const url = new URL(value); return isPublicHttps(value) || url.protocol === 'http:' && ['localhost','127.0.0.1'].includes(url.hostname) && !url.username && !url.password; }
  catch { return false; }
}
function originPattern(value) { return new URL(value).origin + '/*'; }
function sameApplicationURL(original, current) {
  try {
    const source = new URL(original), target = new URL(current);
    if (!isPublicHttps(current) || source.origin !== target.origin) return false;
    const strip = path => path.replace(/\/(?:apply|application)\/?$/, '').replace(/\/$/, '');
    if (strip(source.pathname) !== strip(target.pathname)) return false;
    // Query-based posting identifiers must survive navigation; tracking params
    // may change. Never apply to another job that shares the same form schema.
    for (const key of ['gh_jid','job_id','jobId','postingId','requisitionId']) if (source.searchParams.get(key) !== target.searchParams.get(key)) return false;
    return true;
  } catch { return false; }
}
function relevantOrigins(snapshot) {
  const origins = new Set();
  for (const batch of snapshot?.batches || []) {
    if (batch.status === 'cancelled' || batch.paused) continue;
    for (const item of batch.items || []) if (['pending','queued','needs_input','ready','inspecting','running'].includes(item.status) && isPublicHttps(item.url)) origins.add(originPattern(item.url));
  }
  return [...origins].sort();
}
function safeActive(task, tabId, phase) {
  return {batch_id:task.batch_id,job_id:task.job_id,attempt_id:task.attempt_id,mode:task.mode,url:task.url,tabId,phase,baseline_receipts:task.baseline_receipts || []};
}
async function readState() { return (await chrome.storage.local.get(STORE_KEY))[STORE_KEY] || {worker_id:crypto.randomUUID(),enabled:false}; }
async function changeState(patch) {
  const next = {...await readState(),...patch};
  await chrome.storage.local.set({[STORE_KEY]:next}); return next;
}

async function api(connection, payload = null) {
  if (!connection?.tabId || !workspaceURL(connection.origin)) throw new Error('Connect the worker from an open Jobflow tab.');
  const results = await chrome.scripting.executeScript({
    target:{tabId:connection.tabId},
    func:async (expected, payload) => {
      if (location.origin !== expected.origin || !document.querySelector('meta[name="jobflow-build"]')) return {error:'The connected tab is no longer Jobflow. Reconnect from your workspace.'};
      try {
        const response = await fetch('/api/batches', {cache:'no-store',credentials:'same-origin'});
        if (!response.ok) return {error:'Open Jobflow and sign in again before continuing.'};
        const state = await response.json();
        if (!state.workspace_id || !state.csrf_token || !Array.isArray(state.batches)) return {error:'This Jobflow server does not support browser application batches yet.'};
        if (expected.workspace_id && state.workspace_id !== expected.workspace_id) return {error:'The Jobflow account or workspace changed. Reconnect explicitly to continue.'};
        if (!payload) return {value:state};
        const result = await fetch('/api/action', {method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':state.csrf_token},body:JSON.stringify({...payload,action:'batch',workspace_id:state.workspace_id})});
        const body = await result.json();
        return result.ok && body.ok !== false && !body.error ? {value:body} : {error:body.error || 'Jobflow could not save this update.'};
      } catch { return {error:'Jobflow is unavailable. Keep its tab open and reconnect when it is running.'}; }
    },args:[connection,payload]
  }).catch(problem => {
    if (/cannot access contents of|missing host permission|cannot access (?:this|the) page/i.test(String(problem.message || problem))) {
      // Reloading removes Chrome's temporary activeTab grant. Keep the existing
      // connection/checkpoints and explain the user gesture that restores it;
      // never request persistent workspace access as a background fallback.
      const error = new Error(`Open the connected Jobflow tab (${connection.origin}), then click the Jobflow Browser Worker extension icon to restore access after reload. Click Connect this Jobflow tab if it still needs reconnecting.`);
      error.code = 'workspace_access_required';
      throw error;
    }
    throw problem;
  });
  const result = results[0]?.result;
  if (result?.error) throw new Error(result.error);
  if (!result?.value) throw new Error('The Jobflow tab did not respond. Reconnect from your workspace.');
  return result.value;
}
function scopedReload(state) {
  const marker = state.reload_marker;
  return marker?.worker_id === state.worker_id && sameWorkspace(marker, state.connection) ? marker : null;
}
async function heartbeatControl(state) {
  if (!state.connection) return {blocked:false};
  let marker = scopedReload(state);
  // runtime.reload() ends this instance. Never acknowledge completion, start
  // work, or invoke reload again while the old instance is still running.
  if (marker?.phase === 'reload_invoked' && marker.runtime_instance === RUNTIME_INSTANCE) return {blocked:true};
  const heartbeat = {op:'worker_heartbeat',worker_id:state.worker_id,version:workerVersion(),
    reload_supported:true,enabled:Boolean(state.enabled),active:Boolean(busy || state.active)};
  // Fetch the current request before reporting a persisted result. A request
  // may have expired and been replaced while this browser was offline.
  let snapshot = await api(state.connection,heartbeat);
  let request = snapshot.reload_request;
  if (marker && (request?.id !== marker.request_id || request?.worker_id !== state.worker_id)) {
    if (!busy && !state.active && marker.phase !== 'settled') {
      marker = {...marker,phase:'settled',status:'superseded'};
      await changeState({reload_marker:marker});
    }
  }
  if (marker && request && marker.request_id === request.id && request.worker_id === state.worker_id && ['pending','reloading'].includes(request.status)) {
    const completed = marker.phase === 'reload_invoked' && marker.runtime_instance !== RUNTIME_INSTANCE;
    const failed = marker.phase === 'failed' || marker.phase === 'ack_pending' && marker.runtime_instance !== RUNTIME_INSTANCE ||
      completed && request.status === 'pending';
    if (completed || failed) {
      snapshot = await api(state.connection,{...heartbeat,...(failed ? {reload_failed_id:marker.request_id} : {reload_completed_id:marker.request_id})});
      request = snapshot.reload_request;
    }
  }
  if (!request || request.worker_id !== state.worker_id) return {blocked:false};
  if (marker?.request_id === request.id && ['complete','outdated','expired','failed'].includes(request.status)) {
    if (!busy && !state.active && marker.phase !== 'settled') {
      const message = request.status === 'complete' ? 'Worker reloaded. Review employer access and click Start when ready.' :
        request.status === 'outdated' ? 'Reload finished, but newer worker files are still needed. Install the latest download, then reload the extension.' :
          request.status === 'failed' ? 'The worker reload did not finish. Request it again from Jobflow or reload the extension manually.' :
            'The worker reload request expired. Request it again from Jobflow or reload the extension manually.';
      await changeState({reload_marker:{...marker,phase:'settled',status:request.status},message});
    }
    return {blocked:false};
  }
  if (!['pending','reloading'].includes(request.status)) return {blocked:false};
  const expires = Date.parse(request.expires_at);
  if (!Number.isFinite(expires) || expires <= Date.now()) return {blocked:false};
  // A pending reload takes precedence over a new claim. In-flight applications
  // and retained checkpoints are left intact; normal reconciliation runs first.
  if (busy || state.active || request.status !== 'pending' || marker?.request_id === request.id) return {blocked:true};
  const nextMarker = {request_id:request.id,worker_id:state.worker_id,origin:state.connection.origin,
    workspace_id:state.connection.workspace_id,target_version:request.target_version,phase:'ack_pending',runtime_instance:RUNTIME_INSTANCE};
  await changeState({reload_marker:nextMarker});
  try {
    const acknowledged = await api(state.connection,{op:'worker_reload_ack',worker_id:state.worker_id,request_id:request.id});
    const accepted = acknowledged.reload_request;
    if (accepted?.id !== request.id || accepted.worker_id !== state.worker_id || accepted.status !== 'reloading' ||
        accepted.target_version !== request.target_version || !Number.isFinite(Date.parse(accepted.expires_at)) || Date.parse(accepted.expires_at) <= Date.now()) {
      throw new Error('Jobflow did not acknowledge this reload request.');
    }
    const current = await readState();
    if (busy || current.active || !sameWorkspace(current.connection,state.connection)) throw new Error('A browser task or workspace changed before reload.');
    await changeState({enabled:false,reload_marker:{...nextMarker,phase:'reload_invoked'},
      message:'Reloading the worker. Applications will remain stopped until you click Start.'});
    await chrome.alarms.clear(WORKER_ALARM);
    chrome.runtime.reload();
  } catch (error) {
    // An ACK response can be lost after the server accepted it. Persist the
    // attempt and never retry that request automatically, including on restart.
    await changeState({enabled:false,reload_marker:{...nextMarker,phase:'failed'},
      message:`Worker reload could not finish: ${String(error.message || error).slice(0,350)} Reload the extension manually or request a new reload in Jobflow.`});
    await chrome.alarms.clear(WORKER_ALARM);
  }
  return {blocked:true};
}
async function pollControl() {
  if (controlBusy) return;
  return withControl(async () => {
    const state = await readState();
    if (!state.connection) return;
    try { await heartbeatControl(state); }
    catch (error) {
      if (!busy && !state.active) await changeState({message:`Worker connection needs attention: ${String(error.message || error).slice(0,500)}`});
    }
  });
}
async function initializeControl(browserRestart = false) {
  return withControl(async () => {
    if (browserRestart) {
      await changeState({enabled:false,message:'Browser restarted. Start the worker to reconcile any interrupted task before continuing.'});
      await chrome.alarms.clear(WORKER_ALARM);
    }
    const state = await readState();
    if (state.connection) {
      await chrome.alarms.create(CONTROL_ALARM,{periodInMinutes:0.5});
      try { await heartbeatControl(state); } catch {} // The periodic poll retries the connection, never a reload.
    } else await chrome.alarms.clear(CONTROL_ALARM);
  });
}
async function report(state, task, status, extra = {}) {
  return api(state.connection, {op:'report',worker_id:state.worker_id,batch_id:task.batch_id,job_id:task.job_id,attempt_id:task.attempt_id,status,...extra});
}
async function runForm(tabId, operation, task) {
  const tab = await chrome.tabs.get(tabId);
  // Only confirmation may follow a different path on the same employer origin.
  const allowed = operation === 'confirmation' ? new URL(tab.url || '').origin === new URL(task.url).origin && isPublicHttps(tab.url) : sameApplicationURL(task.url, tab.url);
  if (!allowed) throw new Error('The employer redirected to a different application address. Review the destination manually.');
  await chrome.scripting.executeScript({target:{tabId},files:['content.js']});
  const results = await chrome.scripting.executeScript({target:{tabId},func:async (operation, task, expectedOrigin) => {
    if (location.origin !== expectedOrigin) return {error:'The application page changed origin. Review it manually.'};
    try { return {value:await globalThis.JobflowForm[operation](task)}; }
    catch (error) { return {error:String(error.message || 'The employer form needs attention.').slice(0,500)}; }
  },args:[operation,task,new URL(task.url).origin]});
  const result = results[0]?.result;
  if (result?.error) throw new Error(result.error);
  if (!result?.value) throw new Error('The employer page did not respond.');
  return result.value;
}
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve,milliseconds));
async function loaded(tabId) {
  for (let attempt = 0; attempt < 40; attempt++) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status === 'complete') { await sleep(700); return; }
    await sleep(500);
  }
  throw new Error('The employer page is still loading. Open the task tab and retry inspection.');
}
async function recover(state) {
  const task = state.active;
  if (!task) return;
  const latest = await api(state.connection);
  const current = latest.batches.find(batch => batch.id === task.batch_id)?.items?.find(item => item.job_id === task.job_id);
  if (!current || current.worker_id && current.worker_id !== state.worker_id || !['inspecting','running','submitting','uncertain'].includes(current.status)) {
    await changeState({active:null,lastTabId:task.tabId,message:'The retained task was already resolved in Jobflow.'}); return;
  }
  // A service worker can stop between click and receipt. Never repeat a claim,
  // fill, or click after restart; retain the tab for evidence/manual resolution.
  let proof;
  if (task.mode === 'submit' && task.tabId) {
    try { proof = await runForm(task.tabId,'confirmation',task); } catch {}
  }
  if (proof?.confirmed && ['submitting','click_pending','awaiting_confirmation'].includes(task.phase)) {
    try {
      await report(state,task,'submitted',{evidence:{url:proof.url,text:proof.text},message:'Confirmed in the retained employer tab after worker interruption.'});
      await changeState({active:null,lastTabId:task.tabId,message:'Submission confirmed after resuming the worker.'}); return;
    } catch {} // A revoked/expired claim must be resolved in Jobflow.
  }
  if (current.status === 'uncertain') {
    await changeState({active:null,lastTabId:task.tabId,message:'The interrupted application is already marked uncertain in Jobflow. Check its employer tab and record the result manually.'}); return;
  }
  const status = task.mode === 'submit' ? 'uncertain' : 'needs_input';
  await report(state,task,status,{message:task.mode === 'submit' ? 'The browser worker was interrupted. Check the retained employer tab before recording or retrying; no second submission was attempted.' : 'Inspection was interrupted. Open the retained tab, then retry inspection in Jobflow.'});
  await changeState({active:null,lastTabId:task.tabId,message:'An interrupted task needs review in Jobflow. No application was retried.'});
}
async function pauseTask(state, task, tabId, message, uncertain = false) {
  await report(state,task,uncertain ? 'uncertain' : 'needs_input',{message});
  await changeState({active:null,lastTabId:tabId,message});
}
async function stillAuthorized(state, task, expectedStatus = 'submitting') {
  if (!(await readState()).enabled) throw new Error('The browser worker was stopped before submission. Review this role in Jobflow.');
  const latest = await api(state.connection);
  const batch = latest.batches.find(row => row.id === task.batch_id);
  const item = batch?.items?.find(row => row.job_id === task.job_id);
  if (!batch || batch.paused || ['paused','cancelled'].includes(batch.status) || item?.status !== expectedStatus) throw new Error('Submission was paused or the reviewed application changed. No submit click was made.');
}
async function processTask(state, task) {
  let tabId, clickPossible = false;
  try {
    if (!isPublicHttps(task.url)) throw new Error('Only public HTTPS employer application pages can be opened by the worker.');
    const granted = await chrome.permissions.contains({origins:[originPattern(task.url)]});
    if (!granted) throw new Error('Grant access to this employer from the worker popup, then retry inspection in Jobflow.');
    // Persist the claim BEFORE creating a tab; a crash must never trigger a new
    // claim while an existing send outcome is unresolved.
    await changeState({active:safeActive(task,null,'opening'),message:`${task.mode === 'inspect' ? 'Inspecting' : 'Preparing'} ${task.company} application…`});
    const tabKey = task.batch_id + ':' + task.job_id;
    const prior = (await readState()).taskTabs?.[tabKey];
    let tab;
    if (prior) {
      try { const existing = await chrome.tabs.get(prior); if (sameApplicationURL(task.url,existing.url)) tab = existing; } catch {}
    }
    if (!tab) tab = await chrome.tabs.create({url:task.url,active:false});
    tabId = tab.id;
    const taskTabs = {...(await readState()).taskTabs,[tabKey]:tabId};
    await changeState({active:safeActive(task,tabId,'loading'),lastTabId:tabId,taskTabs});
    await loaded(tabId);
    if (task.mode === 'inspect') {
      const inspected = await runForm(tabId,'inspect',task);
      await report(state,task,'inspected',inspected);
      await changeState({active:null,lastTabId:tabId,message:'Form inspected. Review answers and authorize submissions in Jobflow.'});
      return;
    }
    const before = await runForm(tabId,'confirmation',task);
    if (before.confirmed) throw new Error('This employer page already shows an application confirmation. Check for a previous submission in Jobflow.');
    task.baseline_receipts = before.receipts || [];
    await stillAuthorized(state,task,'running');
    await runForm(tabId,'fill',task);
    await changeState({active:safeActive(task,tabId,'filled')});
    if (!(await readState()).enabled) throw new Error('Worker stopped before submission. Review this application in Jobflow.');
    // Persist sending state server-side before the ONLY submit click. If this
    // request times out, leave an uncertain outcome; never click speculatively.
    clickPossible = true;
    await changeState({active:safeActive(task,tabId,'submitting')});
    await report(state,task,'submitting',{form_signature:task.form_signature,message:'Approved materials filled; preparing one submission click.'});
    await stillAuthorized(state,task);
    await changeState({active:safeActive(task,tabId,'click_pending')});
    await runForm(tabId,'clickSubmit',task);
    await changeState({active:safeActive(task,tabId,'awaiting_confirmation')});
    let proof;
    for (let attempt = 0; attempt < 18; attempt++) {
      await sleep(1000);
      try { proof = await runForm(tabId,'confirmation',task); if (proof.confirmed) break; } catch {}
    }
    if (proof?.confirmed) {
      await report(state,task,'submitted',{evidence:{url:proof.url,text:proof.text},message:'Employer confirmation observed after the submission click.'});
      await changeState({active:null,lastTabId:tabId,message:'Application submitted and confirmed by the employer.'});
    } else await pauseTask(state,task,tabId,'A submit click was made, but no clear employer confirmation was observed. Check the retained tab; this application will not be automatically retried.',true);
  } catch (error) {
    const message = String(error.message || 'This application needs attention.').slice(0,700);
    try { await pauseTask(state,task,tabId,message,clickPossible); }
    catch {
      // Keep the checkpoint if Jobflow is unavailable; recover() resolves it
      // on the next explicit Start without making another submission attempt.
      await changeState({enabled:false,lastTabId:tabId || null,message:'Jobflow could not record the worker result. Reconnect, then Start to reconcile the retained task. No retry will be sent.'});
      await chrome.alarms.clear(WORKER_ALARM);
    }
  }
}
function pump() {
  if (pumpPending) return pumpPending;
  if (busy) return Promise.resolve();
  // Work and control alarms can arrive together. Retain one work request
  // behind the control queue instead of dropping every coincident work tick.
  pumpPending = runPump().finally(() => { pumpPending = null; });
  return pumpPending;
}
async function runPump() {
  let ownsWork = false;
  try {
    const prepared = await withControl(async () => {
      if (busy) return null;
      const state = await readState();
      if (!state.enabled || !state.connection) return null;
      if (state.active) { busy = true; ownsWork = true; return {state,recover:true}; }
      let control;
      try { control = await heartbeatControl(state); }
      catch (error) {
        // A control-channel outage defers the next claim without changing the
        // user's Start/Stop choice or interrupting an existing application.
        await changeState({message:`Waiting for the Jobflow connection before starting another application: ${String(error.message || error).slice(0,350)}`});
        return null;
      }
      if (control.blocked) return null;
      return {state};
    });
    if (!prepared) return;
    // Release the control queue after the heartbeat. Stop/disconnect commands
    // queued while that request was in flight must take effect before a claim.
    const next = prepared.recover ? prepared : await withControl(async () => {
      if (busy) return null;
      const state = await readState();
      if (!state.enabled || !state.connection || state.active ||
          state.worker_id !== prepared.state.worker_id || !sameWorkspace(state.connection,prepared.state.connection)) return null;
      const claimed = await api(state.connection,{op:'claim',worker_id:state.worker_id});
      if (!claimed.task) { await changeState({message:'Waiting for forms to inspect or reviewed applications to submit.'}); return null; }
      busy = true; ownsWork = true; return {state,task:claimed.task};
    });
    if (!next) return;
    if (next.recover) await recover(next.state);
    else await processTask(next.state,next.task);
  } catch (error) {
    await changeState({enabled:false,message:String(error.message || 'Reconnect the worker from Jobflow.').slice(0,700)});
    await chrome.alarms.clear(WORKER_ALARM);
  } finally { if (ownsWork) busy = false; }
}
async function publicState() {
  const state = await readState(); let origins = [], counts = {}, error = '', workspaceAccessRequired = false;
  if (state.connection) {
    try {
      const snapshot = await api(state.connection);
      origins = relevantOrigins(snapshot);
      for (const batch of snapshot.batches) for (const item of batch.items || []) counts[item.status] = (counts[item.status] || 0) + 1;
    } catch (problem) { error = String(problem.message); workspaceAccessRequired = problem.code === 'workspace_access_required'; }
  }
  return {connected:Boolean(state.connection),origin:state.connection?.origin || '',version:workerVersion(),enabled:Boolean(state.enabled),busy:Boolean(busy || controlBusy),message:state.message || '',error,workspaceAccessRequired,origins,counts,lastTabId:state.lastTabId || null,active:Boolean(state.active)};
}
async function command(message) {
  if (message.op === 'state') return publicState();
  if (message.op === 'open-task') return commandLocked(message);
  const result = await withControl(() => commandLocked(message));
  if (message.op === 'start') void pump();
  return result;
}
async function commandLocked(message) {
  if (message.op === 'connect') {
    const previous = await readState();
    if (busy) throw new Error('Stop and finish the current browser step before reconnecting.');
    const tab = await chrome.tabs.get(message.tabId);
    if (!workspaceURL(tab.url)) throw new Error('Open your Jobflow workspace, then click Connect again.');
    const connection = {tabId:tab.id,origin:new URL(tab.url).origin};
    // Reconnect after a browser restart to the same workspace even if an old
    // attempt needs reconciliation. Never move that attempt to another account.
    const priorReload = scopedReload(previous);
    if ((previous.active || priorReload && priorReload.phase !== 'settled') && previous.connection) {
      if (connection.origin !== previous.connection.origin) throw new Error('Reconnect to the same Jobflow origin while an attempt needs reconciliation.');
      connection.workspace_id = previous.connection.workspace_id;
    }
    const snapshot = await api(connection);
    connection.workspace_id = snapshot.workspace_id;
    await changeState({connection,enabled:false,
      ...(priorReload && !sameWorkspace(previous.connection,connection) ? {reload_marker:null} : {}),
      message:'Connected. Review employer access, then start the worker.'});
    await chrome.alarms.create(CONTROL_ALARM,{periodInMinutes:0.5});
    try { await heartbeatControl(await readState()); } catch {} // Registration retries even while stopped.
    return publicState();
  }
  if (message.op === 'start') {
    const state = await readState();
    if (!state.connection) throw new Error('Connect an open Jobflow tab first.');
    if (busy) throw new Error('The current browser step is still running.');
    await api(state.connection);
    if ((await heartbeatControl(state)).blocked && !state.active) throw new Error('A worker reload is pending. Finish the reload before starting applications.');
    await changeState({enabled:true,message:'Worker started. It inspects forms and submits only applications authorized in Jobflow.'});
    await chrome.alarms.create(WORKER_ALARM,{periodInMinutes:0.5});
    await chrome.alarms.create(CONTROL_ALARM,{periodInMinutes:0.5});
    return publicState();
  }
  if (message.op === 'stop') {
    await changeState({enabled:false,message:'Worker stopped. An already-clicked submission will still be checked for confirmation.'});
    await chrome.alarms.clear(WORKER_ALARM); return publicState();
  }
  if (message.op === 'open-task') {
    const state = await readState();
    const tabId = state.active?.tabId || state.lastTabId;
    if (!tabId) throw new Error('No employer task tab is available yet.');
    const tab = await chrome.tabs.update(tabId,{active:true});
    await chrome.windows.update(tab.windowId,{focused:true}); return publicState();
  }
  if (message.op === 'disconnect') {
    const state = await readState();
    if (busy || state.active) throw new Error('Stop and resolve the current task before disconnecting.');
    if (scopedReload(state)?.phase === 'reload_invoked') throw new Error('The worker is finishing a reload. Wait for it to reconnect before disconnecting.');
    await chrome.alarms.clear(WORKER_ALARM);
    await chrome.alarms.clear(CONTROL_ALARM);
    await chrome.storage.local.remove(STORE_KEY); return publicState();
  }
  throw new Error('Unknown worker command.');
}
function bootstrap() {
  chrome.storage.local.setAccessLevel({accessLevel:'TRUSTED_CONTEXTS'}).catch(() => {});
  chrome.runtime.onMessage.addListener((message,sender,respond) => {
    // Only extension UI pages may issue commands. Content scripts and website
    // tabs cannot start a worker or request private materials.
    if (sender.id !== chrome.runtime.id || sender.tab || !sender.url?.startsWith(chrome.runtime.getURL(''))) return false;
    command(message).then(value => respond({ok:true,...value}), error => respond({ok:false,error:String(error.message || error)}));
    return true;
  });
  chrome.alarms.onAlarm.addListener(alarm => {
    if (alarm.name === WORKER_ALARM) void pump();
    else if (alarm.name === CONTROL_ALARM) void pollControl();
  });
  chrome.runtime.onStartup.addListener(() => { void initializeControl(true); });
  void initializeControl();
}
if (typeof module !== 'undefined' && module.exports) module.exports = {isPublicHttps,workspaceURL,originPattern,sameApplicationURL,relevantOrigins,safeActive};
else bootstrap();

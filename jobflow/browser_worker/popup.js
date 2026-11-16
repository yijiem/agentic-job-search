'use strict';
let current = null;
const $ = id => document.getElementById(id);
async function request(op, extra = {}) {
  const response = await chrome.runtime.sendMessage({op,...extra});
  if (!response?.ok) throw new Error(response?.error || 'The browser worker did not respond. Reload the extension.');
  draw(response); return response;
}
function draw(state) {
  current = state;
  $('status').textContent = state.error || state.message || 'Open Jobflow in this browser, then connect its tab.';
  $('status').className = state.error ? 'error' : '';
  $('workspace').textContent = state.origin ? `Workspace: ${state.origin}` : 'No workspace connected';
  $('version').textContent = `Extension version ${state.version || chrome.runtime.getManifest().version}`;
  $('counts').textContent = Object.entries(state.counts || {}).map(([key,value]) => `${value} ${key.replaceAll('_',' ')}`).join(' · ');
  $('start').disabled = !state.connected || Boolean(state.error) || state.enabled;
  $('stop').disabled = !state.enabled;
  $('connect').disabled = state.busy;
  $('connect').textContent = state.workspaceAccessRequired ? 'Reconnect this Jobflow tab' : 'Connect this Jobflow tab';
  $('connect').title = state.workspaceAccessRequired ? 'Open your connected Jobflow tab and click the extension icon there before reconnecting.' : '';
  $('open-task').disabled = !state.lastTabId;
  $('disconnect').disabled = !state.connected || state.busy || state.active;
  $('origins').replaceChildren(...(state.origins || []).map(origin => { const item=document.createElement('li'); item.textContent=origin.replace(/\/\*$/,''); return item; }));
}
function error(problem) { $('status').textContent = problem.message || String(problem); $('status').className = 'error'; }
$('connect').addEventListener('click', async () => {
  try { const [tab] = await chrome.tabs.query({active:true,currentWindow:true}); await request('connect',{tabId:tab.id}); } catch (problem) { error(problem); }
});
$('start').addEventListener('click', async () => {
  try {
    // Call permissions.request directly in the user gesture, before any API
    // fetch. The list was loaded while opening the popup.
    const granted = !current?.origins?.length || await chrome.permissions.request({origins:current.origins});
    if (!granted) throw new Error('Employer access was not granted. No application task was started.');
    await request('start');
  } catch (problem) { error(problem); }
});
for (const op of ['stop','open-task','disconnect']) $(op).addEventListener('click',() => request(op).catch(error));
request('state').catch(error);
const timer=setInterval(() => request('state').catch(error),3000);
window.addEventListener('unload',() => clearInterval(timer));

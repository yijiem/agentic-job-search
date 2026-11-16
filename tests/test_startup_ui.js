/* Initial workspace loading overlaps static assets without duplicate API reads. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assets = path.join(__dirname, '../jobflow/web_assets');
const bootstrap = fs.readFileSync(path.join(assets, 'bootstrap.js'), 'utf8');
const source = fs.readFileSync(path.join(assets, 'app.js'), 'utf8').split('// Bootstrap browser session.')[0];
const fixture = {today:'2026-11-14',jobs:[],applications:[]};
function harness() {
  const nodes = new Map(), requests = [], redirects = [];
  let respond = async () => ({ok:true,status:200,json:async()=>fixture}), renders = 0;
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {innerHTML:'',textContent:'',hidden:false,
      addEventListener(){},setAttribute(){},classList:{toggle(){},add(){},remove(){}},
      querySelector(){return null;},querySelectorAll(){return [];}});
    return nodes.get(selector);
  };
  const context = vm.createContext({URL,URLSearchParams,Intl,Date,
    window:{addEventListener(){}},document:{querySelector:node,querySelectorAll:()=>[],addEventListener(){}},
    location:{hash:'#jobs',assign(url){redirects.push(url);}},setTimeout(){},clearTimeout(){},
    fetch(url,options){requests.push({url,options});return respond();},renderHook(){renders++;},
  });
  return {context,requests,redirects,nodes,
    bootstrap(){vm.runInContext(bootstrap,context);},
    app(){vm.runInContext(source,context);vm.runInContext('render=renderHook',context);},
    refresh(){return vm.runInContext('refresh()',context);},
    respond(callback){respond=callback;},renders(){return renders;},
  };
}
async function main() {
  {
    const app=harness();let resolve;
    app.respond(()=>new Promise(done=>{resolve=done;}));
    app.bootstrap();
    assert.equal(app.requests.length,1,'Workspace request begins before the main application script runs');
    app.app();const loaded=app.refresh();
    assert.equal(app.requests.length,1,'App reuses the in-flight request');
    resolve({ok:true,status:200,json:async()=>fixture});await loaded;
    assert.equal(app.renders(),1);
    assert.equal(app.context.window.JobflowInitialState,undefined,'Initial response is consumed once');
    app.respond(async()=>({ok:true,status:200,json:async()=>fixture}));
    await app.refresh();
    assert.equal(app.requests.length,2,'Manual refresh reads current workspace data');
    assert.ok(app.requests.every(row=>row.url==='/api/state'&&row.options.cache==='no-store'));
  }
  {
    const app=harness();app.app();await app.refresh();app.bootstrap();
    assert.equal(app.requests.length,1,'A late async bootstrap cannot duplicate the application request');
  }
  {
    const app=harness();app.respond(async()=>{throw new Error('Offline');});
    app.bootstrap();await Promise.resolve();app.app();await app.refresh();
    assert.match(app.nodes.get('#main').innerHTML,/Offline/);
    assert.equal(app.requests.length,1,'Initial network failure is displayed without an automatic duplicate retry');
    app.respond(async()=>({ok:true,status:200,json:async()=>fixture}));await app.refresh();
    assert.equal(app.renders(),1,'Explicit retry recovers from a failed early request');
  }
  for (const status of [401,409]) {
    const app=harness();app.respond(async()=>({ok:false,status,json:async()=>({code:'onboarding_required'})}));app.bootstrap();app.app();await app.refresh();
    assert.deepEqual(app.redirects,['/account']);assert.equal(app.requests.length,1);
  }
  {
    const app=harness();app.respond(async()=>({ok:false,status:409,json:async()=>({code:'workspace_busy',error:'Workspace is updating. Try again shortly.'})}));
    app.bootstrap();app.app();await app.refresh();
    assert.deepEqual(app.redirects,[],'A workspace conflict is not an onboarding redirect');
    assert.match(app.nodes.get('#main').innerHTML,/Try again shortly/);
    app.respond(async()=>({ok:true,status:200,json:async()=>fixture}));await app.refresh();
    assert.equal(app.renders(),1,'A busy workspace can recover in place');
  }
  const html=fs.readFileSync(path.join(assets,'index.html'),'utf8');
  assert.match(html,/<script src="\/bootstrap\.js\?v=__JOBFLOW_BUILD__" async>/);
  assert.doesNotMatch(html,/<(?:script|link)[^>]+vendor\/(?:leaflet|maplibre)/,'Map renderer assets stay outside the initial document request path');
  console.log('Initial workspace loading checks passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});

/* node tests/test_worker_scheduling.js — deterministic alarm/command races. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

const clone = value => JSON.parse(JSON.stringify(value));
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise,resolve};
}
async function flush() {
  for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve));
}
async function fixture() {
  let state = {worker_id:'worker',enabled:true,connection:{tabId:1,origin:'http://localhost:8765',workspace_id:'workspace'}};
  const listeners = {}, alarms = new Map();
  const test = {requests:[],events:[],heartbeatGate:null,processGate:null,request:null,reloads:0,
    tasks:[{batch_id:'batch',job_id:'job',attempt_id:'attempt',mode:'inspect',url:'https://jobs.example.com/role'}],
    activeTasks:0,maxActiveTasks:0};
  const on = name => ({addListener(callback) { listeners[name] = callback; }});
  const chrome = {
    storage:{local:{async get(){return {jobflowWorker:clone(state)};},async set(value){state=clone(value.jobflowWorker);},
      async remove(){state={};},async setAccessLevel(){}}},
    alarms:{async create(name,value){alarms.set(name,value);},async clear(name){alarms.delete(name);},onAlarm:on('alarm')},
    runtime:{id:'extension',getManifest:()=>({version:'0.1.2'}),getURL:()=> 'chrome-extension://extension/',
      onMessage:on('message'),onStartup:on('startup'),reload(){test.reloads++;}},
  };
  const context = vm.createContext({module:{exports:{}},chrome,crypto:webcrypto,URL,console,setTimeout,clearTimeout,test});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../jobflow/browser_worker/background.js'),'utf8'),context);
  vm.runInContext(`
    api = async (connection,payload) => {
      if (!payload) return {workspace_id:'workspace',batches:[]};
      test.requests.push({...payload});
      if (payload.op === 'worker_heartbeat') {
        if (test.heartbeatGate) {
          const gate = test.heartbeatGate; test.heartbeatGate = null;
          test.events.push('heartbeat waiting'); await gate.promise;
        }
        return {reload_request:test.request};
      }
      if (payload.op === 'worker_reload_ack') {
        test.request = {...test.request,status:'reloading'};
        return {reload_request:test.request};
      }
      if (payload.op === 'claim') {
        test.events.push('claim'); return {task:test.tasks.shift() || null};
      }
      throw new Error('Unexpected operation: ' + payload.op);
    };
    processTask = async (state,task) => {
      test.activeTasks++; test.maxActiveTasks = Math.max(test.maxActiveTasks,test.activeTasks);
      test.events.push('task started');
      await changeState({active:{attempt_id:task.attempt_id}});
      if (test.processGate) await test.processGate.promise;
      await changeState({active:null});
      test.activeTasks--; test.events.push('task finished');
    };
    recover = async state => { test.events.push('recovered'); await changeState({active:null}); };
    bootstrap();
  `,context);
  await flush();
  test.requests.length=0; test.events.length=0;
  return {test,listeners,alarms,run:code=>vm.runInContext(code,context),get state(){return state;}};
}

async function workspaceAccessTests() {
  let granted=false, permissionRequests=0, reloads=0;
  let request={id:'reload-access',worker_id:'worker',status:'reloading',target_version:'0.1.3',
    expires_at:new Date(Date.now()+60000).toISOString()};
  let state={worker_id:'worker',enabled:false,connection:{tabId:1,origin:'http://localhost:8765',workspace_id:'workspace'},
    reload_marker:{request_id:request.id,worker_id:'worker',origin:'http://localhost:8765',workspace_id:'workspace',
      phase:'reload_invoked',runtime_instance:'previous-runtime'}};
  const calls=[];
  const chrome={
    storage:{local:{async get(){return {jobflowWorker:clone(state)};},async set(value){state=clone(value.jobflowWorker);}}},
    runtime:{getManifest:()=>({version:'0.1.3'}),reload(){reloads++;}},
    permissions:{async request(){permissionRequests++;throw new Error('Workspace access must not be expanded automatically');}},
    scripting:{async executeScript(options){
      if(!granted)throw new Error('Cannot access contents of the page. Extension manifest must request permission to access the respective host.');
      const payload=options.args[1];
      if(payload){
        calls.push(payload);
        assert.equal(payload.op,'worker_heartbeat');
        if(payload.reload_completed_id)request={...request,status:'complete'};
        return [{result:{value:{reload_request:request}}}];
      }
      return [{result:{value:{workspace_id:'workspace',batches:[]}}}];
    }},
  };
  const context=vm.createContext({module:{exports:{}},chrome,crypto:webcrypto,URL,console,setTimeout,clearTimeout});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../jobflow/browser_worker/background.js'),'utf8'),context);
  const run=code=>vm.runInContext(code,context);
  const missing=await run('publicState()');
  assert.equal(missing.workspaceAccessRequired,true);
  assert.equal(missing.connected,true,'A temporary permission loss preserves the existing workspace binding');
  assert.match(missing.error,/Open the connected Jobflow tab \(http:\/\/localhost:8765\)/);
  assert.match(missing.error,/extension icon to restore access after reload/);
  assert.ok(!missing.error.includes('manifest must request permission'));
  await run('pollControl()');
  assert.equal(state.reload_marker.phase,'reload_invoked','Permission loss retains the reload completion checkpoint');
  assert.equal(calls.length,0,'No control mutation is reported without workspace access');
  assert.equal(permissionRequests,0,'Reconnect guidance never requests persistent host permissions');
  granted=true;
  await run('pollControl()');
  assert.equal(state.reload_marker.phase,'settled','The existing user gesture can resume reload confirmation');
  assert.equal(request.status,'complete');
  assert.equal(reloads,0,'Restoring workspace access never repeats the runtime reload');
  const restored=await run('publicState()');
  assert.equal(restored.workspaceAccessRequired,false);
  assert.equal(restored.error,'');

  const nodes=new Map();
  const node=()=>({textContent:'',title:'',disabled:false,addEventListener(){},replaceChildren(){}});
  const document={getElementById(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);},createElement:node};
  const popup=vm.createContext({document,window:{addEventListener(){}},setInterval:()=>1,clearInterval(){},
    chrome:{runtime:{async sendMessage(){return {ok:true,...missing};},getManifest:()=>({version:'0.1.3'})}}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../jobflow/browser_worker/popup.js'),'utf8'),popup);
  await flush();
  assert.equal(nodes.get('connect').textContent,'Reconnect this Jobflow tab');
  assert.equal(nodes.get('start').disabled,true,'Start remains disabled while workspace access needs restoring');
  assert.match(nodes.get('status').textContent,/Open the connected Jobflow tab/);
  popup.restored=restored;
  vm.runInContext('draw(restored)',popup);
  assert.equal(nodes.get('connect').textContent,'Connect this Jobflow tab');
  assert.equal(nodes.get('connect').title,'');
}

async function main() {
  {
    const app=await fixture(), gate=deferred(); app.test.heartbeatGate=gate;
    app.listeners.alarm({name:'jobflow-worker-control'});
    app.listeners.alarm({name:'jobflow-next-application'});
    await flush();
    assert.ok(app.test.events.includes('heartbeat waiting'));
    assert.equal(app.test.requests.filter(row=>row.op==='claim').length,0,'Work waits behind the heartbeat');
    gate.resolve(); await flush();
    assert.equal(app.test.requests.filter(row=>row.op==='claim').length,1,'Coincident work alarm is retained until control finishes');
    assert.deepEqual(app.test.events.filter(value=>value.startsWith('task ')),['task started','task finished']);
  }
  {
    const app=await fixture(), heartbeat=deferred(), task=deferred();
    app.test.heartbeatGate=heartbeat; app.test.processGate=task;
    const control=app.run('pollControl()');
    const first=app.run('pump()'), second=app.run('pump()'), third=app.run('pump()');
    assert.strictEqual(first,second,'Concurrent work requests share the pending pump');
    assert.strictEqual(second,third);
    heartbeat.resolve(); await control; await flush();
    app.listeners.alarm({name:'jobflow-next-application'});
    await flush();
    assert.equal(app.test.requests.filter(row=>row.op==='claim').length,1);
    assert.equal(app.test.maxActiveTasks,1,'Only one application task may execute');
    task.resolve(); await Promise.all([first,second,third]);
    assert.equal(app.test.activeTasks,0);
  }
  for (const controlFirst of [false,true]) {
    const app=await fixture(), gate=deferred(); app.test.heartbeatGate=gate;
    const control=controlFirst ? app.run('pollControl()') : null;
    const work=app.run('pump()'); await flush();
    const stop=app.run('command({op:"stop"})');
    gate.resolve(); await Promise.all([control,work,stop]);
    assert.equal(app.state.enabled,false);
    assert.equal(app.test.requests.filter(row=>row.op==='claim').length,0,
      'Stop queued during a delayed control request takes effect before claiming');
  }
  {
    const app=await fixture(), gate=deferred(); app.test.heartbeatGate=gate;
    const control=app.run('pollControl()'), work=app.run('pump()');
    await flush();
    app.test.request={id:'reload-request',worker_id:'worker',status:'pending',target_version:'0.1.2',
      expires_at:new Date(Date.now()+60000).toISOString()};
    gate.resolve(); await Promise.all([control,work]);
    assert.equal(app.test.reloads,1,'Reload takes precedence over a queued application pump');
    assert.equal(app.test.requests.filter(row=>row.op==='claim').length,0);
    assert.equal(app.state.enabled,false);
  }
  {
    const app=await fixture(), task=deferred(); app.test.processGate=task;
    const work=app.run('pump()'); await flush();
    app.test.request={id:'reload-request',worker_id:'worker',status:'pending',target_version:'0.1.2',
      expires_at:new Date(Date.now()+60000).toISOString()};
    await app.run('pollControl()');
    assert.equal(app.test.reloads,0,'An active task still prevents reloading');
    assert.equal(app.test.activeTasks,1);
    task.resolve(); await work;
    await app.run('pollControl()');
    assert.equal(app.test.reloads,1,'Reload proceeds only after the task finishes');
  }
  await workspaceAccessTests();
  console.log('Worker scheduling tests passed: coincident alarms, coalesced claims, Stop precedence, idle-only reloads, and workspace reconnect guidance.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});

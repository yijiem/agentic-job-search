/* node tests/test_browser_worker.js — no browser packages or live submissions. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const forms = require('../jobflow/browser_worker/content.js');
const worker = require('../jobflow/browser_worker/background.js');

function fixture(options = {}) {
  let sent = 0;
  class Control {
    constructor(tag, properties = {}) {
      this.tagName=tag; this.type='text'; this.name=''; this.id=''; this.disabled=false;
      this.hidden=false; this.readOnly=false; this.required=false; this.checked=false;
      this.attributes={}; this.labels=[]; this.files=[]; this.events=[]; this._value='';
      this.parentElement=null; this.computedStyle={}; this.width=300; this.height=100;
      Object.assign(this,properties);
      if (properties.label) this.labels=[{innerText:properties.label,textContent:properties.label}];
    }
    get value() { return this._value; }
    set value(value) { this._value=String(value); }
    getAttribute(name) { return this.attributes[name] || (['src','title'].includes(name) ? this[name] : null) || null; }
    getClientRects() { return this.hidden ? [] : [this.getBoundingClientRect()]; }
    getBoundingClientRect() { return {x:0,y:0,top:0,left:0,width:this.width,height:this.height,right:this.width,bottom:this.height}; }
    closest() { return null; }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    dispatchEvent(event) { this.events.push(event.type); return true; }
    checkValidity() { return !this.required || this.type==='file' ? !this.required || this.files.length>0 : this.type==='checkbox' ? this.checked : Boolean(this.value); }
    click() { this.checked=!this.checked; this.events.push('click'); }
  }
  class Input extends Control { constructor(props) { super('INPUT',props); } get value(){return this._value;} set value(value){this._value=String(value);} }
  class Textarea extends Control { constructor(props) { super('TEXTAREA',props); } get value(){return this._value;} set value(value){this._value=String(value);} }
  class Select extends Control { constructor(props) { super('SELECT',props); } get value(){return this._value;} set value(value){this._value=String(value);} get selectedOptions(){return this.options.filter(x=>x.value===this.value);} }
  const email = new Input({type:'email',name:'email',label:'Email',required:true});
  const name = new Input({name:'name',label:'Full name',required:true});
  const consent = new Input({type:'checkbox',name:'consent',label:'I consent to processing',required:true});
  const file = new Input({type:'file',name:'resume',label:'Resume',required:true});
  const controls = [email,name,consent,file];
  const submit = new Control('BUTTON',{innerText:'Submit application'});
  const success = new Control('DIV',{innerText:'Your application has been submitted.',hidden:true});
  const form = new Control('FORM');
  function query(selector) {
    if (selector === 'button,input[type="submit"]') return [submit];
    if (selector === 'input,textarea,select' || selector.startsWith('input,textarea,select,')) return controls;
    return [];
  }
  form.querySelectorAll=query;
  const body = new Control('BODY'); body.querySelectorAll=query;
  const frames=[];
  function addFrame(properties, ancestor = {}) {
    const container=new Control('DIV',{parentElement:body,...ancestor});
    const wrapper=new Control('DIV',{parentElement:container});
    const frame=new Control('IFRAME',{parentElement:wrapper,...properties});
    frames.push(frame);
    return frame;
  }
  for (const frame of options.frames || []) addFrame(frame,options.frameAncestor);
  const verifications=(options.verifications || []).map(node=>new Control(node.tagName || 'H1',node));
  submit.click = () => { sent++; form.hidden=true; submit.hidden=true; success.hidden=false; };
  const document = {body,getElementById:()=>null,querySelectorAll(selector) {
    if (selector==='form') return [form];
    if (selector==='input[type="file"]') return [file];
    if (selector==='iframe') return frames;
    if (selector==='[role="dialog"],main,h1,h2') return verifications;
    if (selector.startsWith('[role="status"]')) return [success];
    return [];
  }};
  global.document=document;
  global.location={href:'https://jobs.example.com/job/123',origin:'https://jobs.example.com'};
  global.getComputedStyle=node=>({visibility:'visible',display:'block',opacity:'1',...node.computedStyle});
  global.HTMLInputElement=Input; global.HTMLTextAreaElement=Textarea; global.HTMLSelectElement=Select;
  if (!global.crypto) Object.defineProperty(global, "crypto", {value:webcrypto}); global.TextEncoder=TextEncoder;
  global.Event=class {constructor(type){this.type=type;}};
  global.DataTransfer=class {constructor(){this.files=[];this.items={add:file=>this.files.push(file)};}};
  global.File=class {constructor(parts,name,opts){this.parts=parts;this.name=name;this.type=opts.type;}};
  return {email,name,consent,file,controls,form,submit,success,frames,addFrame,sent:()=>sent};
}
const field = (type,required=false,options=[]) => ({key:'field',label:'Question',type,required,supported:true,options});

async function processFixture({rejectSending=false,revokeBeforeClick=false,confirmation=false,stopBeforeFill=false} = {}) {
  const task={batch_id:'b',job_id:'j',attempt_id:'a',mode:'submit',url:'https://jobs.example.com/job/1',company:'Example',form_signature:'reviewed'};
  let state={worker_id:'worker',enabled:true,connection:{tabId:1,origin:'http://localhost:8765',workspace_id:'w'}};
  const operations=[],reports=[];
  const chrome={storage:{local:{async get(){return {jobflowWorker:state};},async set(value){state=value.jobflowWorker;}}},permissions:{contains:async()=>true},tabs:{create:async()=>({id:2}),get:async()=>({id:2,url:task.url,status:'complete'})},alarms:{clear:async()=>true}};
  const context=vm.createContext({module:{exports:{}},URL,crypto:webcrypto,chrome,console,setTimeout:fn=>{fn();return 0;},clearTimeout(){}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../jobflow/browser_worker/background.js'),'utf8'),context);
  context.fixture={task,rejectSending,revokeBeforeClick,confirmation,stopBeforeFill,operations,reports};
  vm.runInContext(`
    loaded = async () => {};
    api = async (connection,payload) => {
      if (payload) {
        fixture.reports.push(payload.status);
        if (payload.status === 'submitting' && fixture.rejectSending) throw new Error('Checkpoint failed');
        return {ok:true};
      }
      return {batches:[{id:'b',status:fixture.revokeBeforeClick?'paused':'active',items:[{job_id:'j',status:fixture.reports.includes('submitting')?'submitting':'running'}]}]};
    };
    runForm = async (id,operation) => {fixture.operations.push(operation);return operation==='confirmation'?{confirmed:fixture.operations.includes('clickSubmit')&&fixture.confirmation,url:fixture.task.url,text:'Your application has been submitted.'}:{ok:true};};
  `,context);
  await vm.runInContext('processTask({worker_id:"worker",connection:{tabId:1,origin:"http://localhost:8765",workspace_id:"w"}},fixture.task)',context);
  return {operations,reports,state};
}

async function reconnectFixture() {
  let state={worker_id:'worker',enabled:false,connection:{tabId:1,origin:'http://localhost:8765',workspace_id:'original'},active:{batch_id:'b',job_id:'j',attempt_id:'a',mode:'submit',tabId:3,url:'https://jobs.example.com/a',phase:'click_pending',baseline_receipts:[]}};
  let targetURL='http://localhost:8765/#batches';
  const calls=[];
  const chrome={storage:{local:{async get(){return {jobflowWorker:state};},async set(value){state=value.jobflowWorker;}}},tabs:{get:async id=>({id,url:targetURL})},alarms:{create:async()=>{},clear:async()=>{}},runtime:{getManifest:()=>({version:'0.1.2'})}};
  const context=vm.createContext({module:{exports:{}},URL,crypto:webcrypto,chrome,console});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../jobflow/browser_worker/background.js'),'utf8'),context);
  context.fixture={calls};
  vm.runInContext(`api=async(connection,payload)=>{fixture.calls.push({connection,payload});return {workspace_id:'original',batches:[{id:'b',status:'active',items:[{job_id:'j',status:'uncertain'}]}]};};runForm=async()=>({confirmed:false});`,context);
  await vm.runInContext('command({op:"connect",tabId:2})',context);
  assert.equal(state.connection.tabId,2,'Interrupted attempt may reconnect a replacement Jobflow tab');
  assert.equal(calls[0].connection.workspace_id,'original','Reconnection binds to the same workspace identity');
  assert.equal(state.active.attempt_id,'a','Reconnect preserves unresolved attempt');
  targetURL='http://localhost:8766/#batches';
  await assert.rejects(()=>vm.runInContext('command({op:"connect",tabId:4})',context),/same Jobflow origin/);
  await vm.runInContext('(async()=>recover(await readState()))()',context);
  assert.equal(state.active,null,'Already-recorded uncertainty clears local checkpoint without replay');
  assert.ok(calls.every(call=>!call.payload || call.payload.op==='worker_heartbeat'),'Recovery never re-submits or redundantly reports already-recorded uncertainty');
}

function controlFixture(options = {}) {
  const clone=value=>JSON.parse(JSON.stringify(value));
  let state=clone(options.state || {worker_id:'worker',enabled:false,
    connection:{tabId:1,origin:'http://localhost:8765',workspace_id:'original'},
    taskTabs:{'batch:job':3},lastTabId:3});
  let version=options.version || '0.1.2', reloads=0, context;
  const requests=[],events=[],alarms=new Map(),listeners={};
  const server={workspace:'original',request:clone(options.request || null),batches:[],failHeartbeat:false,failAck:false,failCompletion:false};
  const tabs=new Map([[1,'http://localhost:8765/#batches'],[2,'http://localhost:8765/#batches'],[3,'https://jobs.example.com/job/1']]);
  const event=name=>({addListener(listener){listeners[name]=listener;}});
  const chrome={
    storage:{local:{
      async get(){return state ? {jobflowWorker:clone(state)} : {};},
      async set(value){state=clone(value.jobflowWorker);events.push({type:'save',state:clone(state)});},
      async remove(){state=null;},async setAccessLevel(){},
    }},
    alarms:{async create(name,value){alarms.set(name,value);},async clear(name){alarms.delete(name);},onAlarm:event('alarm')},
    tabs:{async get(id){if(!tabs.has(id))throw new Error('Tab closed');return {id,url:tabs.get(id),status:'complete'};}},
    runtime:{id:'worker-extension',getURL:suffix=>'chrome-extension://worker-extension/'+suffix,
      getManifest:()=>({version}),onMessage:event('message'),onStartup:event('startup'),
      reload(){reloads++;events.push({type:'reload',state:clone(state),request:clone(server.request)});},
    },
    scripting:{async executeScript({target,func,args,files}){
      if(files)return [];
      context.location={origin:new URL(tabs.get(target.tabId)).origin};
      return [{result:await func(...args)}];
    }},
  };
  async function fetch(url,init) {
    if(url==='/api/batches')return {ok:true,json:async()=>({workspace_id:server.workspace,csrf_token:'csrf-fixture',batches:clone(server.batches)})};
    assert.equal(url,'/api/action');
    const payload=JSON.parse(init.body);
    requests.push(clone(payload));events.push({type:'request',payload:clone(payload)});
    assert.equal(init.credentials,'same-origin');
    assert.equal(init.headers['X-CSRF-Token'],'csrf-fixture');
    assert.equal(payload.workspace_id,server.workspace);
    let body;
    if(payload.op==='worker_heartbeat') {
      if(server.heartbeatGate)await server.heartbeatGate;
      if(server.failHeartbeat)throw new Error('Heartbeat unavailable');
      server.worker=clone(payload);
      if(payload.reload_completed_id) {
        assert.equal(payload.reload_completed_id,server.request.id);
        if(server.request.status==='reloading')server.request.status=payload.version===server.request.target_version?'complete':'outdated';
        if(server.failCompletion)throw new Error('Completion response lost');
      }
      if(payload.reload_failed_id) {
        assert.equal(payload.reload_failed_id,server.request.id);
        if(['pending','reloading'].includes(server.request.status))server.request.status='failed';
      }
      body={ok:true,worker:server.worker,worker_release:{version:'0.1.2'},reload_request:clone(server.request)};
    } else if(payload.op==='worker_reload_ack') {
      assert.equal(server.worker.active,false,'Reload ACK requires a preceding idle heartbeat');
      assert.equal(payload.request_id,server.request.id);
      if(server.failAck==='reject')return {ok:false,json:async()=>({error:'Worker has an active application.'})};
      server.request.status='reloading';
      if(server.failAck)throw new Error('ACK response lost');
      body={ok:true,worker:server.worker,worker_release:{version:'0.1.2'},reload_request:clone(server.request)};
    } else if(payload.op==='claim') body={ok:true,task:null};
    else throw new Error('Unexpected application mutation: '+payload.op);
    return {ok:true,json:async()=>body};
  }
  function boot() {
    context=vm.createContext({module:{exports:{}},URL,crypto:webcrypto,chrome,console,fetch,
      document:{querySelector:()=>({})},setTimeout,clearTimeout});
    vm.runInContext(fs.readFileSync(path.join(__dirname,'../jobflow/browser_worker/background.js'),'utf8'),context);
  }
  boot();
  return {server,requests,events,alarms,listeners,tabs,boot,
    run:code=>vm.runInContext(code,context),get state(){return state;},get reloads(){return reloads;},set version(value){version=value;},
    async flush(){for(let index=0;index<4;index++)await new Promise(setImmediate);},
  };
}

async function controlTests() {
  const pending=(overrides={})=>({id:'reload-1',worker_id:'worker',status:'pending',target_version:'0.1.2',
    requested_at:new Date().toISOString(),expires_at:new Date(Date.now()+300000).toISOString(),...overrides});
  const stopped=controlFixture({request:pending()});
  const original=JSON.parse(JSON.stringify(stopped.state));
  await stopped.run('initializeControl()');
  assert.equal(stopped.reloads,1,'Stopped connected worker polls and performs the requested reload');
  assert.equal(stopped.alarms.get('jobflow-worker-control').periodInMinutes,0.5);
  assert.equal(stopped.alarms.has('jobflow-next-application'),false);
  assert.deepEqual(stopped.requests.map(row=>row.op),['worker_heartbeat','worker_reload_ack']);
  assert.equal(stopped.requests[0].enabled,false);
  assert.equal(stopped.requests[0].reload_supported,true);
  assert.equal(stopped.state.enabled,false,'Reload never implicitly starts applications');
  assert.deepEqual(stopped.state.connection,original.connection);
  assert.deepEqual(stopped.state.taskTabs,original.taskTabs);
  assert.equal(stopped.state.lastTabId,original.lastTabId);
  await assert.rejects(()=>stopped.run('command({op:"disconnect"})'),/finishing a reload/);
  assert.deepEqual(stopped.state.connection,original.connection,'A pending runtime reload cannot discard the completion checkpoint');
  const reloadEvent=stopped.events.find(event=>event.type==='reload');
  assert.equal(reloadEvent.state.reload_marker.phase,'reload_invoked','One-shot marker is durable before runtime.reload');
  assert.equal(reloadEvent.request.status,'reloading','Server acknowledgement precedes runtime.reload');
  await stopped.run('pollControl();');
  assert.equal(stopped.reloads,1,'Old runtime cannot reload the same request twice');
  assert.ok(!stopped.requests.some(row=>row.reload_completed_id),'Old runtime does not report completion before reload');
  stopped.boot();
  await stopped.run('initializeControl()');
  assert.equal(stopped.server.request.status,'complete');
  assert.equal(stopped.state.reload_marker.phase,'settled');
  assert.equal(stopped.state.enabled,false);
  await stopped.run('pollControl()');
  assert.equal(stopped.reloads,1,'Completed request never causes another reload');

  const outdated=controlFixture({request:pending({target_version:'0.1.3'})});
  await outdated.run('pollControl()');outdated.boot();
  await outdated.run('initializeControl()');
  assert.equal(outdated.server.request.status,'outdated');
  assert.equal(outdated.state.enabled,false);
  await outdated.run('pollControl()');outdated.boot();await outdated.run('initializeControl()');
  assert.equal(outdated.reloads,1,'Version mismatch does not loop or retry reload');

  for(const failure of ['reject','lost']) {
    const rejected=controlFixture({request:pending()});rejected.server.failAck=failure;
    await rejected.run('pollControl()');
    assert.equal(rejected.reloads,0,'Rejected or lost ACK must not reload');
    assert.equal(rejected.state.enabled,false);
    await rejected.run('pollControl()');rejected.boot();await rejected.run('initializeControl()');
    assert.equal(rejected.requests.filter(row=>row.op==='worker_reload_ack').length,1,'A failed ACK is not automatically retried across worker lifetimes');
    assert.equal(rejected.reloads,0);
    assert.equal(rejected.server.request.status,'failed','Failed acknowledgement is reported so the user can retry immediately');
    rejected.server.failAck=false;rejected.server.request=pending({id:'explicit-retry'});
    await rejected.run('pollControl()');
    assert.equal(rejected.reloads,1,'A fresh explicit request may retry after failure');
  }
  const lostCompletion=controlFixture({request:pending()});await lostCompletion.run('pollControl()');lostCompletion.boot();
  lostCompletion.server.failCompletion=true;await lostCompletion.run('initializeControl()');
  assert.equal(lostCompletion.state.reload_marker.phase,'reload_invoked');
  lostCompletion.server.failCompletion=false;await lostCompletion.run('pollControl()');
  assert.equal(lostCompletion.state.reload_marker.phase,'settled','Lost completion heartbeat is retried idempotently');
  assert.equal(lostCompletion.reloads,1);

  const replaced=controlFixture({request:pending()});await replaced.run('pollControl()');replaced.boot();
  replaced.server.request=pending({id:'new-request-after-expiry'});
  await replaced.run('initializeControl()');
  assert.equal(replaced.reloads,2,'An expired and replaced request does not strand the old completion marker');
  assert.ok(!replaced.requests.some(row=>row.reload_completed_id==='reload-1'),'Current request is checked before reporting a stale completion');
  const replacedCompletion=controlFixture({request:pending()});await replacedCompletion.run('pollControl()');replacedCompletion.boot();
  replacedCompletion.server.failCompletion=true;await replacedCompletion.run('initializeControl()');
  const priorCompletions=replacedCompletion.requests.filter(row=>row.reload_completed_id).length;
  replacedCompletion.server.failCompletion=false;replacedCompletion.server.request=pending({id:'new-request-after-lost-completion'});
  await replacedCompletion.run('pollControl()');
  assert.equal(replacedCompletion.reloads,2);
  assert.equal(replacedCompletion.requests.filter(row=>row.reload_completed_id).length,priorCompletions,'Lost completion is not replayed against a newer request');
  const interruptedAck=controlFixture({request:pending()});
  interruptedAck.state.reload_marker={request_id:'reload-1',worker_id:'worker',origin:'http://localhost:8765',workspace_id:'original',
    target_version:'0.1.2',phase:'ack_pending',runtime_instance:'previous-runtime'};
  await interruptedAck.run('initializeControl()');
  assert.equal(interruptedAck.server.request.status,'failed','A crash around ACK is reconciled as a failure without replaying it');
  assert.equal(interruptedAck.reloads,0);
  assert.ok(!interruptedAck.requests.some(row=>row.op==='worker_reload_ack'));

  const activeCheckpoint={batch_id:'b',job_id:'j',attempt_id:'a',mode:'submit',tabId:3,url:'https://jobs.example.com/job/1',phase:'click_pending',baseline_receipts:[]};
  const active=controlFixture({request:pending()});active.state.active=JSON.parse(JSON.stringify(activeCheckpoint));
  await active.run('initializeControl()');
  assert.equal(active.reloads,0,'A retained application checkpoint defers reload');
  assert.equal(active.server.worker.active,true);
  assert.deepEqual(active.state.active,activeCheckpoint,'Control polling preserves the interrupted application');
  active.server.batches=[{id:'b',status:'active',items:[{job_id:'j',worker_id:'worker',status:'uncertain'}]}];
  await active.run('command({op:"start"})');await active.flush();
  assert.equal(active.state.active,null,'Explicit Start can reconcile a retained checkpoint while reload is pending');
  assert.ok(!active.requests.some(row=>row.op==='claim'),'Reconciliation does not claim a new application ahead of reload');
  await active.run('pollControl()');
  assert.equal(active.reloads,1,'Reload proceeds after explicit checkpoint reconciliation');
  assert.equal(active.state.enabled,false);

  const busyWorker=controlFixture({request:pending()});
  await busyWorker.run('busy=true; pollControl()');
  assert.equal(busyWorker.reloads,0,'An in-flight browser task defers reload');
  assert.equal(busyWorker.server.worker.active,true);
  assert.equal(busyWorker.state.reload_marker,undefined);
  await busyWorker.run('busy=false; pollControl()');
  assert.equal(busyWorker.reloads,1);

  const priority=controlFixture({request:pending()});priority.state.enabled=true;
  await priority.run('pump()');
  assert.equal(priority.reloads,1);
  assert.ok(!priority.requests.some(row=>row.op==='claim'),'A pending reload takes precedence over new claims');
  const outage=controlFixture();outage.state.enabled=true;outage.server.failHeartbeat=true;
  await outage.run('pump()');
  assert.equal(outage.state.enabled,true,'A transient heartbeat failure preserves the explicit Start choice');
  assert.ok(!outage.requests.some(row=>row.op==='claim'));
  outage.server.failHeartbeat=false;await outage.run('pump()');
  assert.equal(outage.requests.filter(row=>row.op==='claim').length,1,'Polling resumes normal claims once the connection returns');

  for(const request of [pending({worker_id:'different-worker'}),pending({expires_at:new Date(Date.now()-1000).toISOString()})]) {
    const ignored=controlFixture({request});await ignored.run('pollControl()');
    assert.equal(ignored.reloads,0,'Other workers and expired requests cannot reload this extension');
    assert.ok(!ignored.requests.some(row=>row.op==='worker_reload_ack'));
  }
  const wrongWorkspace=controlFixture({request:pending()});wrongWorkspace.server.workspace='different-account';
  await wrongWorkspace.run('pollControl()');
  assert.equal(wrongWorkspace.reloads,0);
  assert.equal(wrongWorkspace.requests.length,0,'Changed workspace is rejected before sending control mutations');
  const wrongOrigin=controlFixture({request:pending()});wrongOrigin.tabs.set(1,'https://unrelated.example.com/');
  await wrongOrigin.run('pollControl()');
  assert.equal(wrongOrigin.requests.length,0,'Changed connected-tab origin cannot receive control requests');

  const serialized=controlFixture();let releaseHeartbeat;
  serialized.server.heartbeatGate=new Promise(resolve=>{releaseHeartbeat=resolve;});
  const polling=serialized.run('pollControl()');await serialized.flush();
  const duplicatePoll=serialized.run('pollControl()');
  const blockedPump=serialized.run('pump()');
  const disconnecting=serialized.run('command({op:"disconnect"})');
  await serialized.flush();
  assert.ok(serialized.state.connection,'Disconnect waits for the active control request');
  releaseHeartbeat();await Promise.all([polling,duplicatePoll,blockedPump,disconnecting]);
  assert.equal(serialized.requests.filter(row=>row.op==='worker_heartbeat').length,1,'Concurrent control polls are serialized');
  assert.ok(!serialized.requests.some(row=>row.op==='claim'),'The application pump cannot race an active control poll');
  assert.equal(serialized.state,null);

  const lifecycle=controlFixture();
  await lifecycle.run('bootstrap()');await lifecycle.flush();
  assert.ok(lifecycle.alarms.has('jobflow-worker-control'),'Bootstrap restores the control alarm while stopped');
  assert.equal(lifecycle.listeners.message({op:'start'},{id:'worker-extension',tab:{id:1},url:'http://localhost:8765/'},()=>{throw new Error('Website message should not be accepted');}),false,'Website tabs cannot use the extension command channel');
  lifecycle.server.request=pending();lifecycle.listeners.alarm({name:'jobflow-worker-control'});await lifecycle.flush();
  assert.equal(lifecycle.reloads,1,'Control alarm handles website requests without a work alarm');
  lifecycle.boot();await lifecycle.run('initializeControl()');
  await lifecycle.run('command({op:"disconnect"})');
  assert.equal(lifecycle.alarms.size,0,'Disconnect clears both alarms');
  assert.equal(lifecycle.state,null);
  const startup=controlFixture();startup.state.enabled=true;startup.state.active=activeCheckpoint;
  await startup.run('bootstrap()');await startup.flush();startup.listeners.startup();await startup.flush();
  assert.equal(startup.state.enabled,false,'Browser startup does not implicitly resume submissions');
  assert.deepEqual(startup.state.active,activeCheckpoint);
  assert.ok(startup.alarms.has('jobflow-worker-control'));
  assert.ok(!startup.requests.some(row=>row.op==='claim'));
}

async function main() {
  assert.equal(worker.isPublicHttps('https://jobs.lever.co/acme/123'),true);
  for (const url of ['http://jobs.example.com/1','https://127.0.0.1/','https://[::1]/','https://corp.internal/','https://server.local/','https://user:pass@jobs.example.com','https://jobs.example.com:8765/']) assert.equal(worker.isPublicHttps(url),false,url);
  assert.equal(worker.workspaceURL('http://127.0.0.1:8765'),true);
  assert.equal(worker.workspaceURL('http://evil.example.com'),false);
  assert.equal(worker.sameApplicationURL('https://jobs.example.com/a','https://jobs.example.com/a/apply'),true);
  assert.equal(worker.sameApplicationURL('https://jobs.example.com/a','https://jobs.example.com/b'),false);
  assert.equal(worker.sameApplicationURL('https://jobs.example.com/?gh_jid=1','https://jobs.example.com/?gh_jid=2'),false);
  assert.equal(worker.sameApplicationURL('https://jobs.example.com/a','https://other.example.com/a'),false);
  const checkpoint=worker.safeActive({batch_id:'b',job_id:'j',attempt_id:'a',mode:'submit',url:'https://jobs.example.com/a',applicant:{email:'private@example.com'},resume:{content_base64:'secret'},planned_answers:{secret:'answer'}},5,'filled');
  assert.ok(!JSON.stringify(checkpoint).includes('private'));
  assert.ok(!JSON.stringify(checkpoint).includes('secret'));
  assert.equal(forms.planValue({...field('text'),label:'Full name'},{first_name:'Ada',last_name:'Lovelace'}),'Ada Lovelace');
  assert.equal(forms.planValue({...field('text'),label:'Are you authorized to work in the US?'},{email:'a@b.com'}),'');
  assert.equal(forms.planValue({...field('checkbox',true),label:'I agree'},{}),false);
  assert.equal(forms.planValue({...field('text'),label:'Expected salary'}, {}, {'Expected salary':'100000'}),'100000');
  assert.equal(forms.answerIsValid(field('checkbox',true),false),false);
  assert.equal(forms.answerIsValid(field('checkbox',false),false),true);
  assert.equal(forms.answerIsValid(field('select',true,['Yes','No']),'Maybe'),false);
  assert.equal(forms.answerIsValid(field('select',true,['Yes','No']),'Yes'),true);
  assert.equal(forms.answerIsValid({...field('text'),supported:false},''),false);
  assert.equal(forms.confirmationText('Once you submit your application has been submitted.'),'');
  assert.ok(forms.confirmationText('Your application has been received.'));
  assert.equal(forms.confirmationText("Thank you for applying. We couldn't submit your application. Try again."),'');

  const dom=fixture();
  const bytes=Buffer.from('%PDF-test-fixture');
  const sha256=Buffer.from(await webcrypto.subtle.digest('SHA-256',bytes)).toString('hex');
  const task={attempt_id:'a',url:'https://jobs.example.com/job/123',applicant:{first_name:'Ada',last_name:'Lovelace',email:'ada@example.com'},answers:{'I consent to processing':true},resume:{filename:'resume.pdf',mime:'application/pdf',size:bytes.length,sha256,content_base64:bytes.toString('base64')}};
  const inspected=await forms.inspect(task);
  assert.equal(dom.email.value,'','Inspection does not fill employer controls');
  assert.equal(dom.file.files.length,0,'Inspection does not upload resumes');
  assert.equal(dom.sent(),0);
  assert.equal(inspected.fields.length,4);
  task.fields=inspected.fields; task.form_signature=inspected.form_signature; task.planned_answers=inspected.planned_answers;
  await forms.fill(task);
  assert.equal(dom.email.value,'ada@example.com');
  assert.equal(dom.file.files[0].name,'resume.pdf');
  assert.equal(dom.consent.checked,true);
  assert.equal(dom.sent(),0,'Filling does not click submit');
  dom.success.hidden=false;
  assert.equal(forms.confirmation().confirmed,false,'Success-looking text beside an active application form is not a receipt');
  dom.submit.disabled=true;
  assert.equal(forms.confirmation().confirmed,false,'A disabled but visible submit button still means the form remains');
  const baseline=forms.confirmation().receipts;
  dom.submit.hidden=true;
  assert.equal(forms.confirmation({baseline_receipts:baseline}).confirmed,false,'Preexisting footer text cannot become proof after a form disappears');
  dom.submit.hidden=false; dom.submit.disabled=false; dom.success.hidden=true;
  global.location.href='https://jobs.example.com/job/999';
  await assert.rejects(()=>forms.clickSubmit(task),/different application/);
  assert.equal(dom.sent(),0,'Same-origin navigation to a different role after filling must not submit');
  global.location.href=task.url+'?gh_jid=999';
  await assert.rejects(()=>forms.clickSubmit(task),/different application/);
  assert.equal(dom.sent(),0,'Changed query posting identifiers must not submit');
  global.location.href=task.url;
  await forms.clickSubmit(task);
  assert.equal(dom.sent(),1);
  assert.equal(forms.confirmation().confirmed,true);
  await assert.rejects(()=>forms.clickSubmit(task),/single submission click/);
  assert.equal(dom.sent(),1,'Second submit click is forbidden');

  const passiveFrames=[
    {src:'https://www.google.com/recaptcha/enterprise/anchor?size=invisible',title:'reCAPTCHA'},
    {src:'https://www.google.com/recaptcha/api2/anchor?k=public-site-key',title:'reCAPTCHA'},
    {src:'https://newassets.hcaptcha.com/captcha/v1/static/hcaptcha.html#frame=checkbox',title:'Widget containing checkbox for hCaptcha security challenge'},
    {src:'https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile/if/ov2/widget',title:'Widget containing a Cloudflare security challenge'},
    {src:'https://jobs.example.com/help?provider=recaptcha&frame=https://www.google.com/recaptcha/api2/bframe',title:'Employer help'},
  ];
  for (const frame of passiveFrames) {
    const passive=fixture({frames:[frame]});
    for (const name of ['g-recaptcha-response','h-captcha-response']) {
      const token=new HTMLTextAreaElement({name,hidden:true});
      Object.defineProperty(token,'value',{get(){throw new Error('Verification response tokens must never be read');}});
      passive.controls.push(token);
    }
    const review=await forms.inspect(task);
    assert.equal(review.fields.length,4,`Passive widget does not prevent form inspection: ${frame.src}`);
    assert.equal(passive.email.value,'','Inspecting beside a widget does not fill application fields');
    assert.equal(passive.file.files.length,0,'Inspecting beside a widget does not upload a resume');
    assert.equal(passive.sent(),0);
    const approved={...task,...review};
    await forms.fill(approved);
    assert.equal(passive.email.value,'ada@example.com','Approved fields can be filled beside a passive widget');
    assert.equal(passive.file.files[0].name,'resume.pdf');
    assert.equal(passive.sent(),0,'Filling beside a passive widget does not submit');
    await forms.clickSubmit(approved);
    assert.equal(passive.sent(),1,'A passive widget permits the single authorized submit click');
  }

  const activeFrames=[
    {src:'https://www.google.com/recaptcha/api2/bframe?k=public-site-key'},
    {src:'https://www.recaptcha.net/recaptcha/enterprise/bframe?k=public-site-key'},
    {src:'https://www.google.com/recaptcha/widget',title:'recaptcha challenge expires in two minutes'},
    {src:'https://newassets.hcaptcha.com/captcha/v1/static/hcaptcha.html#frame=challenge'},
    {src:'https://newassets.hcaptcha.com/captcha/v1/static/hcaptcha.html',title:'Main content of the hCaptcha challenge'},
  ];
  for (const frame of activeFrames) {
    const challenge=fixture({frames:[frame]});
    await assert.rejects(()=>forms.inspect(task),/challenge|verification/i,`Displayed challenge blocks inspection: ${frame.src}`);
    await assert.rejects(()=>forms.fill(task),/challenge|verification/i,'Displayed challenge blocks filling');
    assert.equal(challenge.email.value,'');
    assert.equal(challenge.file.files.length,0);
    assert.equal(challenge.sent(),0);
  }

  for (const ancestor of [
    {hidden:true},
    {computedStyle:{display:'none'}},
    {computedStyle:{visibility:'hidden'}},
    {computedStyle:{visibility:'collapse'}},
    {computedStyle:{opacity:'0'}},
  ]) {
    const hiddenChallenge=fixture({frames:[activeFrames[0]],frameAncestor:ancestor});
    const review=await forms.inspect(task);
    assert.equal(review.fields.length,4,`An invisible challenge container does not block inspection: ${JSON.stringify(ancestor)}`);
    await forms.fill({...task,...review});
    assert.equal(hiddenChallenge.email.value,'ada@example.com');
    assert.equal(hiddenChallenge.file.files[0].name,'resume.pdf');
    assert.equal(hiddenChallenge.sent(),0);
  }
  for (const dimensions of [{width:0},{height:0}]) {
    fixture({frames:[{...activeFrames[0],...dimensions}]});
    assert.equal((await forms.inspect(task)).fields.length,4,'A zero-size challenge frame is not displayed');
  }

  for (const node of [
    {tagName:'H1',innerText:'Verify you are human'},
    {tagName:'H2',innerText:'Security verification'},
    {tagName:'DIV',attributes:{role:'dialog'},innerText:'Just a moment'},
  ]) {
    const interstitial=fixture({verifications:[node]});
    await assert.rejects(()=>forms.inspect(task),/verification/i,'A verification screen still requires user interaction');
    await assert.rejects(()=>forms.fill(task),/verification/i);
    assert.equal(interstitial.file.files.length,0);
    assert.equal(interstitial.sent(),0);
  }

  const lateChallenge=fixture({frames:[passiveFrames[0]]});
  const lateReview=await forms.inspect(task);
  const lateApproved={...task,...lateReview};
  await forms.fill(lateApproved);
  lateChallenge.addFrame(activeFrames[0]);
  await assert.rejects(()=>forms.clickSubmit(lateApproved),/challenge|verification/i,'A challenge opening after filling prevents submission');
  assert.equal(lateChallenge.sent(),0,'No submit click is made while a late challenge is displayed');

  const changed=fixture();
  const reviewed=await forms.inspect(task);
  changed.controls.push(new HTMLInputElement({name:'new',label:'New required question',required:true}));
  await assert.rejects(()=>forms.fill({...task,form_signature:reviewed.form_signature}),/changed after review/);
  assert.equal(changed.email.value,'','Changed schema rejected before private values are filled');

  const greenhouse=fixture();
  greenhouse.email.type='text';
  greenhouse.file.name='';greenhouse.file.id='resume';greenhouse.file.required=false;
  greenhouse.file.labels=[{innerText:'Attach',textContent:'Attach'}];
  const greenhouseCover=new HTMLInputElement({type:'file',id:'cover_letter',label:'Attach'});
  greenhouse.controls.push(greenhouseCover);
  const uploadLabels={};
  global.document.getElementById=id=>uploadLabels[id] || null;
  function uploadGroup(picker,id,label) {
    uploadLabels[id]={innerText:label,textContent:label};
    const attributes={role:'group','aria-labelledby':id,'aria-required':'false'};
    const group={getAttribute:name=>attributes[name] || null,
      querySelectorAll:selector=>selector==='input[type="file"]'?[picker]:[],querySelector:()=>null};
    picker.closest=selector=>selector==='[role="group"],fieldset'?group:null;
    return {attributes,group};
  }
  const resumeGroup=uploadGroup(greenhouse.file,'upload-label-resume','Resume/CV');
  uploadGroup(greenhouseCover,'upload-label-cover_letter','Cover Letter');
  for(let index=0;index<10;index++)greenhouse.controls.push(new HTMLInputElement({
    required:true,attributes:{'aria-hidden':'true',tabindex:'-1'},computedStyle:{opacity:'0'},width:608,height:21,
  }));
  const greenhouseCombo=new HTMLInputElement({id:'country',label:'Country',required:true,attributes:{role:'combobox'}});
  greenhouse.controls.push(greenhouseCombo);
  const greenhouseReview=await forms.inspect(task);
  assert.equal(greenhouseReview.fields.length,6,'Transparent React-select validation helpers are not separate questions');
  assert.equal(greenhouseReview.fields.find(item=>item.label==='Resume/CV').required,false,'Explicit optional upload group remains optional');
  assert.equal(greenhouseReview.planned_answers[greenhouseReview.fields.find(item=>item.label==='Resume/CV').key],'resume.pdf');
  assert.equal(greenhouseReview.planned_answers[greenhouseReview.fields.find(item=>item.label==='Cover Letter').key],'');
  assert.equal(greenhouseReview.planned_answers[greenhouseReview.fields.find(item=>item.label==='Email').key],'ada@example.com','Text-typed Email fields retain applicant mapping');
  assert.equal(greenhouseReview.fields.find(item=>item.label==='Country').supported,false,'The real custom combobox remains in the review');
  assert.equal(greenhouse.file.files.length,0,'Form discovery and inspection do not upload files');
  assert.equal(greenhouse.email.value,'');
  assert.equal(greenhouse.sent(),0);
  resumeGroup.attributes['aria-required']='true';
  assert.equal((await forms.inspect(task)).fields.find(item=>item.label==='Resume/CV').required,true,'Single-file group required semantics are retained');
  greenhouse.controls.splice(greenhouse.controls.indexOf(greenhouseCombo),1);
  const nativeUploadReview=await forms.inspect(task);
  await forms.fill({...task,...nativeUploadReview});
  assert.equal(greenhouse.file.files[0].name,'resume.pdf','The resume goes to its specifically named upload group');
  assert.equal(greenhouseCover.files.length,0,'An Attach button for optional Cover Letter never receives the resume');
  assert.equal(greenhouse.sent(),0);

  const meaningfulHidden=fixture();
  meaningfulHidden.controls.push(new HTMLInputElement({name:'work_authorization',label:'Work authorization',required:true,
    attributes:{'aria-hidden':'true',tabindex:'-1'},computedStyle:{opacity:'0'}}));
  const meaningfulReview=await forms.inspect(task);
  assert.ok(meaningfulReview.fields.some(item=>item.label==='Work authorization'&&item.required),'Named and labelled controls are not discarded as validation helpers');
  const opaqueHelper=fixture();
  opaqueHelper.controls.push(new HTMLInputElement({required:true,attributes:{'aria-hidden':'true',tabindex:'-1'}}));
  assert.ok((await forms.inspect(task)).fields.some(item=>item.label==='Unlabelled field'),'Visible unnamed controls remain explicit review items');

  const sharedGroup=fixture();
  sharedGroup.file.labels=[{innerText:'Attach',textContent:'Attach'}];
  const secondPicker=new HTMLInputElement({type:'file',name:'cover_letter',label:'Attach'});
  sharedGroup.controls.push(secondPicker);
  const ambiguousGroup={getAttribute:name=>name==='aria-label'?'Resume/CV':null,
    querySelectorAll:()=>[sharedGroup.file,secondPicker],querySelector:()=>null};
  sharedGroup.file.closest=secondPicker.closest=()=>ambiguousGroup;
  assert.equal((await forms.inspect(task)).fields.filter(item=>item.type==='file'&&item.label==='Attach').length,2,'A multi-file group cannot assign the same semantic label to every picker');

  const optional=fixture();
  const cover=new HTMLInputElement({type:'file',name:'cover',label:'Cover letter',required:false});
  optional.controls.push(cover);
  const optionalInspection=await forms.inspect(task);
  const coverField=optionalInspection.fields.find(f=>f.label==='Cover letter');
  assert.equal(coverField.supported,true);
  assert.equal(optionalInspection.planned_answers[coverField.key],'');
  await forms.fill({...task,...optionalInspection});
  assert.equal(cover.files.length,0,'Optional cover letter is omitted rather than receiving the resume');
  const custom=fixture(); custom.email.attributes.role='combobox';
  const customInspection=await forms.inspect(task);
  assert.equal(customInspection.fields.find(f=>f.label==='Email').supported,false,'A text input used as custom combobox requires manual selection');
  const before=fixture(); before.form.hidden=true; before.submit.hidden=true; before.success.hidden=false;
  await assert.rejects(()=>forms.fill(task),/already shows a confirmation/);

  const failed=await processFixture({rejectSending:true});
  assert.ok(!failed.operations.includes('clickSubmit'),'Failed durable checkpoint prevents any submit click');
  assert.ok(failed.reports.includes('uncertain'));
  const revoked=await processFixture({revokeBeforeClick:true});
  assert.ok(!revoked.operations.includes('clickSubmit'),'Paused batch prevents click after checkpoint');
  const successful=await processFixture({confirmation:true});
  assert.equal(successful.operations.filter(op=>op==='clickSubmit').length,1);
  assert.deepEqual(successful.reports,['submitting','submitted']);
  assert.equal(successful.state.active,null);
  const uncertain=await processFixture({confirmation:false});
  assert.equal(uncertain.operations.filter(op=>op==='clickSubmit').length,1);
  assert.equal(uncertain.reports.at(-1),'uncertain');
  await reconnectFixture();
  await controlTests();
  console.log('Browser worker tests passed: form inspection/fill, passive verification widgets, active and hidden challenges, schema changes, resume integrity, one-click submission, checkpoint failure, pause, confirmation, private checkpoint data, and safe worker reload lifecycle.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});

/* Independent writes stay usable while another save or workspace read is slow. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../jobflow/web_assets/app.js'),'utf8').split('// Bootstrap browser session.')[0];
const response=body=>({ok:true,status:200,json:async()=>body});
const fixture=()=>({today:'2026-11-15',csrf_token:'fixture',jobs:[],applications:[]});
const saved=id=>({updated:1,skipped:0,applications:[{id,job_id:id,stage:'saved'}]});
function deferred(){let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};}
function harness(){
  const nodes=new Map(),requests=[],stats={renders:0};
  let respond=async()=>response(fixture());
  const node=selector=>{
    if(!nodes.has(selector))nodes.set(selector,{innerHTML:'',textContent:'',open:false,
      addEventListener(){},setAttribute(){},querySelector(){return null;},querySelectorAll(){return [];},
      classList:{toggle(){},add(){},remove(){},contains(){return false;}}});
    return nodes.get(selector);
  };
  const context=vm.createContext({URL,URLSearchParams,Intl,Date,Map,Set,
    document:{querySelector:node,querySelectorAll:()=>[],addEventListener(){}},
    window:{addEventListener(){}},location:{hash:'#jobs',assign(){}},
    setTimeout(){},clearTimeout(){},fixture:fixture(),renderHook(){stats.renders++;},
    fetch(url,options={}){const payload=options.body?JSON.parse(options.body):null;requests.push({url,payload});return respond(url,payload);},
  });
  vm.runInContext(source,context);
  vm.runInContext('render=renderHook;setData(fixture);',context);
  return {requests,stats,nodes,context,evaluate:code=>vm.runInContext(code,context),respond(fn){respond=fn;},
    save(id){return vm.runInContext('mutate({action:"application",job_ids:['+JSON.stringify(id)+'],stage:"saved"})',context);}};
}
async function main(){
  {
    const app=harness(),first=deferred();
    app.respond(async(url,payload)=>payload.job_ids[0]==='one'?first.promise:response(saved('two')));
    const one=app.save('one');
    assert.equal(await app.save('one'),false,'Duplicate actions against the same record are deduplicated');
    assert.equal(await app.save('two'),true,'Another role saves while the first request is still pending');
    assert.equal(app.evaluate('appById("two").stage'),'saved');
    assert.equal(app.stats.renders,1,'The completed role becomes visible while another save is pending');
    assert.equal(app.evaluate('state.pendingActions.has("application:one")'),true);
    assert.equal(app.requests.length,2);
    first.resolve(response(saved('one')));assert.equal(await one,true);
    assert.equal(app.evaluate('state.pendingActions.size'),0);
    assert.equal(app.evaluate('applications().length'),2);
    assert.ok(app.requests.every(row=>row.url==='/api/action'),'Authoritative records remove the full-state reload after small writes');
  }
  {
    const app=harness(),read=deferred();
    app.respond(async url=>url==='/api/action'?response({updated:1}):read.promise);
    assert.equal(await app.save('legacy'),true,'A durable action resolves before a fallback workspace read finishes');
    assert.deepEqual(app.requests.map(row=>row.url),['/api/action','/api/state']);
    assert.equal(app.evaluate('state.pendingActions.size'),0);
    read.resolve(response(fixture()));await new Promise(resolve=>setImmediate(resolve));
  }
  {
    const app=harness(),oldRead=deferred(),newRead=deferred();let reads=0;
    app.respond(async url=>url==='/api/action'?response(saved('one')):(++reads===1?oldRead.promise:newRead.promise));
    const refresh=app.evaluate('refresh()');
    await app.save('one');
    oldRead.resolve(response(fixture()));await new Promise(resolve=>setImmediate(resolve));
    assert.equal(app.evaluate('appById("one").stage'),'saved','An older workspace response cannot overwrite a confirmed save');
    assert.equal(reads,2,'A superseded read is retried independently');
    newRead.resolve(response({...fixture(),applications:saved('one').applications}));await refresh;
    assert.equal(app.evaluate('appById("one").stage'),'saved');
  }
  {
    const app=harness();app.evaluate("$('#action-dialog').open=true");
    app.respond(async()=>response(saved('one')));
    assert.equal(await app.save('one'),true);
    assert.equal(app.stats.renders,0,'A confirmed record is merged without replacing an open form');
    assert.equal(app.evaluate('appById("one").stage'),'saved');
    app.evaluate("$('#action-dialog').open=false;flushWorkspaceUpdates()");
    assert.equal(app.stats.renders,1);
    assert.equal(app.requests.length,1);
  }
  {
    const app=harness();app.respond(async()=>({ok:false,status:409,json:async()=>({error:'This record changed. Review it before retrying.'})}));
    assert.equal(await app.save('one'),false);
    assert.equal(app.evaluate('applications().length'),0,'Failed writes never look durably saved');
    assert.equal(app.evaluate('state.pendingActions.size'),0,'Failure releases only the affected pending action');
    assert.match(app.nodes.get('#toast').textContent,/Review it/);
  }
  {
    const app=harness();
    app.evaluate('state.data.tasks=[{id:"keep",status:"done",notes:"Completed history"}];state.data.interviews=[{id:"old",round:"Recorded round"}];state.data.account={hosted:true}');
    app.respond(async(url,payload)=>response(payload.action==='task'
      ? {tasks:[{id:'task',status:'done',notes:'My saved note'}],background_job:{status:'pending',id:'derived'}}
      : {interviews:[{id:'interview',job_id:'one',round:'Technical'}]}));
    await app.evaluate('mutate({action:"task",id:"task",status:"done"})');
    await app.evaluate('mutate({action:"interview",job_id:"one",round:"Technical"})');
    assert.equal(app.evaluate('state.data.tasks.length'),2,'A changed task is merged without removing completion history');
    assert.equal(app.evaluate('state.data.tasks.find(row=>row.id==="task").notes'),'My saved note');
    assert.equal(app.evaluate('state.data.interviews.length'),2);
    assert.equal(app.evaluate('state.data.background_job.id'),'derived','Returned queue status starts the existing compact progress path');
    assert.ok(app.requests.every(row=>row.url==='/api/action'),'Task and interview acknowledgements need no full-state reload');
  }
  console.log('Interactive action concurrency UI checks passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});

/* Node standard-library regression: waiting for hosted work reads only its queue. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../jobflow/web_assets/app.js'), 'utf8')
  .split('// Bootstrap browser session.')[0];
const fixture = (status='pending', hosted=true) => ({
  today:'2026-11-14', account:{hosted}, csrf_token:'opaque-csrf',
  jobs:[{id:'existing-job',title:'Preserved role'}], applications:[],
  background_job:{status,message:'Updated.'},
});
function harness(initial=fixture()) {
  const nodes=new Map(), timers=new Map(), reads=[], redirects=[], stats={renders:0};
  let nextTimer=0, responder=async()=>({ok:true,status:200,json:async()=>({background_job:{status:'running'}})});
  const node=selector=>{
    if(!nodes.has(selector))nodes.set(selector,{innerHTML:'',textContent:'',hidden:false,
      addEventListener(){},setAttribute(){},close(){},querySelector(){return null;},querySelectorAll(){return [];},
      classList:{add(){},remove(){},toggle(){},contains(){return false;}}});
    return nodes.get(selector);
  };
  const context=vm.createContext({URL,URLSearchParams,Intl,Date,Map,Set,
    setTimeout(callback,delay){const id=++nextTimer;timers.set(id,{callback,delay});return id;},
    clearTimeout(id){timers.delete(id);},
    document:{querySelector:node,querySelectorAll:()=>[],addEventListener(){}},
    window:{addEventListener(){},scrollTo(){}},
    location:{hash:'#jobs',assign(url){redirects.push(url);}},
    fetch:async(url,options)=>{reads.push({url,options});return responder(url,options);},
    renderHook(){stats.renders++;}, fixture:initial,
  });
  vm.runInContext(source,context,{filename:'app.js'});
  vm.runInContext('render=renderHook; setData(fixture);',context);
  return {nodes,timers,reads,redirects,stats,context,
    respond(fn){responder=fn;},
    evaluate(code){return vm.runInContext(code,context);},
    async fire(delay){const row=[...timers].find(([,value])=>value.delay===delay);assert.ok(row,'Expected timer '+delay);timers.delete(row[0]);await row[1].callback();},
  };
}
async function main(){
  {
    const app=harness();
    await app.fire(15000);
    assert.deepEqual(app.reads.map(row=>row.url),['/api/background']);
    assert.equal(app.reads[0].options.cache,'no-store');
    assert.equal(app.stats.renders,0,'Pending work does not rerender or download the full workspace');
    assert.equal(app.evaluate('state.data.jobs[0].id'),'existing-job');
    assert.equal(app.evaluate('state.data.background_job.status'),'running');
    assert.equal([...app.timers.values()].filter(row=>row.delay===15000).length,1);
    await app.fire(15000);
    assert.deepEqual(app.reads.map(row=>row.url),['/api/background','/api/background']);
  }
  {
    for(const status of ['done','partial','failed']){
      const app=harness();
      app.respond(async url=>({ok:true,status:200,json:async()=>url==='/api/background'?{background_job:{status}}:fixture(status)}));
      await app.fire(15000);
      assert.deepEqual(app.reads.map(row=>row.url),['/api/background','/api/state']);
      assert.equal(app.stats.renders,1,'A terminal result refreshes the full workspace once');
      assert.equal(app.timers.size,0,'Completed work does not keep polling');
    }
  }
  {
    const app=harness();app.respond(async()=>({ok:false,status:503}));
    await app.fire(15000);
    assert.deepEqual(app.reads.map(row=>row.url),['/api/background']);
    assert.equal(app.stats.renders,0);
    assert.equal([...app.timers.values()].filter(row=>row.delay===30000).length,1,'Transient errors back off without downloading records');
  }
  {
    const app=harness();app.respond(async()=>({ok:false,status:401}));
    await app.fire(15000);
    assert.deepEqual(app.redirects,['/account']);
    assert.deepEqual(app.reads.map(row=>row.url),['/api/background']);
    assert.equal(app.timers.size,0,'Expired sessions do not keep polling');
  }
  {
    const app=harness(fixture('pending',false));
    assert.equal(app.timers.size,0,'Local mode does not start hosted queue polling');
    await app.evaluate('pollBackground()');
    assert.equal(app.reads.length,0);
  }
  {
    const app=harness(fixture('done'));
    app.respond(async()=>({ok:true,status:200,json:async()=>({background_job:{status:'pending',id:'new-run'}})}));
    await app.evaluate('startCloudSearch()');
    assert.deepEqual(app.reads.map(row=>row.url),['/api/search'],'Enqueuing a search does not fetch the workspace');
    assert.equal(app.reads[0].options.method,'POST');
    assert.equal(app.evaluate('state.data.background_job.id'),'new-run');
    assert.equal(app.stats.renders,0,'Queuing preserves the currently displayed editor');
    assert.equal([...app.timers.values()].filter(row=>row.delay===15000).length,1);
  }
  for (const editor of ['dialog','pdf','resumes','batches','plan']) {
    const app=harness();
    if(editor==='dialog')app.evaluate("$('#action-dialog').open=true");
    else if(editor==='pdf')app.evaluate("$('.pdf-review-dialog').open=true");
    else if(editor==='plan')app.evaluate('state.agentProposal={job_id:"reviewing"}');
    else app.evaluate('state.page='+JSON.stringify(editor));
    app.respond(async url=>({ok:true,status:200,json:async()=>url==='/api/background'?{background_job:{status:'done'}}:fixture('done')}));
    await app.fire(15000);
    assert.deepEqual(app.reads.map(row=>row.url),['/api/background'],editor+' defers the full refresh');
    assert.equal(app.stats.renders,0,'Completion never replaces an active editor');
    assert.equal(app.evaluate('state.workspaceRefreshPending'),true);
    assert.equal([...app.timers.values()].filter(row=>row.delay===15000).length,0,'Completed work stops polling even while edits are open');
    app.evaluate("$('#action-dialog').open=false;$('.pdf-review-dialog').open=false;state.page='jobs';state.agentProposal=null");
    await app.evaluate('flushWorkspaceUpdates()');
    assert.deepEqual(app.reads.map(row=>row.url),['/api/background','/api/state']);
    assert.equal(app.stats.renders,1,'Deferred data refreshes after the editor is left');
  }
  console.log('Background queue polling UI checks passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});

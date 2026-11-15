/* Basemap lifecycle regressions: node tests/test_maps_ui.js. No network or browser. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../jobflow/web_assets/maps.js'), 'utf8');
function harness({rendererFails = false} = {}) {
  const log = [], timers = new Map(), mutations = [], markers = [];
  let nextTimer = 0;
  const node = () => ({innerHTML:'',textContent:'',hidden:false,dataset:{},handlers:{},clientWidth:700,clientHeight:500,
    classList:{add(){},remove(){},toggle(){}},setAttribute(){},removeAttribute(){},contains(){return false;},
    addEventListener(type,fn){this.handlers[type]=fn;},removeEventListener(type){delete this.handlers[type];},
    querySelectorAll(){return [];},parentElement:null});
  const nodes = new Map();
  const host = node();
  host.querySelector = selector => {if(!nodes.has(selector))nodes.set(selector,node());return nodes.get(selector);};
  host.querySelector('.jf-map-selected').querySelector = selector => host.querySelector('.jf-map-selected ' + selector);
  host.querySelector('.jf-map-status').querySelector = () => node();
  const doc = {documentElement:{dataset:{theme:'light'}},activeElement:null};
  const glHandlers = {};
  const gl = {on(event,fn){glHandlers[event]=fn;},setStyle(url){log.push(['style',url]);},
    getLayer(id){return {type:({water:'fill',park:'fill',label_city:'symbol',background:'background'})[id]};},
    setPaintProperty(id,property,value){log.push(['paint',id,property,value]);}};
  const map = {layers:new Set(),zoom:10,center:[37.6,-122.1],
    setView(center,zoom){this.center=center;this.zoom=zoom;log.push(['view',center,zoom]);return this;},
    fitBounds(points,options){log.push(['fit',points,options]);return this;},getZoom(){return this.zoom;},getCenter(){return this.center;},
    on(){},hasLayer(layer){return this.layers.has(layer);},removeLayer(layer){this.layers.delete(layer);},
    invalidateSize(){log.push(['resize']);},remove(){log.push(['remove']);},
    latLngToLayerPoint(){return {distanceTo(){return 100;}};}};
  const layer = {addTo(target){target.layers.add(this);return this;},getMaplibreMap(){return gl;}};
  const control = {addTo(){}};
  const L = {map(){return map;},control:{zoom(){return control;},scale(){return control;}},
    layerGroup(){return {addTo(){return this;},clearLayers(){}};},
    divIcon(value){return value;},marker(center,options){const marker={center,options,handlers:{},addTo(){return this;},on(event,fn){this.handlers[event]=fn;},getElement(){return null;},bindTooltip(){},bindPopup(html){this.popup=html;}};markers.push(marker);return marker;},
    tileLayer(url){log.push(['raster',url]);return {on(){},addTo(target){target.layers.add(this);return this;}};}};
  const win = {document:doc,L,maplibregl:{setWorkerUrl(url){log.push(['worker',url]);}},
    MaplibreGLLeaflet:{maplibreGL(options){log.push(['vector',options.style]);if(rendererFails)throw new Error('WebGL unavailable');return layer;}},
    MutationObserver:class {constructor(fn){this.fn=fn;mutations.push(this);}observe(){}disconnect(){log.push(['disconnect-theme']);}}};
  const ctx = vm.createContext({window:win,document:doc,Intl,Map,Set,console,
    setTimeout(fn,ms){const id=++nextTimer;timers.set(id,{fn,ms});return id;},clearTimeout(id){timers.delete(id);}});
  vm.runInContext(source,ctx);
  return {api:win.JobflowMaps,doc,host,nodes,log,timers,mutations,glHandlers,markers,
    mount(jobs,settings={},callbacks={}){const html=win.JobflowMaps.render(jobs,true,'',settings);host.id=/id="([^"]+)"/.exec(html)[1];win.JobflowMaps.mount({querySelectorAll(){return [host];}},callbacks);return html;},
    click(selector,dataset){host.handlers.click({target:{closest(value){return value===selector ? {dataset} : null;}}});}};
}
const role = {id:'a',company:'Example',title:'Engineer',location:'San Francisco; Mountain View'};
{
  const h=harness(), before=JSON.stringify(role);
  const html=h.mount([role]);
  assert.match(html,/<strong>1<\/strong> mapped roles/,'Multi-city role deduplicated in total');
  assert.match(html,/OpenMapTiles/);
  assert.deepEqual(h.log.find(x=>x[0]==='worker'),['worker','/vendor/maplibre/maplibre-gl-csp-worker.js']);
  assert.deepEqual(h.log.find(x=>x[0]==='vector'),['vector','https://tiles.openfreemap.org/styles/positron']);
  h.glHandlers['style.load']();
  assert.deepEqual(h.log.find(x=>x[0]==='paint' && x[1]==='water'),['paint','water','fill-color','#c7e4f2']);
  assert.deepEqual(h.log.find(x=>x[0]==='paint' && x[1]==='park'),['paint','park','fill-color','#dfedd9']);
  h.glHandlers.load();
  const loadedPaintCount=h.log.filter(x=>x[0]==='paint').length;h.glHandlers.idle();
  assert.equal(h.log.filter(x=>x[0]==='paint').length,loadedPaintCount,'Idle does not repaint repeatedly');
  assert.equal(h.timers.size,0,'A successful vector load cancels fallback timeout');
  h.doc.documentElement.dataset.theme='dark';h.mutations[0].fn();
  assert.deepEqual(h.log.find(x=>x[0]==='style'),['style','https://tiles.openfreemap.org/styles/dark']);
  const paintCount=h.log.filter(x=>x[0]==='paint').length;h.glHandlers['style.load']();
  assert.equal(h.log.filter(x=>x[0]==='paint').length,paintCount,'Dark map keeps native palette');
  h.api.destroy();
  assert.ok(h.log.some(x=>x[0]==='remove'));
  assert.ok(h.log.some(x=>x[0]==='disconnect-theme'));
  assert.equal(JSON.stringify(role),before);
}
{
  const h=harness();h.mount([role]);h.glHandlers.load();
  assert.ok(h.log.some(x=>x[0]==='paint' && x[1]==='water'),'Load catches a missed style.load event');
  assert.equal(h.host.dataset.jfMapPalette,'light-refined');h.api.destroy();
}
{
  const h=harness({rendererFails:true});h.mount([role]);
  assert.ok(h.log.some(x=>x[0]==='raster'),'Missing WebGL uses actual raster fallback');
  assert.match(h.nodes.get('.jf-map-basemap-label').textContent,/Standard map.*unavailable/);
  assert.doesNotMatch(h.nodes.get('.jf-map-provider').innerHTML,/OpenMapTiles/,'Fallback attribution names its actual source');
  h.api.destroy();assert.equal(h.timers.size,0);
}
{
  const h=harness();h.mount([role]);
  const timeout=[...h.timers.values()].find(x=>x.ms===15000);
  assert.ok(timeout);timeout.fn();
  assert.ok(h.log.some(x=>x[0]==='raster'),'Unresponsive vector service has a bounded fallback');
  h.api.destroy();assert.equal(h.timers.size,0);
}
{
  const h=harness();const sf=h.api.cities.find(x=>x.name==='San Francisco');
  assert.equal(h.api.inCity({location:'South San Francisco'},sf),false);
  assert.equal(h.api.inCity({location:'San Francisco Bay Area'},sf),false);
  assert.equal(h.api.inCity({location:'Greater San Francisco'},sf),false);
  assert.equal(h.api.inCity({location:'San Francisco metropolitan area'},sf),false);
  assert.equal(h.api.inCity({location:'San Mateo County'},h.api.cities.find(x=>x.name==='San Mateo')),false);
  assert.equal(h.api.inCity({location:'Foster City, CA'},h.api.cities.find(x=>x.name==='Foster City')),true);
  assert.equal(h.api.inCity(role,sf),true);
  assert.match(h.api.render([],false),/No city-specific roles/);
}

// Office coordinates are separately sourced evidence, never inferred city centers.
const office = {id:'office-example',company:'Example',city:'San Francisco',address:'123 Example Street, San Francisco, CA',
  latitude:37.78,longitude:-122.41,precision:'address',relationship:'company_office',worksite_confirmed:false,
  source_url:'https://example.com/contact',coordinate_source_url:'https://geocoding.geo.census.gov/',observed_at:'2026-11-14'};
{
  const h=harness(), before=JSON.stringify(office);
  const html=h.mount([role],{locations:[office],mode:'offices'});
  assert.match(html,/<strong>1<\/strong> office pins/);
  assert.doesNotMatch(h.nodes.get('.jf-map-counts').innerHTML,/roles/,'Reference office counts never claim role assignments');
  assert.match(h.markers[0].options.title,/role assignment unconfirmed.*Approximate address pin/);
  assert.deepEqual(Array.from(h.markers[0].center),[office.latitude,office.longitude]);
  assert.match(h.nodes.get('.jf-map-city-list').innerHTML,/123 Example Street/);
  let selected;
  h.api.destroy();
  const second=harness();second.mount([role],{locations:[office]},{onOfficeSelect(value){selected=value;}});
  second.click('[data-jf-map-mode]',{jfMapMode:'offices'});
  assert.match(second.nodes.get('.jf-map-counts').innerHTML,/office pins/);
  second.click('[data-jf-map-office]',{jfMapOffice:office.id});
  assert.equal(selected.id,office.id);
  second.click('[data-jf-map-mode]',{jfMapMode:'cities'});
  assert.match(second.nodes.get('.jf-map-counts').innerHTML,/mapped roles/);
  assert.equal(second.api.focusOffice(office.id),true,'Company detail can focus an office in-place');
  assert.equal(second.api.focusOffice('unknown'),false);
  assert.equal(JSON.stringify(office),before,'Map controls never update location evidence');
  second.api.destroy();
}
{
  const h=harness(), noCoordinates={...office,id:'address-only',address:'No coordinate address'};
  delete noCoordinates.latitude;delete noCoordinates.longitude;
  const invalid={...office,id:'invalid-point',address:'Invalid coordinate address',latitude:999};
  const stringCoordinates={...office,id:'strings',address:'String coordinate address',latitude:'37.78'};
  h.mount([role],{locations:[noCoordinates,invalid,stringCoordinates],mode:'offices'});
  assert.match(h.nodes.get('.jf-map-counts').innerHTML,/<strong>0<\/strong> office pins/);
  assert.equal(h.markers.length,0,'Missing, out-of-range and string coordinates never become pins');
  assert.match(h.nodes.get('.jf-map-city-list').innerHTML,/address only/,'Address evidence remains available without a pin');
  h.api.destroy();
}
{
  const h=harness(), noPin={...office,id:'unpinned-sunnyvale',city:'Sunnyvale',address:'1 Example Street, Sunnyvale, CA'};
  delete noPin.latitude;delete noPin.longitude;
  h.mount([role],{locations:[office,noPin],mode:'offices'});
  h.api.focusOffice(office.id);
  h.click('[data-jf-map-office]',{jfMapOffice:noPin.id});
  const lastView=h.log.filter(entry=>entry[0]==='view').at(-1), city=h.api.cities.find(row=>row.name==='Sunnyvale');
  assert.deepEqual(Array.from(lastView[1]),[city.lat,city.lng],'An address-only selection resets the previous office camera to a city overview');
  assert.equal(lastView[2],12);
  assert.match(h.nodes.get('.jf-map-selected span').textContent,/Sunnyvale · address only/);
  h.api.destroy();
}
{
  const h=harness(), site={...office,id:'explicit-worksite',relationship:'job_site',worksite_confirmed:true,job_ids:['a']};
  const currentRole={...role,office_locations:[site]};
  h.mount([currentRole],{locations:[office,site],mode:'offices'});
  assert.equal(h.markers.length,1,'The company reference and posting share a single physical office pin');
  assert.match(h.nodes.get('.jf-map-counts').innerHTML,/<strong>1<\/strong> office pins/);
  assert.match(h.markers[0].options.title,/role assignment unconfirmed/,'A shared pin does not confirm every role at the company');
  assert.equal(h.api.officeRows([site],[role]).length,0,'Historical job IDs without current attachments cannot confirm a worksite');
  assert.equal(h.api.officeRows([site],[{...currentRole,id:'other'}]).length,0,'Filtered-out confirmed roles remove their job-site pin');
  assert.equal(h.api.officeRows([site],[currentRole]).length,1);
  h.api.destroy();
}
{
  const h=harness(), unsafe={...office,address:'<img src=x onerror="bad()">'};
  const html=h.api.render([role],true,'',{locations:[unsafe],mode:'offices'});
  assert.match(html,/&lt;img src=x onerror=&quot;bad\(\)&quot;&gt;/);
  assert.ok(!html.includes('<img'),'Location records cannot inject markup');
  assert.match(h.api.render([role],true,'',{locations:[],mode:'offices'}),/Location not verified/);
}
// Every matched company remains represented even when there is no office pin.
{
  const h=harness();
  const roles=Array.from({length:139},(_,index)=>({...role,id:'company-'+index,company:'Company '+index}));
  const locations=[{...office,company:'Company 0'}, {...office,id:'address',company:'Company 1',latitude:undefined,longitude:undefined}];
  const reviews=[{company:'Company 2',status:'remote_only',observed_at:'2026-11-14'},
    {company:'Company 3',status:'verified',review_status:'verified'},
    {company:'Company 0',status:'remote_only'}];
  const before=JSON.stringify({roles,locations,reviews});
  const rows=h.api.companyRows(roles,locations,reviews), status=name=>rows.find(row=>row.company===name).status;
  assert.equal(rows.length,139);
  assert.equal(status('Company 0'),'pin','Actual coordinates take priority over a conflicting review');
  assert.equal(status('Company 1'),'address_only');
  assert.equal(status('Company 2'),'remote_only');
  assert.equal(status('Company 3'),'unverified','A review claiming verified cannot create an office');
  const html=h.mount(roles,{locations,reviews,mode:'offices'});
  assert.equal((html.match(/data-jf-map-company=/g)||[]).length,139);
  assert.match(html,/Company 138/);
  assert.equal(JSON.stringify({roles,locations,reviews}),before);
  h.api.destroy();
}
{
  const h=harness();
  const outside={...office,id:'new-york',company:'NY Company',city:'New York',address:'123 Example Avenue, New York',latitude:40.75,longitude:-73.99};
  const remote={...role,id:'remote',company:'Remote Company',title:'Distributed Systems Lead',location:'Remote'};
  const list=[role,{...role,id:'ny',company:'NY Company',location:'New York'},remote];
  let selected,scope;
  h.mount(list,{locations:[office,outside],mode:'offices',reviews:[{company:'Remote Company',status:'remote_only'}]},
    {onCompanySelect(company){selected=company;},onScopeChange(value){scope=value;}});
  assert.equal(h.markers.length,1,'Bay Area starts with local pins only');
  h.click('[data-jf-map-scope]',{jfMapScope:'all'});
  assert.equal(scope,'all');
  assert.match(h.nodes.get('.jf-map-counts').innerHTML,/<strong>2<\/strong> office pins/);
  const points=h.log.filter(entry=>entry[0]==='fit').at(-1)[1];
  assert.ok(points.some(point=>point[0]===40.75),'All locations fits actual distant coordinates');
  h.click('[data-jf-map-scope]',{jfMapScope:'bay'});
  h.click('[data-jf-map-office]',{jfMapOffice:'new-york'});
  assert.equal(scope,'all','Selecting an outside office automatically reveals its map area');
  const search=h.nodes.get('.jf-map-search input');search.value='distributed';search.handlers.input();
  assert.match(h.nodes.get('.jf-map-search-results').innerHTML,/Remote Company/,'Role search finds unpinned companies');
  h.click('[data-jf-map-company]',{jfMapCompany:'Remote Company'});
  assert.equal(selected,'Remote Company');
  assert.match(h.nodes.get('.jf-map-selected span').textContent,/no office pin/);
  assert.equal(h.log.at(-1)[0],'fit','An unpinned company resets an earlier office camera');
  h.api.destroy();
}
console.log('Map renderer lifecycle checks passed.');

'use strict';

// City locations are approximate grouping points, never inferred office addresses.
(function (global) {
  const cities = [
    {name:'San Francisco',lat:37.7749,lng:-122.4194,terms:['san francisco']},
    {name:'Oakland',lat:37.8044,lng:-122.2712,terms:['oakland']},
    {name:'Berkeley',lat:37.8715,lng:-122.2730,terms:['berkeley']},
    {name:'South San Francisco',lat:37.6547,lng:-122.4077,terms:['south san francisco']},
    {name:'San Mateo',lat:37.5630,lng:-122.3255,terms:['san mateo']},
    {name:'Redwood City',lat:37.4852,lng:-122.2364,terms:['redwood city']},
    {name:'Menlo Park',lat:37.4530,lng:-122.1817,terms:['menlo park']},
    {name:'Palo Alto',lat:37.4419,lng:-122.1430,terms:['palo alto']},
    {name:'Mountain View',lat:37.3861,lng:-122.0839,terms:['mountain view']},
    {name:'Sunnyvale',lat:37.3688,lng:-122.0363,terms:['sunnyvale']},
    {name:'Santa Clara',lat:37.3541,lng:-121.9552,terms:['santa clara']},
    {name:'San Jose',lat:37.3382,lng:-121.8863,terms:['san jose']},
    {name:'Fremont',lat:37.5485,lng:-121.9886,terms:['fremont']},
    {name:'San Bruno',lat:37.6305,lng:-122.4111,terms:['san bruno']},
    {name:'Burlingame',lat:37.5779,lng:-122.3481,terms:['burlingame']},
  ];
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const number = value => Number(value || 0).toLocaleString();
  const icon = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${({search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',fit:'<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/><circle cx="12" cy="12" r="4"/>',pin:'<path d="M19 10c0 6-7 11-7 11S5 16 5 10a7 7 0 1 1 14 0Z"/><circle cx="12" cy="10" r="2.2"/>'})[name]}</svg>`;
  let serial = 0;
  const pending = new Map();
  const instances = new Set();
  const viewports = new Map();
  function inCity(job, city) {
    let location = String(job.location || '').toLowerCase();
    if (city.name === 'San Francisco') location = location.replace(/south san francisco|san francisco bay area/g, '');
    if (city.name === 'Palo Alto') location = location.replace(/east palo alto/g, '');
    return city.terms.some(term => new RegExp('(?:^|[^a-z])' + term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(?:$|[^a-z])').test(location));
  }
  function cityRows(list) {
    return cities.map(city => ({...city, jobs:list.filter(job => inCity(job, city))})).filter(city => city.jobs.length);
  }
  function render(list, large = false, selectedCity = '') {
    const id = `jf-map-${++serial}`;
    const rows = cityRows(list);
    const mapped = new Set(rows.flatMap(city => city.jobs));
    pending.set(id, {rows, large, selectedCity});
    const cityButton = city => `<button type="button" class="jf-map-city${city.name === selectedCity ? ' is-selected' : ''}" data-jf-map-city="${escape(city.name)}" aria-pressed="${city.name === selectedCity}"><span>${escape(city.name)}</span><strong>${number(city.jobs.length)}</strong></button>`;
    return `<section id="${id}" class="jf-map${large ? ' jf-map-large' : ''}" aria-label="Bay Area role locations">
      <div class="jf-map-summary"><div><strong>${number(mapped.size)}</strong> mapped roles <span class="jf-map-divider">/</span> <strong>${rows.length}</strong> cities</div><span class="jf-map-live"><i></i>Street map</span></div>
      <div class="jf-map-stage">
        <div class="jf-map-canvas" tabindex="0" role="region" aria-label="Interactive Bay Area street map. Use arrow keys to pan, plus and minus to zoom. City results are also available below."></div>
        <div class="jf-map-search-wrap"><div class="jf-map-search">${icon('search')}<input type="search" placeholder="Search cities" aria-label="Search cities on map" autocomplete="off" aria-controls="${id}-results" aria-expanded="false"><span class="jf-map-search-key" aria-hidden="true">⌕</span></div><div id="${id}-results" class="jf-map-search-results" hidden></div></div>
        <button type="button" class="jf-map-fit" title="Fit all Bay Area locations" aria-label="Fit all Bay Area locations">${icon('fit')}<span>Fit area</span></button>
        <div class="jf-map-selected" ${selectedCity ? '' : 'hidden'}>${icon('pin')}<span>${escape(selectedCity)}</span><button type="button" aria-label="Clear selected city">×</button></div>
        <div class="jf-map-status" role="status" hidden><strong>Street map unavailable</strong><span>City results are still available below.</span><button type="button" class="jf-map-retry">Retry map</button></div>
        <span class="jf-map-provider">© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap contributors</a></span>
      </div>
      <div class="jf-map-footnote"><span>${icon('pin')} City centers · not office addresses</span><span>Drag to pan · + / − to zoom</span></div>
      <details class="jf-map-locations"><summary>Browse ${rows.length} ${rows.length === 1 ? 'city' : 'cities'}<span>Location results</span></summary><div class="jf-map-city-list">${rows.map(cityButton).join('') || '<p class="jf-map-empty">No city-specific roles match these filters.</p>'}</div></details>
      <p class="jf-map-disclosure">${number(list.length - mapped.size)} roles have remote, regional, or other locations. Roles listing several cities can appear in each.</p>
    </section>`;
  }
  function mount(root, options = {}) {
    root.querySelectorAll('.jf-map').forEach(host => {
      const data = pending.get(host.id);
      if (!data || host.dataset.jfMapMounted) return;
      host.dataset.jfMapMounted = 'true';
      pending.delete(host.id);
      const instance = {host, map:null, resize:null, intersection:null, handlers:[], markers:null, lastSelection:data.selectedCity, destroyed:false};
      instances.add(instance);
      const listen = (node, type, fn) => {node.addEventListener(type, fn); instance.handlers.push(() => node.removeEventListener(type, fn));};
      const canvas = host.querySelector('.jf-map-canvas');
      const search = host.querySelector('.jf-map-search input');
      const results = host.querySelector('.jf-map-search-results');
      const status = host.querySelector('.jf-map-status');
      const selected = host.querySelector('.jf-map-selected');
      const compactKey = data.large ? 'large' : 'compact';
      let layer = null, failedTiles = 0, loadedTiles = 0;
      function markSelected(cityName) {
        data.selectedCity = cityName;
        selected.hidden = !cityName;
        selected.querySelector('span').textContent = cityName;
        host.querySelectorAll('[data-jf-map-city]').forEach(button => {
          button.classList.toggle('is-selected', button.dataset.jfMapCity === cityName);
          button.setAttribute('aria-pressed', String(button.dataset.jfMapCity === cityName));
        });
      }
      function chooseCity(cityName) {
        const city = data.rows.find(row => row.name === cityName);
        if (!city) return;
        const fromSearchResult = results.contains(document.activeElement);
        markSelected(cityName);
        hideSearch();
        search.value = '';
        if (instance.map) {
          instance.map.setView([city.lat, city.lng], Math.max(12, instance.map.getZoom()), {animate:false});
          drawMarkers();
        }
        if (fromSearchResult) canvas.focus({preventScroll:true});
        options.onSelect?.(cityName);
      }
      function hideSearch() {results.hidden = true; search.setAttribute('aria-expanded', 'false');}
      function showSearch() {
        const query = search.value.trim().toLowerCase();
        const matches = data.rows.filter(city => city.name.toLowerCase().includes(query));
        results.innerHTML = matches.map(city => `<button type="button" data-jf-map-city="${escape(city.name)}"><span>${icon('pin')} ${escape(city.name)}</span><strong>${number(city.jobs.length)} <small>roles</small></strong></button>`).join('') || '<p>No matching cities</p>';
        results.hidden = false;
        search.setAttribute('aria-expanded', 'true');
      }
      function fitArea() {
        if (!instance.map) return;
        const points = data.rows.length > 1 ? data.rows.map(city => [city.lat,city.lng]) : [[37.31,-122.49],[37.90,-121.83]];
        instance.map.fitBounds(points, {paddingTopLeft:[42,82], paddingBottomRight:[44,54],maxZoom:11,animate:false});
      }
      // Cluster nearby city markers in screen space. Cluster totals deduplicate roles
      // that name multiple cities; individual city counts retain all matching roles.
      function drawMarkers() {
        if (!instance.map) return;
        const L = global.L, map = instance.map;
        instance.markers.clearLayers();
        const groups = [];
        for (const city of data.rows) {
          const point = map.latLngToLayerPoint([city.lat,city.lng]);
          let group = map.getZoom() < 12 && city.name !== data.selectedCity ? groups.find(item => !item.selected && item.point.distanceTo(point) < 48) : null;
          if (!group) {group = {cities:[],point,selected:city.name === data.selectedCity};groups.push(group);}
          group.cities.push(city);
        }
        groups.forEach(group => {
          const clustered = group.cities.length > 1;
          const count = new Set(group.cities.flatMap(city => city.jobs)).size;
          const center = [group.cities.reduce((sum,city) => sum + city.lat,0)/group.cities.length,group.cities.reduce((sum,city) => sum + city.lng,0)/group.cities.length];
          const city = group.cities[0];
          const label = clustered ? `${number(count)} roles across ${group.cities.length} cities. Zoom to expand.` : `${city.name}: ${number(count)} roles. View city results.`;
          const width = count >= 1000 ? 58 : count >= 100 ? 48 : 40;
          const marker = L.marker(center, {
            icon:L.divIcon({className:`jf-map-marker${clustered ? ' is-cluster' : ''}${group.selected ? ' is-selected' : ''}`,html:`<span>${number(count)}</span>${clustered ? `<small>${group.cities.length} cities</small>` : ''}`,iconSize:[width,clustered ? 46 : 36],iconAnchor:[width/2,clustered ? 23 : 18]}),
            title:label,alt:label,keyboard:true,riseOnHover:true,zIndexOffset:group.selected?1000:count,
          }).addTo(instance.markers);
          marker.on('add', () => {const element=marker.getElement();if(element){element.setAttribute('role','button');element.setAttribute('aria-label',label);element.setAttribute('aria-pressed',String(group.selected));}});
          // The marker is already on the layer, so apply labels for this initial add too.
          const element = marker.getElement();
          if(element){
            element.setAttribute('role','button');element.setAttribute('aria-label',label);element.setAttribute('aria-pressed',String(group.selected));
            element.addEventListener('keydown', event => {if(event.key===' '){event.preventDefault();marker.fire('click');}});
          }
          marker.bindTooltip(escape(clustered ? group.cities.map(item => item.name).join(' · ') : city.name),{direction:'top',offset:[0,-18],className:'jf-map-tooltip'});
          marker.on('click', () => {
            if (clustered) map.fitBounds(group.cities.map(item=>[item.lat,item.lng]), {padding:[64,80],maxZoom:13,animate:false});
            else chooseCity(city.name);
          });
        });
      }
      function showUnavailable(message) {
        status.hidden = false;
        status.querySelector('strong').textContent = message || 'Street map unavailable';
        host.classList.add('has-map-error');
      }
      function initialize() {
        if (instance.destroyed || instance.map || !canvas.clientWidth || !canvas.clientHeight) return;
        if (!global.L) {showUnavailable('Street map could not start');return;}
        const L = global.L;
        instance.map = L.map(canvas, {zoomControl:false,attributionControl:false,scrollWheelZoom:false,minZoom:7,maxZoom:18,zoomSnap:1});
        const map = instance.map;
        L.control.zoom({position:'bottomright'}).addTo(map);
        L.control.scale({position:'bottomleft',imperial:true,metric:true,maxWidth:100}).addTo(map);
        instance.markers = L.layerGroup().addTo(map);
        layer = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
          minZoom:7,maxZoom:18,detectRetina:false,referrerPolicy:'origin',updateWhenIdle:true,keepBuffer:1,
        });
        layer.on('loading', () => {failedTiles=0;loadedTiles=0;});
        layer.on('tileload', () => {loadedTiles++;status.hidden=true;host.classList.remove('has-map-error');});
        layer.on('tileerror', () => {failedTiles++;showUnavailable(loadedTiles ? 'Some map tiles could not load' : 'Street map unavailable');});
        layer.on('load', () => {if (failedTiles) showUnavailable(loadedTiles ? 'Some map tiles could not load' : 'Street map unavailable');});
        map.on('zoomend', drawMarkers);
        const previous = viewports.get(compactKey);
        const selectedRow = data.rows.find(city => city.name === data.selectedCity);
        if (selectedRow && previous?.selected !== data.selectedCity) map.setView([selectedRow.lat,selectedRow.lng],12,{animate:false});
        else if (previous) map.setView(previous.center, previous.zoom, {animate:false});
        else fitArea();
        layer.addTo(map);
        drawMarkers();
        instance.intersection?.disconnect();
      }
      listen(search, 'focus', showSearch);
      listen(search, 'input', showSearch);
      listen(search, 'keydown', event => {if(event.key==='Escape'){hideSearch();search.blur();}else if(event.key==='ArrowDown'){event.preventDefault();results.querySelector('button')?.focus();}else if(event.key==='Enter'){event.preventDefault();const match=results.querySelector('button');if(match)chooseCity(match.dataset.jfMapCity);}});
      listen(host, 'keydown', event => {if(event.key==='Escape')hideSearch();});
      listen(host, 'focusout', event => {if(!host.querySelector('.jf-map-search-wrap').contains(event.relatedTarget))hideSearch();});
      listen(host, 'click', event => {
        const button=event.target.closest('[data-jf-map-city]');
        if(button){chooseCity(button.dataset.jfMapCity);return;}
        if(!event.target.closest('.jf-map-search-wrap'))hideSearch();
      });
      listen(host.querySelector('.jf-map-fit'), 'click', fitArea);
      listen(selected.querySelector('button'), 'click', () => {markSelected('');drawMarkers();options.onSelect?.('');});
      listen(host.querySelector('.jf-map-retry'), 'click', () => {if(layer){status.hidden=true;layer.redraw();}else initialize();});
      if (global.ResizeObserver) {
        instance.resize = new ResizeObserver(() => {if(instance.map)instance.map.invalidateSize({pan:false});});
        instance.resize.observe(canvas);
      }
      if (global.IntersectionObserver) {
        instance.intersection = new IntersectionObserver(entries => {if(entries.some(entry=>entry.isIntersecting))initialize();});
        instance.intersection.observe(canvas);
      } else initialize();
      // ResizeObserver handles dimensions; details toggles make closed overview
      // sections initialize correctly even in browsers without IntersectionObserver.
      for(let node=host.parentElement;node;node=node.parentElement){if(node.tagName==='DETAILS')listen(node,'toggle',()=>{if(node.open){initialize();instance.map?.invalidateSize({pan:false});}});}
      instance.cleanup = () => {
        instance.destroyed=true;
        if(instance.map) {
          viewports.set(compactKey,{center:instance.map.getCenter(),zoom:instance.map.getZoom(),selected:data.selectedCity});
          instance.map.remove();
        }
        instance.resize?.disconnect();instance.intersection?.disconnect();
        instance.handlers.forEach(remove=>remove());
        delete host.dataset.jfMapMounted;
      };
    });
  }
  function destroy() {instances.forEach(instance=>instance.cleanup());instances.clear();pending.clear();}
  global.JobflowMaps = {cities,inCity,render,mount,destroy};
})(window);

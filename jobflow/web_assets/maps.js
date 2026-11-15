'use strict';

// City locations are approximate grouping points, never inferred office addresses.
(function (global) {
  const cities = [
    {name:'San Francisco',lat:37.7749,lng:-122.4194,terms:['san francisco']},
    {name:'Oakland',lat:37.8044,lng:-122.2712,terms:['oakland']},
    {name:'Berkeley',lat:37.8715,lng:-122.2730,terms:['berkeley']},
    {name:'South San Francisco',lat:37.6547,lng:-122.4077,terms:['south san francisco']},
    {name:'San Mateo',lat:37.5630,lng:-122.3255,terms:['san mateo']},
    // Approximate grouping point: Census ACS26 internal point, never an office pin.
    // https://tigerweb.geo.census.gov/tigerwebmain/Files/acs26/tigerweb_acs26_incplace_ca.html
    {name:'Foster City',lat:37.56482,lng:-122.2507815,terms:['foster city']},
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
  const vectorStyles = {light:'https://tiles.openfreemap.org/styles/positron',dark:'https://tiles.openfreemap.org/styles/dark'};
  const vectorAttribution = '<a href="https://openfreemap.org/" target="_blank" rel="noopener noreferrer">OpenFreeMap</a> · <a href="https://www.openmaptiles.org/" target="_blank" rel="noopener noreferrer">© OpenMapTiles</a> · <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">© OpenStreetMap</a>';
  const rasterAttribution = '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap contributors</a>';
  const mapTheme = () => global.document?.documentElement?.dataset.theme === 'dark' ? 'dark' : 'light';
  // Named layer IDs/types checked against OpenFreeMap Positron on 2026-11-14.
  // Limit overrides to these geographic layers; dark cartography keeps its own palette.
  const lightMapPaint = [
    ['background','background','background-color','#f7f8f5'],
    ['water','fill','fill-color','#c7e4f2'],
    ['waterway','line','line-color','#acd2e5'],
    ['park','fill','fill-color','#dfedd9'],
    ['landcover_wood','fill','fill-color','#e2eddc'],
    ['landuse_residential','fill','fill-color','#eef0eb'],
    ['building','fill','fill-color','#e5e9e5'],
    ['building','fill','fill-outline-color','#d8dfd9'],
    ['highway_minor','line','line-color','#dfe5e3'],
    ['highway_major_casing','line','line-color','#d6dfe2'],
    ['highway_major_inner','line','line-color','#ffffff'],
    ['highway_motorway_casing','line','line-color','#ccd9df'],
    ['highway_motorway_inner','line','line-color','#fffdf4'],
    ['highway_motorway_bridge_casing','line','line-color','#c1d0d8'],
    ['highway_motorway_bridge_inner','line','line-color','#fffdf4'],
    ['boundary_3','line','line-color','#bac8cd'],
    ['boundary_3','line','line-opacity',0.55],
    ['label_city','symbol','text-color','#3f5560'],
    ['label_town','symbol','text-color','#52666e'],
    ['label_village','symbol','text-color','#61747b'],
    ['label_other','symbol','text-color','#718087'],
    ['water_name_point_label','symbol','text-color','#5c8ca4'],
    ['water_name_line_label','symbol','text-color','#5c8ca4'],
    ['waterway_line_label','symbol','text-color','#5c8ca4'],
  ];
  function styleLightMap(map) {
    if (mapTheme() !== 'light' || !map?.getLayer || !map?.setPaintProperty || map.getLayer('water')?.type !== 'fill') return false;
    for (const [id,type,property,value] of lightMapPaint) {
      if (map.getLayer(id)?.type === type) map.setPaintProperty(id,property,value);
    }
    return true;
  }

  const icon = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${({search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',fit:'<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/><circle cx="12" cy="12" r="4"/>',pin:'<path d="M19 10c0 6-7 11-7 11S5 16 5 10a7 7 0 1 1 14 0Z"/><circle cx="12" cy="10" r="2.2"/>'})[name]}</svg>`;
  let serial = 0;
  const pending = new Map();
  const instances = new Set();
  const viewports = new Map();
  function inCity(job, city) {
    let location = String(job.location || '').toLowerCase();
    if (city.name === 'San Francisco') location = location.replace(/south san francisco|san francisco bay area/g, '');
    if (city.name === 'Palo Alto') location = location.replace(/east palo alto/g, '');
    return city.terms.some(term => {
      const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const specific = location.replace(new RegExp('\\b(?:greater\\s+)?' + escaped + '\\s+(?:county|region|bay area|metro(?:politan)?(?: area)?)\\b','g'),'')
        .replace(new RegExp('\\bgreater\\s+' + escaped + '\\b','g'),'');
      return new RegExp('(?:^|[^a-z])' + escaped + '(?:$|[^a-z])').test(specific);
    });
  }
  function cityRows(list) {
    return cities.map(city => ({...city, jobs:list.filter(job => inCity(job, city))})).filter(city => city.jobs.length);
  }
  function hasCoordinates(office) {
    return typeof office.latitude === 'number' && Number.isFinite(office.latitude) && Math.abs(office.latitude) <= 90 &&
      typeof office.longitude === 'number' && Number.isFinite(office.longitude) && Math.abs(office.longitude) <= 180;
  }
  function officeRows(locations, list) {
    const companies = new Set(list.map(job => job.company)), seen = new Set();
    const confirmedJobs = office => list.filter(job => job.company === office.company && (office.job_ids || []).includes(job.id) &&
      (job.office_locations || []).some(current => current.id === office.id && current.relationship === 'job_site' && current.worksite_confirmed === true));
    return (Array.isArray(locations) ? locations : []).filter(office => {
      if (!office || !office.id || seen.has(office.id) || !companies.has(office.company) || !office.address ||
        !['company_office','job_site'].includes(office.relationship) || !['address','building'].includes(office.precision)) return false;
      if (office.relationship === 'job_site' && (office.worksite_confirmed !== true || !confirmedJobs(office).length)) return false;
      seen.add(office.id); return true;
    }).map(office => ({...office, jobs:office.relationship === 'job_site' ? confirmedJobs(office) : []}));
  }
  function officeSites(offices) {
    const grouped = new Map();
    for (const office of offices) {
      const key = office.company.toLowerCase() + '|' + office.address.toLowerCase().replace(/[^a-z0-9]/g,'');
      if (!grouped.has(key)) grouped.set(key,[]);
      grouped.get(key).push(office);
    }
    return [...grouped.values()].map(records => {
      const primary = records.find(office => office.relationship === 'company_office') || records[0];
      const pin = records.find(hasCoordinates);
      return {...primary,...(pin ? {latitude:pin.latitude,longitude:pin.longitude,precision:pin.precision,coordinate_source_url:pin.coordinate_source_url,coordinate_method:pin.coordinate_method,coordinate_note:pin.coordinate_note} : {}),records};
    });
  }
  // Scope changes which recorded pins are visible, never a role's worksite.
  const bayCities = new Set([...cities.map(city => city.name),'East Palo Alto','Pleasanton','Dublin','San Ramon','Walnut Creek','Hayward','Union City','Newark','Milpitas','Cupertino','Campbell','Los Gatos','Los Altos','San Carlos','Belmont','Millbrae','Richmond','San Rafael','Emeryville','Alameda','Sausalito','Novato','Santa Rosa','Napa','Concord','San Leandro','Livermore']);
  function inBayArea(office) {
    if (!hasCoordinates(office)) return bayCities.has(office.city);
    return office.latitude >= 37 && office.latitude <= 38.85 && office.longitude >= -123.1 && office.longitude <= -121.45;
  }
  function companyRows(list, locations = [], reviews = []) {
    const grouped = new Map(), offices = officeSites(officeRows(locations,list));
    for (const job of list) {
      if (!grouped.has(job.company)) grouped.set(job.company,new Map());
      grouped.get(job.company).set(job.id,job);
    }
    const latest = new Map();
    for (const review of reviews || []) {
      if (!latest.has(review.company) || String(review.observed_at || '') >= String(latest.get(review.company).observed_at || '')) latest.set(review.company,review);
    }
    return [...grouped].map(([company,jobs]) => {
      const sites = offices.filter(office => office.company === company), review = latest.get(company);
      const status = sites.some(hasCoordinates) ? 'pin' : sites.length ? 'address_only' : review?.status === 'remote_only' ? 'remote_only' : 'unverified';
      return {company,jobs:[...jobs.values()],offices:sites,review,status};
    }).sort((a,b) => a.company.localeCompare(b.company));
  }
  const coverageLabels = {pin:'Office pin available',address_only:'Verified address · no pin',remote_only:'Remote-only company',unverified:'Location not verified'};
  const scopedOffices = data => data.offices.filter(office => data.scope === 'all' || inBayArea(office));
  const companyButton = company => `<button type="button" class="jf-map-office jf-map-company" data-jf-map-company="${escape(company.company)}"><span><strong>${escape(company.company)}</strong><small>${escape(coverageLabels[company.status])} · ${number(company.jobs.length)} ${company.jobs.length === 1 ? 'role' : 'roles'}</small>${company.offices.length ? `<span>${escape(company.offices.map(office => office.city).filter((city,index,all) => all.indexOf(city) === index).join(' · '))}</span>` : ''}</span><span class="jf-map-result-arrow" aria-hidden="true">→</span></button>`;
  const officeDescription = office => office.relationship === 'job_site' && office.worksite_confirmed === true ? 'Confirmed role location' : 'Company office · role assignment unconfirmed';
  const officeButton = office => `<button type="button" class="jf-map-office" data-jf-map-office="${escape(office.id)}"><span><strong>${escape(office.company)}</strong><span>${escape(office.address)}</span><small>${escape(officeDescription(office))}${hasCoordinates(office) ? '' : ' · address only'}</small></span>${icon('pin')}</button>`;
  function mapCounts(data) {
    return data.mode === 'offices' ? `<strong>${officeSites(scopedOffices(data)).filter(hasCoordinates).length}</strong> office pins <span class="jf-map-divider">/</span> ${data.scope === 'all' ? 'All locations' : 'Bay Area'}` : `<strong>${number(data.mapped)}</strong> mapped roles <span class="jf-map-divider">/</span> <strong>${data.rows.length}</strong> cities`;
  }
  function locationList(data) {
    if (data.mode === 'offices') return data.companies.map(company => companyButton(company) + company.offices.map(officeButton).join('')).join('') || '<p class="jf-map-empty">No companies match these filters.</p>';
    return data.rows.map(city => `<button type="button" class="jf-map-city${city.name === data.selectedCity ? ' is-selected' : ''}" data-jf-map-city="${escape(city.name)}" aria-pressed="${city.name === data.selectedCity}"><span>${escape(city.name)}</span><strong>${number(city.jobs.length)}</strong></button>`).join('') || '<p class="jf-map-empty">No city-specific roles match these filters.</p>';
  }
  const disclosure = data => data.mode === 'offices' ? 'Office pins show sourced company addresses. An office is not a confirmed worksite unless a posting explicitly links the role. Address pins may be approximate.' : `${number(data.unmapped)} roles have remote, regional, or other locations. Roles listing several cities can appear in each.`;
  function render(list, large = false, selectedCity = '', settings = {}) {
    const id = `jf-map-${++serial}`;
    const rows = cityRows(list);
    const mapped = new Set(rows.flatMap(city => city.jobs));
    const data = {rows,large,selectedCity,mapped:mapped.size,unmapped:list.length-mapped.size,
      offices:officeRows(settings.locations,list),companies:companyRows(list,settings.locations,settings.reviews),scope:settings.scope === 'all' ? 'all' : 'bay',mode:settings.mode === 'offices' ? 'offices' : 'cities',selectedOffice:settings.selectedOffice || ''};
    pending.set(id, data);
    return `<section id="${id}" class="jf-map${large ? ' jf-map-large' : ''}" aria-label="Company and role locations">
      <div class="jf-map-summary"><div class="jf-map-counts" aria-live="polite">${mapCounts(data)}</div><div class="jf-map-modes" role="group" aria-label="Map locations">${['cities','offices'].map(mode => `<button type="button" data-jf-map-mode="${mode}" aria-pressed="${data.mode === mode}">${mode === 'cities' ? 'Cities' : 'Offices'}</button>`).join('')}</div></div>
      <div class="jf-map-scope-row"><span>Map area</span><div class="jf-map-modes" role="group" aria-label="Map area">${[['bay','Bay Area'],['all','All locations']].map(([value,label]) => `<button type="button" data-jf-map-scope="${value}" aria-pressed="${data.scope === value}">${label}</button>`).join('')}</div><span class="jf-map-directory-total">${data.companies.length} ${data.companies.length === 1 ? 'company' : 'companies'} in directory</span></div>
      <div class="jf-map-stage">
        <div class="jf-map-canvas" tabindex="0" role="region" aria-label="Interactive street map. Use arrow keys to pan, plus and minus to zoom. Company and city results are also available below."></div>
        <div class="jf-map-search-wrap"><div class="jf-map-search">${icon('search')}<input type="search" placeholder="Search cities" aria-label="Search cities on map" autocomplete="off" aria-controls="${id}-results" aria-expanded="false"><span class="jf-map-search-key" aria-hidden="true">⌕</span></div><div id="${id}-results" class="jf-map-search-results" hidden></div></div>
        <button type="button" class="jf-map-fit" title="Fit visible locations" aria-label="Fit visible locations">${icon('fit')}<span>Fit area</span></button>
        <div class="jf-map-selected" ${selectedCity ? '' : 'hidden'}>${icon('pin')}<span>${escape(selectedCity)}</span><button type="button" aria-label="Clear selected location">×</button></div>
        <div class="jf-map-status" role="status" hidden><strong>Street map unavailable</strong><span>Company and city results are still available below.</span><button type="button" class="jf-map-retry">Retry map</button></div>
        <span class="jf-map-provider">${vectorAttribution}</span>
      </div>
      <div class="jf-map-footnote"><span class="jf-map-precision">${icon('pin')} ${data.mode === 'offices' ? 'Sourced office addresses · select for details' : 'City centers · select to see companies'}</span><span class="jf-map-basemap-label">Vector street map</span></div>
      <details class="jf-map-locations"><summary><span class="jf-map-list-title">Browse ${data.mode === 'offices' ? data.companies.length + ' companies · all locations' : rows.length + ' cities'}</span><span>Location results</span></summary><div class="jf-map-city-list${data.mode === 'offices' ? ' is-offices' : ''}">${locationList(data)}</div></details>
      <p class="jf-map-disclosure">${disclosure(data)}</p>
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
      const savedViewport = viewports.get(compactKey);
      let maintainFit = (!savedViewport || savedViewport.mode !== data.mode || savedViewport.scope !== data.scope) && !data.selectedCity && !data.selectedOffice, measuredSize = '';
      let layer = null, vectorMap = null, themeObserver = null, baseMode = '', startTimer = null, failureTimer = null;
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
        maintainFit = false;
        data.selectedOffice = '';
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
      function chooseCompany(name) {
        const company = data.companies.find(row => row.company === name);
        if (!company) return;
        const fromSearchResult = results.contains(document.activeElement);
        data.selectedOffice = ''; markSelected(''); hideSearch(); search.value = '';
        const pins = company.offices.filter(hasCoordinates);
        if (pins.some(office => !inBayArea(office))) {data.scope='all';options.onScopeChange?.('all');}
        updateModeChrome(); maintainFit = false;
        if (instance.map) {
          if (pins.length) instance.map.fitBounds(pins.map(office => [office.latitude,office.longitude]),{padding:[70,90],maxZoom:15,animate:false});
          else fitArea();
        }
        selected.hidden = false;
        selected.querySelector('span').textContent = name + (pins.length ? '' : ' · no office pin');
        drawMarkers();
        if (fromSearchResult) canvas.focus({preventScroll:true});
        options.onCompanySelect?.(name);
      }
      function chooseOffice(id) {
        const office = data.offices.find(row => row.id === id);
        if (!office) return;
        if (!inBayArea(office) && data.scope !== 'all') {data.scope='all';updateModeChrome();options.onScopeChange?.('all');}
        const fromSearchResult = results.contains(document.activeElement);
        maintainFit = false;
        data.selectedOffice = id;
        markSelected(office.city || '');
        selected.hidden = false;
        hideSearch(); search.value = '';
        const pin = hasCoordinates(office) ? office : officeSites(data.offices).find(site => site.records.some(record => record.id === id) && hasCoordinates(site));
        selected.querySelector('span').textContent = office.company + ' · ' + office.city + (pin ? '' : ' · address only');
        if (instance.map) {
          if (pin) instance.map.setView([pin.latitude,pin.longitude],17,{animate:false});
          else {
            const city = cities.find(row => row.name === office.city);
            if (city) instance.map.setView([city.lat,city.lng],12,{animate:false});
            else fitArea();
          }
        }
        drawMarkers();
        if (fromSearchResult) canvas.focus({preventScroll:true});
        options.onOfficeSelect?.(office);
      }
      instance.focusOffice = id => {
        if (!data.offices.some(office => office.id === id)) return false;
        changeMode('offices',false); chooseOffice(id); return true;
      };
      instance.focusCompany = name => {
        if (!data.companies.some(company => company.company === name)) return false;
        if (data.mode !== 'offices') changeMode('offices',false);
        chooseCompany(name); return true;
      };
      instance.clearSelection = () => {data.selectedOffice='';markSelected('');maintainFit=true;fitArea();drawMarkers();};
      function updateModeChrome() {
        const offices = data.mode === 'offices';
        host.querySelector('.jf-map-counts').innerHTML = mapCounts(data);
        host.querySelectorAll('[data-jf-map-mode]').forEach(button => button.setAttribute('aria-pressed',String(button.dataset.jfMapMode === data.mode)));
        host.querySelectorAll('[data-jf-map-scope]').forEach(button => button.setAttribute('aria-pressed',String(button.dataset.jfMapScope === data.scope)));
        search.placeholder = offices ? 'Search companies, addresses, roles' : 'Search cities';
        search.setAttribute('aria-label',offices ? 'Search companies, addresses and roles' : 'Search cities on map');
        host.querySelector('.jf-map-precision').innerHTML = icon('pin') + (offices ? ' Sourced office addresses · select for details' : ' City centers · select to see companies');
        host.querySelector('.jf-map-list-title').textContent = 'Browse ' + (offices ? data.companies.length + ' companies · all locations' : data.rows.length + ' cities');
        const list = host.querySelector('.jf-map-city-list');
        list.classList.toggle('is-offices',offices); list.innerHTML = locationList(data);
        host.querySelector('.jf-map-disclosure').textContent = disclosure(data);
      }
      function changeMode(mode, fit = true) {
        if (!['cities','offices'].includes(mode)) return;
        data.mode = mode; data.selectedOffice = ''; markSelected('');
        if (mode === 'cities' && data.scope !== 'bay') {data.scope='bay';options.onScopeChange?.('bay');}
        hideSearch(); search.value = ''; updateModeChrome();
        if (fit) {maintainFit = true; fitArea();}
        drawMarkers(); options.onModeChange?.(mode);
      }
      function changeScope(scope) {
        if (!['bay','all'].includes(scope)) return;
        data.scope = scope; data.selectedOffice=''; markSelected('');
        if (scope === 'all' && data.mode !== 'offices') {data.mode='offices';options.onModeChange?.('offices');}
        hideSearch(); search.value='';updateModeChrome();maintainFit=true;fitArea();drawMarkers();options.onScopeChange?.(scope);
      }
      function hideSearch() {results.hidden = true; search.setAttribute('aria-expanded', 'false');}
      function showSearch() {
        const query = search.value.trim().toLowerCase();
        if (data.mode === 'offices') {
          const matches = data.companies.filter(company => `${company.company} ${company.offices.map(office => office.city + ' ' + office.address).join(' ')} ${company.jobs.map(job => job.title + ' ' + job.location).join(' ')}`.toLowerCase().includes(query));
          results.innerHTML = matches.map(company => companyButton(company) + (query ? company.offices.filter(office => `${office.city} ${office.address}`.toLowerCase().includes(query)).map(officeButton).join('') : '')).join('') || '<p>No matching companies or roles</p>';
          results.hidden = false; search.setAttribute('aria-expanded','true'); return;
        }
        const matches = data.rows.filter(city => city.name.toLowerCase().includes(query));
        results.innerHTML = matches.map(city => `<button type="button" data-jf-map-city="${escape(city.name)}"><span>${icon('pin')} ${escape(city.name)}</span><strong>${number(city.jobs.length)} <small>roles</small></strong></button>`).join('') || '<p>No matching cities</p>';
        results.hidden = false;
        search.setAttribute('aria-expanded', 'true');
      }
      function fitArea() {
        if (!instance.map) return;
        const offices = scopedOffices(data).filter(hasCoordinates);
        const points = data.mode === 'offices' && offices.length ? offices.map(office => [office.latitude,office.longitude]) : data.rows.length > 1 ? data.rows.map(city => [city.lat,city.lng]) : [[37.31,-122.49],[37.90,-121.83]];
        instance.map.fitBounds(points, {paddingTopLeft:[50,75], paddingBottomRight:[50,45],maxZoom:data.mode === 'offices' ? 15.5 : 11.5,animate:false});
      }
      function drawOfficeMarkers() {
        const L = global.L, map = instance.map, groups = [];
        for (const office of officeSites(scopedOffices(data)).filter(hasCoordinates)) {
          const point = map.latLngToLayerPoint([office.latitude,office.longitude]);
          let group = groups.find(group => (map.getZoom() < 16 || group.offices.some(item => item.latitude === office.latitude && item.longitude === office.longitude)) && group.point.distanceTo(point) < 55);
          if (!group) {group={offices:[],point};groups.push(group);}
          group.offices.push(office);
        }
        for (const group of groups) {
          const office = group.offices[0], clustered = group.offices.length > 1;
          const active = group.offices.some(item => item.records.some(record => record.id === data.selectedOffice));
          const center = [group.offices.reduce((sum,item) => sum+item.latitude,0)/group.offices.length,group.offices.reduce((sum,item) => sum+item.longitude,0)/group.offices.length];
          const label = clustered ? `${group.offices.length} offices. Select to see companies and addresses.` : `${office.company}: ${office.address}. ${officeDescription(office)}. ${office.precision === 'address' ? 'Approximate address pin' : 'Office pin'}.`;
          const marker = L.marker(center,{icon:L.divIcon({className:`jf-map-marker jf-map-office-marker${clustered ? ' is-cluster' : ''}${active ? ' is-selected' : ''}${!clustered && office.precision === 'address' ? ' is-approximate' : ''}`,html:clustered ? `<span>${group.offices.length}</span><small>offices</small>` : `${icon('pin')}<span>${escape(office.company)}</span>`,iconSize:[clustered ? 58 : 130,clustered ? 48 : 34],iconAnchor:[clustered ? 29 : 65,clustered ? 24 : 17]}),title:label,alt:label,keyboard:true,riseOnHover:true,zIndexOffset:active?1000:10}).addTo(instance.markers);
          const element = marker.getElement();
          if (element) {element.setAttribute('role','button');element.setAttribute('aria-label',label);element.setAttribute('aria-pressed',String(active));element.addEventListener('keydown',event=>{if(event.key===' '){event.preventDefault();marker.fire('click');}});}
          marker.bindTooltip(escape(clustered ? group.offices.map(item => item.company).join(' · ') : office.company + ' · ' + office.address),{direction:'top',offset:[0,-18],className:'jf-map-tooltip'});
          if (clustered) marker.bindPopup(`<div class="jf-map-office-popup"><strong>${group.offices.length} office addresses</strong>${group.offices.map(officeButton).join('')}</div>`,{maxWidth:320});
          marker.on('click',() => {
            if (!clustered) chooseOffice(office.id);
          });
        }
      }
      // Cluster nearby city markers in screen space. Cluster totals deduplicate roles
      // that name multiple cities; individual city counts retain all matching roles.
      function drawMarkers() {
        if (!instance.map) return;
        const L = global.L, map = instance.map;
        instance.markers.clearLayers();
        if (data.mode === 'offices') {drawOfficeMarkers();return;}
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
          const width = count >= 1000 ? 66 : count >= 100 ? 58 : 50;
          const marker = L.marker(center, {
            icon:L.divIcon({className:`jf-map-marker${clustered ? ' is-cluster' : ''}${group.selected ? ' is-selected' : ''}`,html:`<span>${number(count)}</span>${clustered ? `<small>${group.cities.length} cities</small>` : ''}`,iconSize:[width,clustered ? 48 : 34],iconAnchor:[width/2,clustered ? 24 : 17]}),
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
      function clearBasemap() {
        clearTimeout(startTimer); clearTimeout(failureTimer);
        startTimer = null; failureTimer = null;
        if (layer && instance.map?.hasLayer(layer)) instance.map.removeLayer(layer);
        layer = null; vectorMap = null;
      }
      function useStandardMap(reason) {
        if (instance.destroyed || !instance.map) return;
        clearBasemap();
        baseMode = 'standard';
        host.querySelector('.jf-map-provider').innerHTML = rasterAttribution;
        host.querySelector('.jf-map-basemap-label').textContent = reason || 'Standard street map';
        host.classList.add('is-standard-map');
        let failedTiles = 0, loadedTiles = 0;
        layer = global.L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
          minZoom:2,maxZoom:18,detectRetina:false,referrerPolicy:'origin',updateWhenIdle:true,keepBuffer:1,
        });
        layer.on('loading', () => {failedTiles=0;loadedTiles=0;});
        layer.on('tileload', () => {loadedTiles++;status.hidden=true;host.classList.remove('has-map-error');});
        layer.on('tileerror', () => {failedTiles++;});
        layer.on('load', () => {if (failedTiles) showUnavailable(loadedTiles ? 'Some map tiles could not load' : 'Street map unavailable');});
        layer.addTo(instance.map);
      }
      function useVectorMap() {
        if (instance.destroyed || !instance.map) return;
        clearBasemap();
        if (!global.maplibregl || !global.MaplibreGLLeaflet?.maplibreGL) {
          useStandardMap('Standard map · vector renderer unavailable'); return;
        }
        baseMode = 'vector';
        host.classList.remove('is-standard-map', 'has-map-error');
        status.hidden = true;
        host.querySelector('.jf-map-provider').innerHTML = vectorAttribution;
        host.querySelector('.jf-map-basemap-label').textContent = 'Vector street map';
        try {
          // The CSP distribution uses a same-origin worker, without blob scripts.
          global.maplibregl.setWorkerUrl('/vendor/maplibre/maplibre-gl-csp-worker.js');
          layer = global.MaplibreGLLeaflet.maplibreGL({
            style:vectorStyles[mapTheme()],attributionControl:false,interactive:false,
            renderWorldCopies:false, maxZoom:18, padding:.15,
          }).addTo(instance.map);
          vectorMap = layer.getMaplibreMap();
          let lightPaintApplied = false;
          const applyPalette = () => {
            if (instance.destroyed || baseMode !== 'vector') return;
            if (mapTheme() === 'dark') {host.dataset.jfMapPalette='dark';return;}
            if (!lightPaintApplied && styleLightMap(vectorMap)) {
              lightPaintApplied=true;host.dataset.jfMapPalette='light-refined';
            }
          };
          vectorMap.on('style.load', () => {lightPaintApplied=false;applyPalette();});
          let ready = false;
          const onReady = () => {
            if (instance.destroyed || baseMode !== 'vector') return;
            ready = true; clearTimeout(startTimer); clearTimeout(failureTimer);
            applyPalette();
            status.hidden=true;host.classList.remove('has-map-error');
            host.querySelector('.jf-map-basemap-label').textContent = 'Vector street map';
          };
          vectorMap.on('load', onReady);
          vectorMap.on('idle', onReady);
          vectorMap.on('error', () => {
            if (instance.destroyed || baseMode !== 'vector') return;
            clearTimeout(failureTimer);
            failureTimer = setTimeout(() => {
              if (!ready) useStandardMap('Standard map · vector service unavailable');
              else host.querySelector('.jf-map-basemap-label').textContent = 'Vector map · some details unavailable';
            },1200);
          });
          startTimer = setTimeout(() => {if(!ready)useStandardMap('Standard map · vector service unavailable');},15000);
        } catch (_) {
          useStandardMap('Standard map · vector renderer unavailable');
        }
      }
      function initialize() {
        if (instance.destroyed || instance.map || !canvas.clientWidth || !canvas.clientHeight) return;
        if (!global.L) {showUnavailable('Street map could not start');return;}
        const L = global.L;
        instance.map = L.map(canvas, {zoomControl:false,attributionControl:false,scrollWheelZoom:false,minZoom:2,maxZoom:18,zoomSnap:.25,zoomDelta:.5});
        const map = instance.map;
        L.control.zoom({position:'bottomright'}).addTo(map);
        L.control.scale({position:'bottomleft',imperial:true,metric:false,maxWidth:90}).addTo(map);
        instance.markers = L.layerGroup().addTo(map);
        map.on('zoomend', drawMarkers);
        const previous = viewports.get(compactKey);
        const selectedRow = data.rows.find(city => city.name === data.selectedCity);
        const selectedOffice = data.offices.find(office => office.id === data.selectedOffice && hasCoordinates(office));
        if (data.mode === 'offices' && selectedOffice && (data.scope === 'all' || inBayArea(selectedOffice))) map.setView([selectedOffice.latitude,selectedOffice.longitude],17,{animate:false});
        else if (selectedRow && previous?.selected !== data.selectedCity) map.setView([selectedRow.lat,selectedRow.lng],12,{animate:false});
        else if (previous && previous.mode === data.mode && previous.scope === data.scope) map.setView(previous.center, previous.zoom, {animate:false});
        else fitArea();
        useVectorMap();
        drawMarkers();
        instance.intersection?.disconnect();
        if (global.MutationObserver && global.document?.documentElement) {
          themeObserver = new global.MutationObserver(() => {
            if (vectorMap && baseMode === 'vector') vectorMap.setStyle(vectorStyles[mapTheme()]);
          });
          themeObserver.observe(global.document.documentElement,{attributes:true,attributeFilter:['data-theme']});
        }
      }
      updateModeChrome();
      if (data.selectedOffice) {
        const office = data.offices.find(row => row.id === data.selectedOffice);
        if (office) {selected.hidden=false;selected.querySelector('span').textContent=office.company + ' · ' + office.city + (hasCoordinates(office) ? '' : ' · address only');}
      }
      listen(search, 'focus', showSearch);
      listen(search, 'input', showSearch);
      listen(search, 'keydown', event => {if(event.key==='Escape'){hideSearch();search.blur();}else if(event.key==='ArrowDown'){event.preventDefault();results.querySelector('button')?.focus();}else if(event.key==='Enter'){event.preventDefault();const match=results.querySelector('button');if(match){if(match.dataset.jfMapCompany)chooseCompany(match.dataset.jfMapCompany);else if(match.dataset.jfMapOffice)chooseOffice(match.dataset.jfMapOffice);else chooseCity(match.dataset.jfMapCity);}}});
      listen(host, 'keydown', event => {if(event.key==='Escape')hideSearch();});
      listen(host, 'focusout', event => {if(!host.querySelector('.jf-map-search-wrap').contains(event.relatedTarget))hideSearch();});
      listen(host, 'click', event => {
        const scope=event.target.closest('[data-jf-map-scope]');
        if(scope){changeScope(scope.dataset.jfMapScope);return;}
        const company=event.target.closest('[data-jf-map-company]');
        if(company){chooseCompany(company.dataset.jfMapCompany);return;}
        const mode=event.target.closest('[data-jf-map-mode]');
        if(mode){changeMode(mode.dataset.jfMapMode);return;}
        const office=event.target.closest('[data-jf-map-office]');
        if(office){chooseOffice(office.dataset.jfMapOffice);return;}
        const button=event.target.closest('[data-jf-map-city]');
        if(button){chooseCity(button.dataset.jfMapCity);return;}
        if(!event.target.closest('.jf-map-search-wrap'))hideSearch();
      });
      listen(canvas, 'pointerdown', () => {maintainFit=false;});
      listen(canvas, 'keydown', () => {maintainFit=false;});
      listen(host.querySelector('.jf-map-fit'), 'click', () => {maintainFit=true;fitArea();});
      listen(selected.querySelector('button'), 'click', () => {data.selectedOffice='';markSelected('');drawMarkers();options.onSelect?.('');});
      listen(host.querySelector('.jf-map-retry'), 'click', () => {if(instance.map)useVectorMap();else initialize();});
      if (global.ResizeObserver) {
        instance.resize = new global.ResizeObserver(() => {
          const size = canvas.clientWidth + 'x' + canvas.clientHeight;
          if (instance.map && measuredSize !== size) {
            measuredSize = size;
            instance.map.invalidateSize({pan:false});
            if (maintainFit) fitArea();
          }
        });
        instance.resize.observe(canvas);
      }
      if (global.IntersectionObserver) {
        instance.intersection = new global.IntersectionObserver(entries => {if(entries.some(entry=>entry.isIntersecting))initialize();});
        instance.intersection.observe(canvas);
      } else initialize();
      // ResizeObserver handles dimensions; details toggles make closed overview
      // sections initialize correctly even in browsers without IntersectionObserver.
      for(let node=host.parentElement;node;node=node.parentElement){if(node.tagName==='DETAILS')listen(node,'toggle',()=>{if(node.open){initialize();instance.map?.invalidateSize({pan:false});}});}
      instance.cleanup = () => {
        instance.destroyed=true;
        clearTimeout(startTimer); clearTimeout(failureTimer); themeObserver?.disconnect();
        if(instance.map) {
          viewports.set(compactKey,{center:instance.map.getCenter(),zoom:instance.map.getZoom(),selected:data.selectedCity,mode:data.mode,scope:data.scope});
          instance.map.remove();
        }
        instance.resize?.disconnect();instance.intersection?.disconnect();
        instance.handlers.forEach(remove=>remove());
        delete host.dataset.jfMapMounted;
      };
    });
  }
  function destroy() {instances.forEach(instance=>instance.cleanup());instances.clear();pending.clear();}
  function focusOffice(id) {return [...instances].some(instance => instance.focusOffice?.(id));}
  function focusCompany(name) {return [...instances].some(instance => instance.focusCompany?.(name));}
  function clearSelection() {instances.forEach(instance => instance.clearSelection?.());}
  global.JobflowMaps = {cities,inCity,inBayArea,hasCoordinates,officeRows,officeSites,companyRows,coverageLabels,render,mount,destroy,focusOffice,focusCompany,clearSelection};
})(window);

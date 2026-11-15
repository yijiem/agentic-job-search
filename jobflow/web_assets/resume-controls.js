/* Progressive, keyboard-accessible controls backed by the original form fields. */
(function () {
  'use strict';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const icon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z"/><path d="M14 3v6h6M8 13h8M8 17h5"/></svg>';
  const uploadIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M12 16V4m-4 4 4-4 4 4M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/></svg>';
  const chevron = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="m6 8 4 4 4-4"/></svg>';
  const check = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="m4 10 4 4 8-8"/></svg>';
  const controls = new Map(), uploads = new Map();
  let active = null, sequence = 0, observer = null, typeahead = '', typeaheadAt = 0;
  const disabled = node => node.disabled || node.matches(':disabled');
  function optionData(option, index) {
    const data = option.dataset || {}, title = data.title || option.textContent || '';
    const extension = (data.filename || '').split('.').pop();
    return {index, value:option.value, title, filename:data.filename || '', type:data.type || (extension && ['pdf','docx','txt','md'].includes(extension.toLowerCase()) ? extension.toUpperCase() : ''), size:data.size || '', isDefault:data.default === 'true', version:data.version || '', disabled:option.disabled || Boolean(option.parentElement?.disabled), empty:!option.value};
  }
  const metadata = option => [option.type, option.version ? 'Version ' + option.version : '', option.size].filter(Boolean).join(' · ');
  function rowContent(option) {
    return `<span class="resume-control-icon${option.empty?' is-placeholder':''}">${icon}</span><span class="resume-control-copy"><span class="resume-control-title">${escape(option.title)}${option.isDefault?'<span class="resume-control-default">Default</span>':''}</span><span class="resume-control-meta">${escape(metadata(option) || (option.empty ? (/batch/i.test(option.title) ? 'Inherit the batch selection' : 'Choose from your library') : option.filename || 'Saved in your library'))}</span></span>`;
  }
  function labelText(select) { return Array.from(select.labels || []).map(label => label.textContent.trim()).filter(Boolean).join(' ') || select.getAttribute('aria-label') || 'Resume'; }
  function visibleOptions(control) {
    const query = (control.search?.value || '').trim().toLocaleLowerCase();
    return control.options.filter(option => !query || [option.title,option.filename,option.type,option.version].join(' ').toLocaleLowerCase().includes(query));
  }
  function sync(control) {
    const select = control.select;
    control.options = Array.from(select.options).map(optionData);
    const option = control.options[select.selectedIndex] || {title:'Choose a resume',empty:true};
    const signature = JSON.stringify([option,disabled(select),select.required,labelText(select)]);
    if(signature !== control.signature) {
      control.signature = signature;
      control.trigger.innerHTML = rowContent(option) + '<span class="resume-control-chevron">' + chevron + '</span>';
      control.trigger.disabled = disabled(select);
      control.trigger.setAttribute('aria-label', labelText(select) + ': ' + option.title);
      control.trigger.setAttribute('aria-required', String(select.required));
      control.trigger.classList.toggle('is-placeholder', Boolean(option.empty));
    }
    if(active === control) { if(disabled(select)) close(control); else renderOptions(control); }
  }
  function position(control) {
    if(!control.trigger.isConnected) { close(control); return; }
    const rect = control.trigger.getBoundingClientRect(), gap = 8, edge = 12;
    if(rect.bottom<0||rect.top>window.innerHeight){close(control);return;}
    const width = Math.min(Math.max(rect.width, 300), window.innerWidth - edge * 2);
    const roomBelow = window.innerHeight - rect.bottom - gap - edge, roomAbove = rect.top - gap - edge;
    const above = roomBelow < 260 && roomAbove > roomBelow;
    const maxHeight = Math.max(150, Math.min(410, above ? roomAbove : roomBelow));
    control.popup.style.width = width + 'px';
    control.popup.style.maxHeight = maxHeight + 'px';
    control.popup.style.left = Math.max(edge, Math.min(rect.left, window.innerWidth - width - edge)) + 'px';
    control.popup.style.top = above ? Math.max(edge, rect.top - gap - Math.min(control.popup.scrollHeight,maxHeight)) + 'px' : rect.bottom + gap + 'px';
    control.popup.classList.toggle('opens-above',above);
  }
  function setActive(control,index,scroll=true) {
    control.activeIndex = index;
    const id = index >= 0 ? control.id + '-option-' + index : '';
    for(const element of control.list.querySelectorAll('[role="option"]')) element.classList.toggle('is-active', Number(element.dataset.optionIndex) === index);
    for(const target of [control.trigger,control.search].filter(Boolean)) {
      if(id) target.setAttribute('aria-activedescendant',id); else target.removeAttribute('aria-activedescendant');
    }
    if(scroll && id) document.getElementById(id)?.scrollIntoView({block:'nearest'});
  }
  function renderOptions(control) {
    const rows = visibleOptions(control);
    control.list.innerHTML = rows.map(option => `<div role="option" id="${escape(control.id)}-option-${option.index}" data-option-index="${option.index}" aria-selected="${option.value===control.select.value}"${option.disabled?' aria-disabled="true"':''} class="resume-control-option${option.value===control.select.value?' is-selected':''}">${rowContent(option)}<span class="resume-control-check">${option.value===control.select.value?check:''}</span></div>`).join('') || '<div class="resume-control-empty" role="status">No matching resumes</div>';
    const eligible = rows.filter(option => !option.disabled);
    const current = eligible.find(option => option.index === control.activeIndex) || eligible.find(option => option.value === control.select.value) || eligible[0];
    setActive(control,current?.index ?? -1,false);
    control.count.textContent = rows.filter(option=>!option.empty).length + ' saved version' + (rows.filter(option=>!option.empty).length===1?'':'s');
    position(control);
  }
  function open(control,edge) {
    if(disabled(control.select)) return;
    if(active && active !== control) close(active);
    control.options = Array.from(control.select.options).map(optionData);
    control.search.value = '';
    control.searchWrap.hidden = control.options.filter(option=>!option.empty).length <= 5;
    control.activeIndex = control.select.selectedIndex;
    active = control;
    control.popup.hidden = false;
    if(typeof control.popup.showPopover === 'function') control.popup.showPopover();
    control.trigger.setAttribute('aria-expanded','true');
    control.wrapper.classList.add('is-open');
    renderOptions(control);
    const rows = visibleOptions(control).filter(option=>!option.disabled);
    if(edge === 'first') setActive(control,rows[0]?.index ?? -1);
    if(edge === 'last') setActive(control,rows[rows.length-1]?.index ?? -1);
    if(!control.searchWrap.hidden) control.search.focus();
  }
  function close(control,focus=false) {
    if(!control) return;
    if(typeof control.popup.hidePopover === 'function' && control.popup.matches(':popover-open')) control.popup.hidePopover();
    control.popup.hidden = true;
    control.trigger.setAttribute('aria-expanded','false');
    control.trigger.removeAttribute('aria-activedescendant');
    control.wrapper.classList.remove('is-open');
    if(active === control) active = null;
    if(focus && control.trigger.isConnected && !disabled(control.select)) control.trigger.focus();
  }
  function choose(control,index) {
    const option = control.options.find(row=>row.index===index);
    if(!option || option.disabled || disabled(control.select)) return;
    const changed = control.select.value !== option.value;
    control.select.value = option.value;
    close(control,true);sync(control);
    if(changed) control.select.dispatchEvent(new Event('change',{bubbles:true}));
    const replacement=document.getElementById(control.id);if(replacement&&replacement!==control.trigger&&document.activeElement===document.body)replacement.focus();
  }
  function keyboard(control,event) {
    if(disabled(control.select)) return;
    const key = event.key, isSearch = event.target === control.search;
    if(key === 'Tab') { close(control,isSearch); return; }
    if(key === 'Escape') { if(active===control){event.preventDefault();event.stopPropagation();close(control,true);} return; }
    if(active !== control) {
      if(['ArrowDown','ArrowUp','Enter',' '].includes(key)) { event.preventDefault();open(control,key==='ArrowUp'?'last':undefined); }
      return;
    }
    const rows = visibleOptions(control).filter(option=>!option.disabled);
    let index = rows.findIndex(option=>option.index===control.activeIndex);
    if(['ArrowDown','ArrowUp','Home','End'].includes(key) && (!isSearch || key.startsWith('Arrow') || event.ctrlKey)) {
      event.preventDefault();index = key==='Home'?0:key==='End'?rows.length-1:key==='ArrowDown'?Math.min(rows.length-1,index+1):Math.max(0,index-1);setActive(control,rows[index]?.index ?? -1);return;
    }
    if(key==='Enter'||key===' '&&!isSearch) { event.preventDefault();choose(control,control.activeIndex);return; }
    if(!isSearch && key.length===1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();const now=Date.now();typeahead=now-typeaheadAt>700?key:typeahead+key;typeaheadAt=now;
      const match=rows.find(option=>option.title.toLocaleLowerCase().startsWith(typeahead.toLocaleLowerCase()));if(match)setActive(control,match.index);
    }
  }
  function enhanceSelect(select) {
    if(controls.has(select)||select.multiple) return;
    const id = (select.id || 'resume-select-'+(++sequence)) + '-picker';
    if(!select.id) select.id=id+'-native';
    const wrapper=document.createElement('div');wrapper.className='resume-picker';select.before(wrapper);wrapper.appendChild(select);
    const trigger=document.createElement('button');trigger.type='button';trigger.className='resume-picker-trigger';trigger.id=id;trigger.setAttribute('role','combobox');trigger.setAttribute('aria-haspopup','listbox');trigger.setAttribute('aria-expanded','false');trigger.setAttribute('aria-controls',id+'-list');wrapper.appendChild(trigger);
    const popup=document.createElement('div');popup.className='resume-picker-popover';popup.id=id+'-popover';popup.hidden=true;if('showPopover' in popup)popup.setAttribute('popover','manual');
    popup.innerHTML=`<div class="resume-control-menu-head"><span>Your resume library</span><span class="resume-control-count"></span></div><div class="resume-control-search-wrap"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><circle cx="8.5" cy="8.5" r="5.5"/><path d="m13 13 4 4"/></svg><input type="search" class="resume-control-search" autocomplete="off" placeholder="Search resumes…" aria-label="Search saved resumes" role="combobox" aria-expanded="true" aria-controls="${escape(id)}-list" aria-autocomplete="list"></div><div id="${escape(id)}-list" class="resume-control-options" role="listbox" aria-label="${escape(labelText(select))}"></div>`;
    document.body.appendChild(popup);
    const control={select,wrapper,trigger,popup,id,list:popup.querySelector('.resume-control-options'),search:popup.querySelector('input'),searchWrap:popup.querySelector('.resume-control-search-wrap'),count:popup.querySelector('.resume-control-count'),options:[],activeIndex:-1,signature:''};
    controls.set(select,control);select.classList.add('resume-native-select');select.setAttribute('aria-hidden','true');select.tabIndex=-1;
    trigger.addEventListener('click',()=>active===control?close(control,true):open(control));
    trigger.addEventListener('keydown',event=>keyboard(control,event));
    popup.addEventListener('keydown',event=>keyboard(control,event));
    control.search.addEventListener('input',()=>renderOptions(control));
    popup.addEventListener('pointerdown',event=>{if(event.target.closest('[role="option"]'))event.preventDefault();});
    popup.addEventListener('click',event=>{const option=event.target.closest('[data-option-index]');if(option)choose(control,Number(option.dataset.optionIndex));});
    popup.addEventListener('pointermove',event=>{const option=event.target.closest('[data-option-index]');if(option&&option.getAttribute('aria-disabled')!=='true')setActive(control,Number(option.dataset.optionIndex),false);});
    select.addEventListener('change',()=>sync(control));
    select.addEventListener('invalid',event=>{event.preventDefault();trigger.setAttribute('aria-invalid','true');open(control);trigger.focus();});
    select.addEventListener('change',()=>trigger.removeAttribute('aria-invalid'));
    for(const label of select.labels||[])label.addEventListener('click',event=>{event.preventDefault();trigger.focus();});
    sync(control);
  }
  function uploadSummary(files,multiple) {
    const names=Array.from(files||[]).map(file=>file.name);
    return {title:names.length===1?names[0]:names.length?names.length+' resumes selected':multiple?'Add resumes':'Add a resume',detail:names.length>1?names.join(', '):names.length?'Ready to upload · choose a different file':'Browse your files'};
  }
  function syncUpload(control) {
    const {input,wrapper,content,hint} = control, selected=uploadSummary(input.files,input.multiple);
    const signature=JSON.stringify([selected,disabled(input),input.dataset.uploadTitle,input.dataset.uploadHint]);if(signature===control.signature)return;control.signature=signature;
    const title=input.files?.length?selected.title:input.dataset.uploadTitle||selected.title;
    content.innerHTML=`<span class="resume-upload-icon">${uploadIcon}</span><span class="resume-upload-copy"><strong>${escape(title)}</strong><span>${escape(selected.detail)}</span></span><span class="resume-upload-plus" aria-hidden="true">+</span>`;
    hint.textContent=input.dataset.uploadHint || 'PDF, DOCX or TXT · up to 5 MB';
    wrapper.classList.toggle('has-files',Boolean(input.files?.length));wrapper.classList.toggle('is-disabled',disabled(input));
  }
  function enhanceUpload(input) {
    if(uploads.has(input))return;
    const wrapper=document.createElement('div');wrapper.className='resume-upload-control';input.before(wrapper);wrapper.appendChild(input);input.classList.add('resume-native-upload');
    const content=document.createElement('div');content.className='resume-upload-content';content.setAttribute('aria-hidden','true');wrapper.appendChild(content);
    const hint=document.createElement('span');hint.className='resume-upload-hint';hint.id=(input.id||'resume-upload-'+(++sequence))+'-hint';wrapper.appendChild(hint);
    const description=input.getAttribute('aria-describedby');input.setAttribute('aria-describedby',[description,hint.id].filter(Boolean).join(' '));
    const status=document.createElement('span');status.className='sr-only';status.setAttribute('role','status');wrapper.appendChild(status);
    const control={input,wrapper,content,hint,status,signature:''};uploads.set(input,control);
    input.addEventListener('change',()=>queueMicrotask(()=>{if(!input.isConnected)return;syncUpload(control);status.textContent=input.files?.length?uploadSummary(input.files,input.multiple).title:'';}));
    input.form?.addEventListener('reset',()=>queueMicrotask(()=>syncUpload(control)));
    syncUpload(control);
  }
  function mount(root=document) {
    for(const [select,control] of controls)if(!select.isConnected){close(control);control.popup.remove();controls.delete(select);}
    for(const [input] of uploads)if(!input.isConnected)uploads.delete(input);
    if(root.matches?.('select[data-resume-picker]'))enhanceSelect(root);
    if(root.matches?.('input[type="file"][data-resume-upload]'))enhanceUpload(root);
    root.querySelectorAll?.('select[data-resume-picker]').forEach(enhanceSelect);
    root.querySelectorAll?.('input[type="file"][data-resume-upload]').forEach(enhanceUpload);
  }
  function start() {
    mount();
    document.addEventListener('pointerdown',event=>{if(active&&!active.wrapper.contains(event.target)&&!active.popup.contains(event.target))close(active);});
    document.addEventListener('focusin',event=>{if(active&&!active.wrapper.contains(event.target)&&!active.popup.contains(event.target))close(active);});
    document.addEventListener('scroll',event=>{if(active&&!active.popup.contains(event.target))position(active);},true);
    window.addEventListener('resize',()=>{if(active)position(active);});
    observer=new MutationObserver(records=>{
      const dirty=new Set();
      for(const record of records){
        if(record.type==='childList')for(const added of record.addedNodes)if(added.nodeType===1)mount(added);
        const select=record.target.closest?.('select[data-resume-picker]');if(select&&controls.has(select))dirty.add(controls.get(select));
        if(record.type==='attributes'&&record.target.matches?.('fieldset')){for(const select of record.target.querySelectorAll('select[data-resume-picker]'))if(controls.has(select))dirty.add(controls.get(select));for(const input of record.target.querySelectorAll('input[data-resume-upload]'))if(uploads.has(input))syncUpload(uploads.get(input));}
        const upload=uploads.get(record.target);if(upload)syncUpload(upload);
      }
      for(const control of dirty)sync(control);
      for(const [select,control] of controls)if(!select.isConnected){close(control);control.popup.remove();controls.delete(select);}
      for(const [input] of uploads)if(!input.isConnected)uploads.delete(input);
    });
    observer.observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['disabled','selected','required','data-title','data-filename','data-type','data-size','data-default','data-version']});
  }
  if(typeof module!=='undefined'&&module.exports)module.exports={escape,optionData,metadata,rowContent,uploadSummary};
  if(typeof window!=='undefined'&&typeof document!=='undefined'){
    window.JobflowResumeControls={mount,close:()=>close(active)};
    if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
  }
})();

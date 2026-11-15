/* Local PDF rendering and anchored edits. Original files are never modified. */
(function () {
  'use strict';
  const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const state={dialog:null,options:null,library:null,resume:null,jobId:'',pdf:null,pdfBytes:null,document:null,review:null,pages:[],sourceLinks:[],scale:1,activePage:1,mode:'original',previewPdf:null,previewBytes:null,selection:null,editingId:null,busy:false,error:'',notice:'',confirmed:false,setDefault:false,label:'',generation:0,pdfjs:null,rendering:false,noteTool:false,editorDirty:false,closeRequested:false};
  const find=selector=>state.dialog?.querySelector(selector);
  const action=(label,op,extra='',primary=false)=>`<button type="button" class="button${primary?' primary':''}" data-pdf-action="${op}" ${extra}${state.busy||state.rendering?' disabled':''}>${label}</button>`;
  const safeLink=url=>{try{const u=new URL(url);return ['http:','https:'].includes(u.protocol)?u.href:'';}catch{return '';}};
  function exportLink(value){try{const url=new URL(value);return ['http:','https:','mailto:'].includes(url.protocol)&&!url.username&&!url.password?url.href:'';}catch{return '';}}
  const clamp=(n,min=0,max=1)=>Math.max(min,Math.min(max,n));
  function applyEdits(text,annotations){
    const edits=annotations.filter(a=>a.kind==='replace'&&a.status==='accepted').sort((a,b)=>a.start-b.start||a.end-b.end);
    let cursor=0,result='';
    for(const edit of edits){if(!Number.isInteger(edit.start)||!Number.isInteger(edit.end)||edit.start<cursor||edit.end<=edit.start||text.slice(edit.start,edit.end)!==edit.before)throw new Error('Some edits overlap or no longer match the source. Review them before saving.');result+=text.slice(cursor,edit.start)+edit.after;cursor=edit.end;}
    return result+text.slice(cursor);
  }
  function countChanges(annotations){return annotations.reduce((counts,a)=>{if(a.kind==='replace')counts[a.status]=(counts[a.status]||0)+1;else counts.notes++;return counts;},{pending:0,accepted:0,rejected:0,notes:0});}
  function normalizedRects(rects,pageRect){return rects.map(r=>[clamp((r.left-pageRect.left)/pageRect.width),clamp((r.top-pageRect.top)/pageRect.height),clamp(r.width/pageRect.width),clamp(r.height/pageRect.height)]).filter(r=>r[2]>0&&r[3]>0);}
  function bytesBase64(bytes){let out='';for(let offset=0;offset<bytes.length;offset+=0x8000)out+=String.fromCharCode(...bytes.subarray(offset,offset+0x8000));return btoa(out);}
  async function request(path,payload){
    const options={credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'}};
    if(payload){options.method='POST';options.headers['Content-Type']='application/json';options.headers['X-CSRF-Token']=state.library?.csrf_token||state.options?.csrfToken||'';options.body=JSON.stringify({action:'batch',...payload,workspace_id:state.library?.workspace_id||state.options?.workspaceId});}
    const response=await fetch(path,options);let body;
    try{body=await response.json();}catch{throw new Error('The PDF workspace could not be reached. Try again.');}
    if(!response.ok||body.error)throw new Error(body.error||'The PDF review could not be saved.');return body;
  }
  function setMessage(text,isError=false){state.error=isError?text:'';state.notice=isError?'':text;renderSidebar();renderToolbar();}
  function checkActive(generation){if(generation!==state.generation||!state.dialog?.open)throw new Error('PDF review closed.');}
  function dialogShell(){return `<header class="pdf-review-header"><div><span class="pdf-review-eyebrow">Resume review</span><h2 id="pdf-review-title">${esc(state.resume?.label||'Opening your PDF…')}</h2><p id="pdf-review-subtitle">Select text to suggest an edit, then review and save a separate revision.</p></div><button class="icon-button" type="button" data-pdf-action="close" aria-label="Close PDF review">×</button></header><div id="pdf-review-toolbar" class="pdf-review-toolbar"></div><div class="pdf-review-body"><section class="pdf-review-viewport" aria-label="PDF document"><div id="pdf-review-pages" class="pdf-review-pages"><div class="pdf-review-loading"><span class="spinner"></span>Rendering your PDF…</div></div></section><aside id="pdf-review-sidebar" class="pdf-review-sidebar" aria-label="Edits and suggestions"></aside></div>`;}
  function renderToolbar(){
    const toolbar=find('#pdf-review-toolbar');if(!toolbar)return;
    const count=state.mode==='preview'?state.previewPdf?.numPages:state.pdf?.numPages;
    toolbar.innerHTML=`<div class="pdf-review-tabs" role="tablist" aria-label="PDF view"><button type="button" role="tab" aria-selected="${state.mode==='original'}" data-pdf-action="original">Original & annotations</button><button type="button" role="tab" aria-selected="${state.mode==='preview'}" data-pdf-action="preview"${state.busy||!state.review?' disabled':''}>Accepted edits preview</button></div><div class="pdf-review-page-tools">${action('−','zoom-out','aria-label="Zoom out"')}<span aria-label="Zoom level">${Math.round(state.scale*100)}%</span>${action('+','zoom-in','aria-label="Zoom in"')}${action('Fit','fit','aria-label="Fit PDF to width"')}${state.mode==='original'&&state.review?.status==='draft'?action(state.noteTool?'Cancel note':'Page note','page-note','aria-label="Add a page comment"'):''}<label for="pdf-review-page">Page</label><select id="pdf-review-page" aria-label="Go to PDF page">${Array.from({length:count||1},(_,i)=>`<option value="${i+1}"${state.activePage===i+1?' selected':''}>${i+1} / ${count||1}</option>`).join('')}</select><a class="button" href="/api/application-resumes/${encodeURIComponent(state.options.resumeId)}" target="_blank" rel="noopener noreferrer">Original file ↗</a></div>`;
  }
  function selectionEditor(){
    const selection=state.selection;if(!selection)return `<div class="pdf-selection-help"><span aria-hidden="true">↖</span><h3>Select text on the PDF</h3><p>Highlight a sentence to replace it or add a comment. Your notes and decisions are saved in this private review.</p></div>`;
    return `<section class="pdf-annotation-editor"><div class="pdf-card-head"><h3>${state.editingId?'Edit annotation':'Annotate selection'}</h3>${action('Cancel','cancel-selection')}</div><p class="pdf-selection-page">Page ${selection.page}</p><blockquote>${esc(selection.before||'Page note')}</blockquote><form id="pdf-annotation-form"><label for="pdf-annotation-kind">Annotation type</label><select id="pdf-annotation-kind"><option value="replace"${selection.kind==='replace'?' selected':''}${!selection.before?' disabled':''}>Replace selected text</option><option value="note"${selection.kind==='note'?' selected':''}>Comment only</option></select><label for="pdf-annotation-after">Suggested replacement</label><textarea id="pdf-annotation-after" rows="5" maxlength="20000"${selection.kind==='note'?' disabled':''}>${esc(selection.after??selection.before)}</textarea><label for="pdf-annotation-note">Comment or explanation</label><textarea id="pdf-annotation-note" rows="2" maxlength="4000">${esc(selection.note||'')}</textarea><button type="submit" class="button primary"${state.busy||state.rendering?' disabled':''}>${state.editingId?'Save annotation':'Add annotation'}</button></form></section>`;
  }
  function annotationCard(annotation,index){
    const current=state.editingId===annotation.id;
    const origin=annotation.source==='ai'?'AI suggestion':annotation.source==='local'?'Role evidence':'Your annotation';
    const status=annotation.kind==='note'?'Comment':annotation.status==='accepted'?'Accepted':annotation.status==='rejected'?'Rejected':'Review';
    const sources=Array.isArray(annotation.evidence)?annotation.evidence:[];
    return `<article class="pdf-annotation-card ${esc(annotation.status)}${current?' is-active':''}" data-pdf-card="${esc(annotation.id)}"><div class="pdf-card-head"><button type="button" class="pdf-anchor-link" data-pdf-action="focus" data-annotation-id="${esc(annotation.id)}">${index+1} · Page ${annotation.page}</button><span class="pdf-review-badge">${esc(status)}</span></div><span class="pdf-annotation-source">${esc(origin)}</span>${annotation.kind==='replace'?`<div class="pdf-edit-diff"><del>${esc(annotation.before)}</del><ins>${esc(annotation.after||'[Remove selected text]')}</ins></div>`:`<blockquote>${esc(annotation.before)}</blockquote>`}${annotation.note?`<p>${esc(annotation.note)}</p>`:''}${annotation.reason?`<p class="pdf-edit-reason">${esc(annotation.reason)}</p>`:''}${sources.length?`<details><summary>Supporting evidence</summary><ul>${sources.map(s=>{const url=safeLink(typeof s==='object'?s.url:'');return `<li>${url?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(s.title||s.label||url)}</a>`:esc(typeof s==='string'?s:s.text||s.label||s.title||'Saved resume')}</li>`;}).join('')}</ul></details>`:''}<div class="pdf-card-actions">${annotation.kind==='replace'&&annotation.status!=='accepted'?action('Accept','accept',`data-annotation-id="${esc(annotation.id)}"`):''}${annotation.kind==='replace'&&annotation.status!=='rejected'?action('Reject','reject',`data-annotation-id="${esc(annotation.id)}"`):''}${action('Edit','edit',`data-annotation-id="${esc(annotation.id)}"`)}${action('Remove','remove',`data-annotation-id="${esc(annotation.id)}"`)}</div></article>`;
  }
  function renderSidebar(){
    const sidebar=find('#pdf-review-sidebar');if(!sidebar)return;
    const review=state.review,counts=countChanges(review?.annotations||[]),ai=state.library?.pdf_ai||state.library?.resume_ai||{};
    const error=state.error?`<p class="pdf-review-error" role="alert">${esc(state.error)} ${review?action('Refresh review','reload'):''}</p>`:'';
    const notice=state.notice?`<p class="pdf-review-notice" role="status">${esc(state.notice)}</p>`:'';
    const closePrompt=state.closeRequested?`<section class="pdf-annotation-editor"><h3>Keep your unsaved annotation?</h3><p>Save the annotation before closing, or discard the text you have not saved.</p><div class="pdf-card-actions">${action('Keep editing','keep-editing')}${action('Discard & close','discard-close')}</div></section>`:'';
    if(!review){sidebar.innerHTML=`${error}${notice}<div class="pdf-selection-help"><h3>View, annotate, revise</h3><p>Your PDF is rendered locally. Proposed edits stay separate from the original.</p></div>`;return;}
    if(review.status==='saved'){sidebar.innerHTML=`${error}${notice}<div class="pdf-saved-result"><span class="pdf-review-badge">Revision saved</span><h3>Your new PDF is ready</h3><p>It is a separate version in your resume library and can be selected for future applications.</p><a class="button primary" href="/api/application-resumes/${encodeURIComponent(review.saved_resume_id)}" target="_blank" rel="noopener noreferrer">Open saved revision ↗</a>${action('Return to library','close')}</div>`;return;}
    const roleTitle=state.options.jobTitle||[review.company,review.job_title].filter(Boolean).join(' · ')||'Selected application role';
    const tailoring=state.jobId?`<section class="pdf-suggestion-tools"><span class="pdf-review-eyebrow">Tailor for your application</span><p>${esc(roleTitle)}</p><div class="pdf-suggest-buttons">${action('Suggest from role evidence','suggest-local')}${action('Generate AI suggestions','suggest-ai',!ai.ai_available?'disabled':'')}</div><p class="pdf-review-hint">${ai.ai_available?'AI suggestions are drafts grounded in your resume. Review every claim before accepting.':esc(ai.ai_reason||'Role suggestions use your saved text and the recorded job. AI rewriting is not connected for this workspace.')}</p></section>`:'<section class="pdf-suggestion-tools"><span class="pdf-review-eyebrow">General resume review</span><p>Edit and comment on your saved resume.</p></section>';
    sidebar.innerHTML=`${error}${notice}${closePrompt}${tailoring}${selectionEditor()}<section class="pdf-annotations"><div class="pdf-card-head"><h3>Edits & comments</h3><span>${counts.accepted} accepted · ${counts.pending} to review</span></div>${review.annotations.length?review.annotations.map(annotationCard).join(''):'<p class="pdf-review-hint">Your annotations and proposed changes will appear here, linked to the source text.</p>'}</section><form id="pdf-save-revision" class="pdf-save-revision"><h3>Save a separate revision</h3><p>Accepted edits are typeset into a clean PDF. The layout may differ from the original. Replaced text is removed from the new file.</p><label for="pdf-revision-label">Version name</label><input id="pdf-revision-label" maxlength="200" required value="${esc(state.label)}"><label class="pdf-review-check"><input id="pdf-revision-confirm" type="checkbox"${state.confirmed?' checked':''}> I reviewed the accepted edits and the new PDF preview.</label><label class="pdf-review-check"><input id="pdf-revision-default" type="checkbox"${state.setDefault?' checked':''}> Use this version by default</label>${state.editorDirty?'<p class="pdf-review-hint">Save or cancel your annotation before saving a PDF revision.</p>':''}${!counts.accepted?'<p class="pdf-review-hint">Accept at least one text edit to create a revision. Comments are saved separately in this review.</p>':''}${counts.pending?`<p class="pdf-review-hint">Accept or reject ${counts.pending} pending edit${counts.pending===1?'':'s'} before saving.</p>`:''}${action('Preview accepted edits','preview')}<button type="submit" class="button primary"${state.busy||state.editorDirty||!state.confirmed||counts.pending||!counts.accepted||!state.previewBytes?' disabled':''}>Confirm & save PDF revision</button></form>`;
  }
  function pdfOptions(data){return {data,isEvalSupported:false,enableXfa:false,useSystemFonts:true,useWasm:false,standardFontDataUrl:'/vendor/pdfjs/standard_fonts/',cMapUrl:'/vendor/pdfjs/cmaps/',cMapPacked:true};}
  function loadScript(src,globalName){if(window[globalName])return Promise.resolve();return new Promise((resolve,reject)=>{const script=document.createElement('script');script.src=src;script.onload=()=>window[globalName]?resolve():reject(new Error('The PDF exporter did not initialize.'));script.onerror=()=>{script.remove();reject(new Error('The PDF exporter could not be loaded. Reload the app and try again.'));};document.head.appendChild(script);});}
  async function loadExportLibraries(){await loadScript('/vendor/pdf-lib/pdf-lib.min.js','PDFLib');await loadScript('/vendor/pdf-lib/fontkit.umd.min.js','fontkit');}
  function showAIDisclosure(){const sidebar=find('#pdf-review-sidebar');if(!sidebar)return;let disclosure=find('#pdf-ai-disclosure');if(disclosure)disclosure.remove();disclosure=document.createElement('section');disclosure.id='pdf-ai-disclosure';disclosure.className='pdf-annotation-editor';disclosure.innerHTML=`<h3>Generate AI suggestions</h3><p>${esc(state.library.pdf_ai.disclosure||'This sends the resume text and the selected job description to OpenAI. API usage may incur charges.')}</p><p class="pdf-review-hint">Suggested wording stays pending until you accept it.</p>${action('Send text & generate suggestions','confirm-ai','',true)}`;sidebar.prepend(disclosure);disclosure.scrollIntoView({block:'nearest'});}
  async function loadPdfJs(){if(!state.pdfjs){state.pdfjs=await import('/vendor/pdfjs/pdf.mjs');state.pdfjs.GlobalWorkerOptions.workerSrc='/vendor/pdfjs/pdf.worker.mjs';}return state.pdfjs;}
  function pageItems(content,viewport,offset,pageNumber){
    const items=[],lines=[];let text='',previous=null;
    for(const item of content.items){if(typeof item.str!=='string'||!item.str)continue;
      if(previous){const sameLine=Math.abs(previous.transform[5]-item.transform[5])<Math.max(2,Math.abs(item.transform[3])*.25);const separator=previous.hasEOL||!sameLine?'\n':/\s$/.test(previous.str)||/^\s/.test(item.str)?'':' ';text+=separator;}
      const start=offset+text.length;text+=item.str;const end=offset+text.length;
      const tx=state.pdfjs.Util.transform(viewport.transform,item.transform),fontHeight=Math.hypot(tx[2],tx[3]);const style=content.styles[item.fontName]||{},ascent=style.ascent??(style.descent?1+style.descent:.8);
      const x=tx[4],y=tx[5]-fontHeight*ascent,width=Math.abs(item.width*viewport.scale),height=fontHeight;
      const rect=[clamp(x/viewport.width),clamp(y/viewport.height),clamp(width/viewport.width),clamp(height/viewport.height)];
      const line={page:pageNumber,start,end,text:item.str,rect};lines.push(line);items.push({...line,fontFamily:style.fontFamily||'sans-serif',angle:Math.atan2(tx[1],tx[0]),fontHeight,ascent});previous=item;
    }
    return {text,items,lines};
  }
  async function extractDocument(pdf,generation){
    const document={text:'',pages:[],lines:[]},pages=[],links=[];
    if(pdf.numPages>30)throw new Error('The PDF reviewer supports resumes of up to 30 pages.');
    for(let number=1;number<=pdf.numPages;number++){
      checkActive(generation);const page=await pdf.getPage(number),viewport=page.getViewport({scale:1}),content=await page.getTextContent();
      if(number>1)document.text+='\n\n';const result=pageItems(content,viewport,document.text.length,number);
      const annotations=await page.getAnnotations({intent:'display'});for(const annotation of annotations){if(annotation.subtype!=='Link'||!Array.isArray(annotation.rect))continue;const url=exportLink(annotation.url||annotation.unsafeUrl);if(!url)continue;const bounds=[...viewport.convertToViewportPoint(annotation.rect[0],annotation.rect[1]),...viewport.convertToViewportPoint(annotation.rect[2],annotation.rect[3])],left=Math.min(bounds[0],bounds[2]),right=Math.max(bounds[0],bounds[2]),top=Math.min(bounds[1],bounds[3]),bottom=Math.max(bounds[1],bounds[3]);const matching=result.items.filter(item=>{const x=(item.rect[0]+item.rect[2]/2)*viewport.width,y=(item.rect[1]+item.rect[3]/2)*viewport.height;return x>=left&&x<=right&&y>=top&&y<=bottom;});const label=matching.map(item=>item.text).join(' ').trim();if(label)links.push({label,url});}
      document.text+=result.text;document.pages.push({number,width:viewport.width,height:viewport.height});document.lines.push(...result.lines);pages.push({number,page,viewport,items:result.items});
      if(document.text.length>200000)throw new Error('This document has too much text for a resume review.');
    }
    return {document,pages,links:links.filter(link=>document.text.indexOf(link.label)===document.text.lastIndexOf(link.label))};
  }
  async function renderPages(){
    const generation=state.generation,container=find('#pdf-review-pages');if(!container||state.rendering)return;
    state.rendering=true;renderToolbar();try{container.innerHTML='';const pdf=state.mode==='preview'?state.previewPdf:state.pdf;if(!pdf)return;
    const scale=state.scale,pixelRatio=Math.min(window.devicePixelRatio||1,2);
    for(let number=1;number<=pdf.numPages;number++){
      checkActive(generation);const page=await pdf.getPage(number),viewport=page.getViewport({scale});
      const wrapper=document.createElement('div');wrapper.className='pdf-page';wrapper.dataset.page=String(number);wrapper.style.width=viewport.width+'px';wrapper.style.height=viewport.height+'px';wrapper.setAttribute('aria-label',`Page ${number}`);
      const canvas=document.createElement('canvas');canvas.width=Math.floor(viewport.width*pixelRatio);canvas.height=Math.floor(viewport.height*pixelRatio);canvas.style.width=viewport.width+'px';canvas.style.height=viewport.height+'px';canvas.setAttribute('aria-hidden','true');wrapper.appendChild(canvas);container.appendChild(wrapper);
      await page.render({canvasContext:canvas.getContext('2d'),viewport,transform:pixelRatio===1?null:[pixelRatio,0,0,pixelRatio,0,0]}).promise;checkActive(generation);
      if(state.mode==='original'){
        const textLayer=document.createElement('div');textLayer.className='pdf-text-layer';textLayer.setAttribute('aria-label',`Selectable text on page ${number}`);
        const measuring=document.createElement('canvas').getContext('2d');
        for(const item of state.pages[number-1].items){
          const span=document.createElement('span');span.textContent=item.text;span.dataset.pdfStart=String(item.start);span.dataset.pdfEnd=String(item.end);span.dataset.pdfPage=String(number);span.style.left=(item.rect[0]*100)+'%';span.style.top=(item.rect[1]*100)+'%';span.style.fontSize=(item.fontHeight*scale)+'px';span.style.fontFamily=item.fontFamily;span.style.height=(item.rect[3]*100)+'%';
          measuring.font=`${item.fontHeight*scale}px ${item.fontFamily}`;const measured=measuring.measureText(item.text).width;span.style.transform=`rotate(${item.angle}rad) scaleX(${measured?item.rect[2]*viewport.width/measured:1})`;textLayer.appendChild(span);
        }
        wrapper.appendChild(textLayer);const overlays=document.createElement('div');overlays.className='pdf-annotation-layer';wrapper.appendChild(overlays);
      }
    }
    renderOverlays();}finally{if(generation===state.generation){state.rendering=false;renderToolbar();}}
  }
  function renderOverlays(){
    if(!state.dialog)return;
    state.dialog.querySelectorAll('.pdf-annotation-layer').forEach(layer=>layer.innerHTML='');if(state.mode!=='original')return;
    (state.review?.annotations||[]).forEach((annotation,index)=>{
      const layer=find(`.pdf-page[data-page="${annotation.page}"] .pdf-annotation-layer`);if(!layer)return;
      for(const [rIndex,rect] of (annotation.rects||[]).entries()){
        const mark=document.createElement('button');mark.type='button';mark.className=`pdf-highlight ${annotation.status} ${annotation.kind}`;mark.dataset.pdfAction='focus';mark.dataset.annotationId=annotation.id;mark.style.left=(rect[0]*100)+'%';mark.style.top=(rect[1]*100)+'%';mark.style.width=(rect[2]*100)+'%';mark.style.height=(rect[3]*100)+'%';mark.setAttribute('aria-label',`Annotation ${index+1}: ${annotation.kind==='note'?annotation.note:annotation.after}`);mark.title=annotation.kind==='note'?annotation.note:annotation.after;if(!rIndex)mark.dataset.number=String(index+1);layer.appendChild(mark);
      }
    });
  }
  function selectionPoint(node,offset){
    const element=node?.nodeType===3?node.parentElement:node;const span=element?.closest?.('[data-pdf-start]');if(!span)return null;
    const range=document.createRange();range.selectNodeContents(span);try{range.setEnd(node,offset);}catch{return null;}
    return {position:Number(span.dataset.pdfStart)+Math.min(range.toString().length,span.textContent.length),page:Number(span.dataset.pdfPage)};
  }
  function readSelection(){
    if(state.mode!=='original'||state.busy||state.editorDirty||state.noteTool||!state.review||state.review.status!=='draft')return;
    const selection=window.getSelection();if(!selection||selection.isCollapsed||!selection.rangeCount)return;
    const range=selection.getRangeAt(0),start=selectionPoint(range.startContainer,range.startOffset),end=selectionPoint(range.endContainer,range.endOffset);
    if(!start||!end||end.position<=start.position)return;
    if(start.page!==end.page){setMessage('Select text within one page for each annotation.',true);return;}
    const page=find(`.pdf-page[data-page="${start.page}"]`);if(!page)return;
    const before=state.document.text.slice(start.position,end.position);if(!before.trim())return;
    state.selection={kind:'replace',start:start.position,end:end.position,before,after:before,note:'',page:start.page,rects:normalizedRects(Array.from(range.getClientRects()),page.getBoundingClientRect())};state.editingId=null;state.editorDirty=false;state.error='';renderSidebar();
  }
  async function updateAnnotations(annotations,notice){
    if(state.busy)return;const generation=state.generation;state.busy=true;state.error='';renderSidebar();
    try{const response=await request('/api/action',{op:'pdf_review_update',review_id:state.review.id,revision:state.review.revision,annotations});if(generation!==state.generation)return;state.review=response.review;state.confirmed=false;state.previewBytes=null;state.selection=null;state.editingId=null;state.editorDirty=false;state.closeRequested=false;state.notice=notice;renderOverlays();}
    catch(error){if(generation===state.generation)state.error=error.message;}
    finally{if(generation===state.generation){state.busy=false;renderSidebar();renderToolbar();}}
  }
  async function createReview(){const generation=state.generation;const response=await request('/api/action',{op:'pdf_review_create',resume_id:state.options.resumeId,...(state.jobId?{job_id:state.jobId}:{}),document:state.document});checkActive(generation);state.review=response.review;state.confirmed=false;state.previewBytes=null;state.selection=null;state.editingId=null;state.editorDirty=false;renderSidebar();renderOverlays();}
  async function suggestions(provider){
    if(!state.jobId||state.busy)return;const generation=state.generation;state.busy=true;state.error='';state.notice=provider==='ai'?'Preparing AI suggestions…':'Matching your existing evidence to the role…';renderSidebar();renderToolbar();
    try{const response=await request('/api/action',{op:'pdf_review_suggest',review_id:state.review.id,revision:state.review.revision,mode:provider});if(generation!==state.generation)return;state.review=response.review;state.confirmed=false;state.previewBytes=null;state.notice=response.message||'Suggestions are ready. Accept, reject, or edit each proposal.';renderOverlays();}
    catch(error){if(generation===state.generation){state.error=error.message;state.notice='';}}
    finally{if(generation===state.generation){state.busy=false;renderSidebar();renderToolbar();}}
  }
  function layoutParagraphs(text){
    const lines=text.replace(/\r\n?/g,'\n').split('\n'),paragraphs=[],heading=/^(?:(?:technical )?skills|(?:professional |work )?experience|education|projects|publications|certifications|(?:professional )?summary)$/i;
    for(const raw of lines){const line=raw.replace(/\u0088/g,'•').trim();const previous=paragraphs[paragraphs.length-1]||'';
      const startsBlock=!line||heading.test(line)||/^[-*•▪◦‣]\s/.test(line)||/(?:19|20)\d{2}.*(?:Present|Current|(?:19|20)\d{2})/i.test(line);
      const canJoin=paragraphs.length>1&&previous&&!heading.test(previous)&&!/[.!?:]$/.test(previous)&&!startsBlock&&(previous.length>70||/^[a-z0-9]/.test(line));
      if(canJoin)paragraphs[paragraphs.length-1]+=previous.endsWith('-')?line:' '+line;else paragraphs.push(line);
    }
    return paragraphs;
  }
  function wrapText(text,font,size,width){
    const words=text.split(/\s+/),lines=[];let line='';
    for(const word of words){if(!word)continue;const candidate=line?line+' '+word:word;if(font.widthOfTextAtSize(candidate,size)<=width){line=candidate;continue;}if(line){lines.push(line);line='';}if(font.widthOfTextAtSize(word,size)<=width){line=word;continue;}let part='';for(const char of word){if(part&&font.widthOfTextAtSize(part+char,size)>width){lines.push(part);part='';}part+=char;}line=part;}
    if(line)lines.push(line);return lines.length?lines:[''];
  }
  async function buildRevisionPdf(text){
    await loadExportLibraries();
    const {PDFDocument,PDFName,rgb}=window.PDFLib,document=await PDFDocument.create();document.registerFontkit(window.fontkit);
    const response=await fetch('/vendor/pdfjs/standard_fonts/LiberationSans-Regular.ttf',{credentials:'same-origin',cache:'force-cache'});if(!response.ok)throw new Error('The resume font is unavailable. Reload the app and try again.');
    const font=await document.embedFont(await response.arrayBuffer(),{subset:true});
    const visibleText=text.replace(/\u0088/g,'•');const available=new Set(font.getCharacterSet());const unsupported=[...new Set([...visibleText].filter(character=>!/[\r\n\t]/.test(character)&&!available.has(character.codePointAt(0))))];if(unsupported.length)throw new Error('The export font cannot render '+unsupported.slice(0,6).map(character=>'U+'+character.codePointAt(0).toString(16).toUpperCase()).join(', ')+'. Replace these characters before exporting so the PDF does not lose text.');
    const boldResponse=await fetch('/vendor/pdfjs/standard_fonts/LiberationSans-Bold.ttf',{credentials:'same-origin',cache:'force-cache'});if(!boldResponse.ok)throw new Error('The resume font is unavailable. Reload the app and try again.');const boldFont=await document.embedFont(await boldResponse.arrayBuffer(),{subset:true});
    document.setTitle(state.label||'Resume revision');document.setCreator('Jobflow');document.setProducer('Jobflow PDF review');
    const width=612,height=792,margin=44,headings=/^(?:technical )?skills$|^(?:professional |work )?experience$|^education$|^projects$|^publications$|^certifications$|^(?:professional )?summary$/i;
    let page=document.addPage([width,height]),y=height-margin;
    const paragraphs=layoutParagraphs(text);
    for(let i=0;i<paragraphs.length;i++){
      const line=paragraphs[i].replace(/\u0088/g,'•');if(!line.trim()){y-=5;continue;}
      const name=i===0,heading=headings.test(line.trim()),size=name?19:heading?12:10;
      const leading=name?25:heading?18:13,spaceBefore=heading?8:0,activeFont=name||heading?boldFont:font;
      const wrapped=wrapText(line,activeFont,size,width-margin*2);if(y-spaceBefore-leading*Math.min(wrapped.length,2)<margin){page=document.addPage([width,height]);y=height-margin;}
      y-=spaceBefore;
      for(const segment of wrapped){if(y-leading<margin){page=document.addPage([width,height]);y=height-margin;}y-=leading;page.drawText(segment,{x:margin,y,size,font:activeFont,color:rgb(.10,.15,.18)});for(const link of state.sourceLinks){const at=segment.indexOf(link.label);if(at<0||segment.indexOf(link.label,at+1)>=0)continue;const left=margin+activeFont.widthOfTextAtSize(segment.slice(0,at),size),right=left+activeFont.widthOfTextAtSize(link.label,size);const reference=document.context.register(document.context.obj({Type:'Annot',Subtype:'Link',Rect:[left,y-2,right,y+size],Border:[0,0,0],A:{Type:'Action',S:'URI',URI:window.PDFLib.PDFString.of(link.url)}}));let annotations=page.node.lookup(PDFName.of('Annots'));if(!annotations){annotations=document.context.obj([]);page.node.set(PDFName.of('Annots'),annotations);}annotations.push(reference);}}
    }
    return new Uint8Array(await document.save());
  }
  async function preview(){
    if(state.busy||!state.review)return;const generation=state.generation;state.busy=true;state.error='';renderToolbar();renderSidebar();
    try{const text=applyEdits(state.document.text,state.review.annotations);const bytes=await buildRevisionPdf(text);if(generation!==state.generation)return;state.previewBytes=bytes;if(state.previewPdf)await state.previewPdf.destroy();const pdf=await state.pdfjs.getDocument(pdfOptions(state.previewBytes.slice())).promise;if(generation!==state.generation){pdf.destroy();return;}state.previewPdf=pdf;state.mode='preview';state.activePage=1;const firstPage=await pdf.getPage(1);checkActive(generation);state.scale=clamp(((find('.pdf-review-viewport')?.clientWidth||800)-60)/firstPage.getViewport({scale:1}).width,.5,1.5);state.notice='Preview includes accepted edits only, in a new clean layout. Review every page before saving.';await renderPages();}
    catch(error){if(generation===state.generation){state.error=error.message;state.previewBytes=null;state.confirmed=false;}}
    finally{if(generation===state.generation){state.busy=false;renderSidebar();renderToolbar();}}
  }
  async function saveRevision(){
    if(state.busy||state.editorDirty||!state.confirmed||!state.previewBytes||countChanges(state.review.annotations).pending||!countChanges(state.review.annotations).accepted)return;
    const generation=state.generation;state.busy=true;state.error='';renderSidebar();renderToolbar();
    try{const text=applyEdits(state.document.text,state.review.annotations);const response=await request('/api/action',{op:'pdf_review_save',review_id:state.review.id,revision:state.review.revision,text,content_base64:bytesBase64(state.previewBytes),label:state.label,confirmed:true,set_default:state.setDefault});if(generation!==state.generation)return;state.review=response.review||{...state.review,status:'saved',saved_resume_id:response.resume.id};state.notice='Saved as a separate PDF revision. Your original is still in the library.';await state.options.onSaved?.(response.resume);}
    catch(error){if(generation===state.generation)state.error=error.message;}
    finally{if(generation===state.generation){state.busy=false;renderSidebar();renderToolbar();}}
  }
  function focusAnnotation(id){
    const annotation=state.review?.annotations.find(a=>a.id===id);if(!annotation)return;
    if(state.mode!=='original'){state.mode='original';renderPages().then(()=>focusAnnotation(id)).catch(error=>setMessage(error.message,true));return;}
    state.activePage=annotation.page;find(`.pdf-page[data-page="${annotation.page}"]`)?.scrollIntoView({behavior:'smooth',block:'start'});find(`[data-pdf-card="${id}"]`)?.scrollIntoView({behavior:'smooth',block:'nearest'});state.dialog.querySelectorAll('.pdf-annotation-card.is-active').forEach(card=>card.classList.remove('is-active'));find(`[data-pdf-card="${id}"]`)?.classList.add('is-active');renderToolbar();
  }
  async function click(event){
    const target=event.target.closest?.('[data-pdf-action]');if(!target){if(state.noteTool&&!state.busy&&state.mode==='original'){const page=event.target.closest?.('.pdf-page');if(page){const box=page.getBoundingClientRect(),pageNumber=Number(page.dataset.page),x=clamp((event.clientX-box.left)/box.width,0,.98),y=clamp((event.clientY-box.top)/box.height,0,.98);state.selection={kind:'note',start:0,end:0,before:'',after:'',note:'',page:pageNumber,rects:[[x,y,.02,.02]]};state.editingId=null;state.noteTool=false;renderSidebar();renderToolbar();find('#pdf-annotation-note')?.focus();}}return;}const op=target.dataset.pdfAction;if(op==='close'){requestClose();return;}if(op==='discard-close'){close();return;}if(op==='keep-editing'){state.closeRequested=false;renderSidebar();return;}if(state.busy||state.rendering)return;
    if(state.editorDirty&&['reload','edit','remove','accept','reject','page-note'].includes(op)){setMessage('Save or cancel your annotation before changing this review.',true);return;}
    if(op==='page-note'){state.noteTool=!state.noteTool;state.notice=state.noteTool?'Click a point on the PDF to attach a comment.':'';renderSidebar();renderToolbar();return;}
    if(op==='cancel-selection'){state.selection=null;state.editingId=null;state.editorDirty=false;state.closeRequested=false;renderSidebar();return;}
    if(op==='preview'){await preview();return;}
    if(op==='original'){state.mode='original';state.activePage=1;await renderPages();renderSidebar();return;}
    if(op==='zoom-in'||op==='zoom-out'||op==='fit'){if(op==='fit'){const generation=state.generation,pdf=state.mode==='preview'?state.previewPdf:state.pdf;if(!pdf)return;const page=await pdf.getPage(1);checkActive(generation);state.scale=clamp(((find('.pdf-review-viewport')?.clientWidth||800)-60)/page.getViewport({scale:1}).width,.5,2);}else state.scale=clamp(state.scale+(op==='zoom-in'?.15:-.15),.5,2);await renderPages();return;}
    if(op==='suggest-local'||op==='suggest-ai'){if(op==='suggest-ai'){if(!state.library?.pdf_ai?.ai_available)return;showAIDisclosure();return;}await suggestions('local');return;}
    if(op==='confirm-ai'){await suggestions('ai');return;}
    if(op==='reload'){const generation=state.generation;try{const response=await request('/api/resume-reviews/'+encodeURIComponent(state.review.id));if(generation!==state.generation)return;state.review=response.review;state.error='';state.selection=null;state.editingId=null;state.confirmed=false;state.previewBytes=null;renderSidebar();renderOverlays();}catch(error){if(generation===state.generation)setMessage(error.message,true);}return;}
    const id=target.dataset.annotationId,annotation=state.review?.annotations.find(a=>a.id===id);if(!annotation)return;
    if(op==='focus'){focusAnnotation(id);return;}
    if(op==='edit'){state.selection={...annotation};state.editingId=id;state.editorDirty=false;renderSidebar();find('#pdf-annotation-after')?.focus();return;}
    if(op==='remove'){await updateAnnotations(state.review.annotations.filter(a=>a.id!==id),'Annotation removed from this review.');return;}
    if(op==='accept'||op==='reject')await updateAnnotations(state.review.annotations.map(a=>a.id===id?{...a,status:op==='accept'?'accepted':'rejected'}:a),op==='accept'?'Edit accepted. Preview the revision to see the result.':'Suggestion rejected.');
  }
  async function submit(event){
    if(event.target.id==='pdf-annotation-form'){
      event.preventDefault();if(state.busy||!state.selection)return;const annotation={...state.selection,kind:find('#pdf-annotation-kind').value,after:find('#pdf-annotation-after').value,note:find('#pdf-annotation-note').value,status:'pending',source:'manual'};
      if(annotation.kind==='note'&&!annotation.note.trim()){setMessage('Add a comment before saving this note.',true);return;}
      if(annotation.kind==='note')annotation.status='accepted';
      const annotations=state.review.annotations.filter(a=>a.id!==state.editingId);if(state.editingId)annotation.id=state.editingId;else delete annotation.id;annotations.push(annotation);await updateAnnotations(annotations,'Annotation saved.');
    }else if(event.target.id==='pdf-save-revision'){event.preventDefault();await saveRevision();}
  }
  async function change(event){
    const target=event.target;
    if(target.id==='pdf-annotation-kind'){if(state.selection){state.selection.kind=target.value;markEditorDirty();}const replacement=find('#pdf-annotation-after');if(replacement)replacement.disabled=target.value==='note';}
    if(target.id==='pdf-review-page'){state.activePage=Number(target.value);find(`.pdf-page[data-page="${state.activePage}"]`)?.scrollIntoView({behavior:'smooth',block:'start'});}
    if(target.id==='pdf-revision-confirm'){state.confirmed=target.checked;const button=find('#pdf-save-revision button[type="submit"]');if(button)button.disabled=state.busy||state.editorDirty||!state.confirmed||countChanges(state.review.annotations).pending||!countChanges(state.review.annotations).accepted||!state.previewBytes;}
    if(target.id==='pdf-revision-default')state.setDefault=target.checked;
  }
  function markEditorDirty(){state.editorDirty=true;state.confirmed=false;const check=find('#pdf-revision-confirm');if(check)check.checked=false;const save=find('#pdf-save-revision button[type="submit"]');if(save)save.disabled=true;}
  function requestClose(){if(state.editorDirty){state.closeRequested=true;renderSidebar();}else close();}
  function input(event){const target=event.target;if(state.selection&&['pdf-annotation-after','pdf-annotation-note'].includes(target.id)){state.selection[target.id==='pdf-annotation-after'?'after':'note']=target.value;markEditorDirty();}if(target.id==='pdf-revision-label'){state.label=target.value;state.confirmed=false;const check=find('#pdf-revision-confirm');if(check)check.checked=false;const button=find('#pdf-save-revision button[type="submit"]');if(button)button.disabled=true;}}
  async function open(options){
    close();const generation=++state.generation;state.options=options;state.jobId=options.jobId||'';state.error='';state.notice='';state.selection=null;state.editingId=null;state.review=null;state.resume=null;state.library=null;state.document=null;state.pages=[];state.sourceLinks=[];state.scale=1;state.activePage=1;state.mode='original';state.previewBytes=null;state.confirmed=false;state.setDefault=false;state.busy=true;state.noteTool=false;state.rendering=false;state.editorDirty=false;state.closeRequested=false;
    const dialog=document.createElement('dialog');state.dialog=dialog;dialog.className='pdf-review-dialog';dialog.setAttribute('aria-labelledby','pdf-review-title');dialog.innerHTML=dialogShell();document.body.appendChild(dialog);dialog.addEventListener('click',event=>{click(event).catch(error=>{if(generation===state.generation)setMessage(error.message,true);});});dialog.addEventListener('submit',event=>{submit(event).catch(error=>{if(generation===state.generation)setMessage(error.message,true);});});dialog.addEventListener('change',event=>{change(event).catch(error=>{if(generation===state.generation)setMessage(error.message,true);});});dialog.addEventListener('input',input);dialog.addEventListener('mouseup',event=>{if(event.target.closest?.('.pdf-text-layer'))readSelection();});dialog.addEventListener('keyup',event=>{if(event.key==='Shift'&&event.target.closest?.('.pdf-review-pages'))readSelection();});dialog.addEventListener('cancel',event=>{event.preventDefault();requestClose();});dialog.showModal();renderToolbar();renderSidebar();
    try{
      const library=await request('/api/resumes');checkActive(generation);state.library=library;
      if(options.workspaceId&&state.library.workspace_id!==options.workspaceId)throw new Error('The account changed. Close this review and reopen the library.');
      const detail=await request('/api/resumes/'+encodeURIComponent(options.resumeId));checkActive(generation);state.resume=detail.resume;if(state.resume.mime!=='application/pdf')throw new Error('Choose a PDF resume to open the annotated viewer.');
      state.label=(state.resume.label||state.resume.filename.replace(/\.pdf$/i,''))+' · revised';find('#pdf-review-title').textContent=state.resume.label||state.resume.filename;
      const pdfjs=await loadPdfJs(),response=await fetch('/api/application-resumes/'+encodeURIComponent(options.resumeId),{credentials:'same-origin',cache:'no-store'});if(!response.ok)throw new Error('The saved PDF could not be loaded.');const pdfBytes=new Uint8Array(await response.arrayBuffer());checkActive(generation);state.pdfBytes=pdfBytes;
      const loadedPdf=await pdfjs.getDocument(pdfOptions(state.pdfBytes.slice())).promise;if(generation!==state.generation){loadedPdf.destroy();return;}state.pdf=loadedPdf;const extracted=await extractDocument(state.pdf,generation);checkActive(generation);state.document=extracted.document;state.pages=extracted.pages;state.sourceLinks=extracted.links;
      state.scale=clamp(((find('.pdf-review-viewport')?.clientWidth||800)-60)/state.pages[0].viewport.width,.5,1.5);await renderPages();
      if(!state.document.text.trim())state.notice='This PDF has no selectable text. Add page comments, or upload a text-based PDF to edit and tailor its wording.';
      await createReview();checkActive(generation);if(options.autoSuggest&&state.jobId&&!state.review.annotations.length&&state.document.text.trim()){state.busy=false;await suggestions('local');checkActive(generation);}
    }catch(error){if(generation===state.generation){state.error=error.message;const loading=find('.pdf-review-loading');if(loading)loading.textContent='PDF review unavailable.';}}
    finally{if(generation===state.generation){state.busy=false;renderSidebar();renderToolbar();}}
  }
  function close(){
    state.generation++;const dialog=state.dialog;state.dialog=null;if(dialog){dialog.close();dialog.remove();}Promise.resolve(state.pdf?.destroy?.()).catch(()=>{});Promise.resolve(state.previewPdf?.destroy?.()).catch(()=>{});state.pdf=null;state.previewPdf=null;state.pdfBytes=null;state.previewBytes=null;state.document=null;state.review=null;state.selection=null;state.options=null;
  }
  window.JobflowPDFReview={open,close};
  if(typeof module!=='undefined'&&module.exports)module.exports={applyEdits,countChanges,normalizedRects,wrapText,layoutParagraphs,exportLink,esc,_state:state,_renderSidebar:renderSidebar,_saveRevision:saveRevision,_suggestions:suggestions,_input:input,_change:change,_click:click};
})();

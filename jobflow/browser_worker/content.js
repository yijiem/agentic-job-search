/* Runs only in the extension's isolated world. No page messages or page scripts
 * are treated as instructions. No values leave a form during inspection. */
(() => {
  'use strict';
  const normalize = value => String(value ?? '').replace(/\s+/g, ' ').trim();
  const folded = value => normalize(value).toLowerCase().replace(/[\s*:.]+$/g, '');
  const cleanLabel = value => normalize(value).replace(/\s*\*\s*$/, '');
  const visible = node => Boolean(node && !node.hidden && node.getClientRects().length &&
    getComputedStyle(node).visibility !== 'hidden' && getComputedStyle(node).display !== 'none');
  const text = node => normalize(node?.innerText || node?.textContent || '');
  const confirmPattern = /(?:your application (?:has been|was) (?:successfully )?(?:submitted|received)|(?:we have|we've|we’ve) received your application|thank you for (?:your application|applying)(?:[.!\s]|$)|application submitted successfully)/i;
  function confirmationText(value) {
    const normalized = normalize(value);
    if (/(?:could not|couldn't|couldn’t|unable to|failed to|was not|not submitted|submission error|try again)/i.test(normalized)) return '';
    const match = normalized.match(confirmPattern);
    if (!match || /(?:if|once|when|after)\s+(?:you\s+)?(?:have\s+)?(?:submit|apply)/i.test(normalized.slice(Math.max(0, match.index - 90), match.index))) return '';
    return normalized.slice(Math.max(0, match.index - 40), match.index + match[0].length + 140).slice(0, 500);
  }
  function labelledText(node) {
    return (node.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map(id => text(document.getElementById(id))).filter(Boolean).join(' ');
  }
  function fileGroup(node) {
    if (node.type !== 'file') return null;
    const group = node.closest('[role="group"],fieldset');
    if (!group || group.querySelectorAll('input[type="file"]').length !== 1) return null;
    const label = labelledText(group) || group.getAttribute('aria-label') || text(group.querySelector('legend'));
    return label ? {label:cleanLabel(label),required:group.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label)} : null;
  }
  function labelFor(node) {
    const labelled = labelledText(node);
    const labels = Array.from(node.labels || []).map(text).filter(Boolean).join(' ');
    const label = cleanLabel(labels || labelled || node.getAttribute('aria-label') || node.getAttribute('placeholder') || node.name || node.id || 'Unlabelled field');
    // Some uploaders label their picker only "Attach" and name the field on an
    // enclosing accessible group. A group with several files is ambiguous.
    if (node.type === 'file' && /^(?:attach|upload|browse|choose|select)(?:\s+(?:a\s+)?file)?(?:\s*(?:…|\.\.\.))?$/i.test(label)) {
      return fileGroup(node)?.label || label;
    }
    return label;
  }
  function hiddenValidationControl(node) {
    // React-select uses an unnamed, transparent input for native required
    // validation. It is not another applicant question; its combobox remains
    // in the review. Do not discard named/labelled controls or file pickers.
    return node.tagName === 'INPUT' && node.type === 'text' &&
      node.getAttribute('aria-hidden') === 'true' && node.getAttribute('tabindex') === '-1' &&
      !normalize(node.name) && !normalize(node.id) && !node.labels?.length &&
      !node.getAttribute('aria-label') && !node.getAttribute('aria-labelledby') &&
      parseFloat(getComputedStyle(node).opacity) === 0;
  }
  function fieldKey(type, node, label) {
    // Labels survive React-generated ids changing after a reload. Include a
    // stable name when one exists; duplicate keys are explicitly unsupported.
    const name = normalize(node.name);
    return `${type}:${name ? name + ':' : ''}${folded(label)}`.slice(0, 250);
  }
  function explicitValue(answers, field) {
    if (Object.prototype.hasOwnProperty.call(answers || {}, field.key)) return answers[field.key];
    const matches = Object.keys(answers || {}).filter(key => folded(key) === folded(field.label));
    return matches.length === 1 ? answers[matches[0]] : undefined;
  }
  function planValue(field, applicant = {}, answers = {}, resume = {}) {
    const explicit = explicitValue(answers, field);
    if (explicit !== undefined) return explicit;
    const label = folded(field.label);
    if (field.type === 'checkbox') return false;
    if (field.type === 'file') return /resume|curriculum vitae|\bcv\b/.test(label) ? resume.filename || '' : '';
    // Intentionally no guessed work authorization, visa, salary, demographic,
    // referral, legal acknowledgement, or free-response answers.
    const rules = [
      [/^(first name|given name)$/, 'first_name'], [/^(last name|family name|surname)$/, 'last_name'],
      [/^(e-?mail|e-?mail address)$/, 'email'], [/^(phone|phone number|telephone|mobile|mobile number)$/, 'phone'],
      [/^(location|current location|city|city of residence)$/, 'location'],
      [/^(linkedin|linkedin profile|linkedin url|linkedin profile url)$/, 'linkedin'],
      [/^(website|personal website|portfolio|portfolio url|website url)$/, 'website'],
    ];
    if (/^(name|full name|legal name)$/.test(label)) return normalize(`${applicant.first_name || ''} ${applicant.last_name || ''}`);
    for (const [pattern, key] of rules) if (pattern.test(label)) return applicant[key] || '';
    return '';
  }
  function answerIsValid(field, value) {
    if (!field.supported) return false;
    if (field.type === 'checkbox') return typeof value === 'boolean' && (!field.required || value === true);
    if (value === '' || value === undefined || value === null) return !field.required;
    if (typeof value !== 'string') return false;
    if (['select', 'radio'].includes(field.type) && !(field.options || []).includes(value)) return false;
    if (field.type === 'email' && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) return false;
    return true;
  }
  function schema(fields) {
    return fields.map(({key,label,type,required,options,supported}) => ({key,label,type,required:Boolean(required),options:options || [],supported:Boolean(supported)}));
  }
  async function signature(fields) {
    const bytes = new TextEncoder().encode(JSON.stringify(schema(fields)));
    return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), b => b.toString(16).padStart(2, '0')).join('');
  }
  function challengeVisible(node) {
    if (!visible(node)) return false;
    // Providers often leave a challenge frame mounted inside a hidden or
    // transparent container. The frame's own computed style is not enough.
    for (let parent = node; parent; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      if (parent.hidden || style.display === 'none' ||
          ['hidden', 'collapse'].includes(style.visibility) || parseFloat(style.opacity) === 0) return false;
    }
    return Array.from(node.getClientRects()).some(rect => rect.width > 0 && rect.height > 0);
  }
  function activeChallengeFrame(frame) {
    if (!challengeVisible(frame)) return false;
    let url;
    try { url = new URL(frame.src, location.href); } catch { return false; }
    const title = normalize(frame.getAttribute('title'));
    if (/(^|\.)(google\.com|recaptcha\.net)$/.test(url.hostname)) {
      return /^\/recaptcha\/(api2|enterprise)\/bframe\/?$/.test(url.pathname) ||
        /^recaptcha challenge\b/i.test(title);
    }
    if (/(^|\.)hcaptcha\.com$/.test(url.hostname)) {
      return /(?:[?#&])frame=challenge(?:[&#]|$)/.test(url.search + url.hash) ||
        /^main content of the hcaptcha challenge\b/i.test(title);
    }
    return false;
  }
  function blockers() {
    const passwords = Array.from(document.querySelectorAll('input[type="password"]')).some(visible);
    if (passwords) return 'Sign in on the employer page, then retry inspection from Jobflow.';
    // An anchor, badge, checkbox, or Turnstile iframe can be passive or already
    // completed. Its presence alone is not evidence of a challenge. Inspect and
    // fill ordinary fields without reading tokens or interacting with widgets;
    // the employer still controls verification and acceptance of submission.
    if (Array.from(document.querySelectorAll('iframe')).some(activeChallengeFrame)) {
      return 'A security challenge is open on the employer page. Complete the displayed challenge there, then click Inspect form again in Jobflow.';
    }
    if (Array.from(document.querySelectorAll('[role="dialog"],main,h1,h2')).filter(challengeVisible)
      .some(node => /^(verify (?:that )?you are human|security verification|just a moment)/i.test(text(node)))) return 'The employer page is showing a security verification screen. Follow its instructions there, then click Inspect form again in Jobflow.';
    return '';
  }
  function submitButtons(root, includeDisabled = false) {
    return Array.from(root.querySelectorAll('button,input[type="submit"]')).filter(node => visible(node) && (includeDisabled || !node.disabled) &&
      /^(submit(?: application)?|send application|apply(?: now| for (?:this|the) (?:job|role))?)$/i.test(normalize(node.value || text(node)).replace(/[→↗➜]+/g, '').trim()));
  }
  function findRoot() {
    const forms = Array.from(document.querySelectorAll('form')).filter(visible).filter(form => {
      const inputs = Array.from(form.querySelectorAll('input,textarea,select')).filter(visible);
      return inputs.some(node => node.type === 'file' && /resume|curriculum|\bcv\b/i.test(labelFor(node))) ||
        inputs.some(node => node.type === 'email') && submitButtons(form,true).length > 0;
    });
    if (forms.length === 1) return forms[0];
    if (forms.length > 1) throw new Error('More than one application form is visible. Open the intended form and retry inspection.');
    const inputs = Array.from(document.querySelectorAll('input[type="file"]')).filter(node => /resume|curriculum|\bcv\b/i.test(labelFor(node)));
    if (inputs.length === 1 && submitButtons(document.body,true).length === 1) return document.body;
    throw new Error('Open the application form in this browser tab, then retry inspection from Jobflow. Embedded forms and custom application steps need your attention.');
  }
  function readForm() {
    const blocker = blockers();
    if (blocker) throw new Error(blocker);
    const root = findRoot();
    const fields = [], controls = new Map(), radioNames = new Set();
    const nodes = Array.from(root.querySelectorAll('input,textarea,select,[role="combobox"],[role="checkbox"],[role="radio"]'));
    for (const node of nodes) {
      const native = /^(INPUT|TEXTAREA|SELECT)$/.test(node.tagName);
      if (native && ['hidden','submit','button','reset','image'].includes(node.type)) continue;
      if (node.disabled || node.readOnly) continue;
      if (hiddenValidationControl(node)) continue;
      // A native hidden file picker is valid if its labelled upload UI is
      // visible; arbitrary hidden text controls stay out of the applicant form.
      if (!visible(node) && !(node.type === 'file' && (node.labels?.length || /resume/i.test(node.name || node.id)))) continue;
      if (!native && node.querySelector('input,select')) continue;
      const label = labelFor(node);
      let type = node.tagName === 'TEXTAREA' ? 'textarea' : node.tagName === 'SELECT' ? 'select' : node.type || node.getAttribute('role') || 'unknown';
      let supported = native && node.getAttribute('role') !== 'combobox' && ['text','email','tel','url','number','date','textarea','select','radio','checkbox','file'].includes(type);
      const required = Boolean(node.required || node.getAttribute('aria-required') === 'true' || /\*\s*$/.test(text(node.labels?.[0])) || fileGroup(node)?.required);
      let options = [], elements = [node];
      if (type === 'radio') {
        if (!node.name) supported = false;
        if (radioNames.has(node.name)) continue;
        radioNames.add(node.name);
        elements = nodes.filter(other => other.type === 'radio' && other.name === node.name && !other.disabled && visible(other));
        const group = node.closest('fieldset') || node.closest('[role="radiogroup"]');
        const groupLabel = cleanLabel(text(group?.querySelector('legend')) || group?.getAttribute('aria-label') || node.name);
        options = elements.map(labelFor);
        const field = {key:fieldKey(type,node,groupLabel),label:groupLabel,type,required:elements.some(x => x.required) || required,options,supported:supported && new Set(options).size === options.length};
        fields.push(field); controls.set(field.key, elements); continue;
      }
      if (type === 'select') {
        supported = supported && !node.multiple;
        options = Array.from(node.options).filter(option => !option.disabled && option.value !== '').map(option => cleanLabel(option.textContent));
        if (new Set(options).size !== options.length) supported = false;
      }
      if (type === 'file' && !/resume|curriculum vitae|\bcv\b/i.test(label) && required) supported = false;
      if (type === 'file' && node.multiple) supported = false;
      const field = {key:fieldKey(type,node,label),label,type,required,options,supported};
      if (controls.has(field.key)) { field.supported = false; const old = fields.find(x => x.key === field.key); if (old) old.supported = false; }
      fields.push(field); controls.set(field.key, elements);
    }
    if (new Set(fields.map(field => field.key)).size !== fields.length) throw new Error('This form has indistinguishable question labels. Complete it manually.');
    if (!fields.length) throw new Error('No supported application fields were found. Complete this application manually.');
    if (fields.length > 150) throw new Error('This form is too large for automatic review. Complete it manually.');
    const buttons = submitButtons(root,true);
    if (buttons.length !== 1) throw new Error('The final Submit application button is missing or ambiguous. Open the final application step and retry inspection.');
    return {fields, controls, root, button:buttons[0]};
  }
  function assertApplicationURL(task) {
    let allowed = false;
    try {
      const expected = new URL(task.url), current = new URL(location.href);
      const base = path => path.replace(/\/(?:apply|application)\/?$/, '').replace(/\/$/, '');
      allowed = current.protocol === 'https:' && !current.username && !current.password &&
        expected.origin === current.origin && base(expected.pathname) === base(current.pathname);
      for (const key of ['gh_jid','job_id','jobId','postingId','requisitionId']) {
        if (expected.searchParams.get(key) !== current.searchParams.get(key)) allowed = false;
      }
    } catch {}
    if (!allowed) throw new Error('The browser moved to a different application. No further form action was taken. Review the employer tab.');
  }
  async function inspect(task) {
    assertApplicationURL(task);
    const form = readForm();
    const planned_answers = Object.fromEntries(form.fields.map(field => [field.key, planValue(field, task.applicant, task.answers, task.resume)]));
    const form_signature = await signature(form.fields);
    assertApplicationURL(task);
    return {fields:schema(form.fields),form_signature,planned_answers,
      message:form.fields.some(field => !answerIsValid(field, planned_answers[field.key])) ? 'Review missing answers or unsupported controls in Jobflow.' : 'Form inspected. Review the exact materials and answers in Jobflow before authorizing submission.'};
  }
  function setNativeValue(node, value) {
    const prototype = node.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : node.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(node, value);
    node.dispatchEvent(new Event('input', {bubbles:true}));
    node.dispatchEvent(new Event('change', {bubbles:true}));
  }
  async function fill(task) {
    assertApplicationURL(task);
    if (confirmation().confirmed) throw new Error('This page already shows a confirmation. Check for a previous submission before continuing.');
    const form = readForm();
    if (await signature(form.fields) !== task.form_signature) throw new Error('The application form changed after review. Inspect it again and review the new questions.');
    assertApplicationURL(task);
    if (!task.resume?.content_base64) throw new Error('The approved resume could not be loaded. Review this application again.');
    // Validate every reviewed answer before any field event can upload data.
    for (const field of form.fields) {
      assertApplicationURL(task);
      const value = task.planned_answers?.[field.key];
      if (!answerIsValid(field,value)) throw new Error(`Review the answer for “${field.label}” in Jobflow.`);
      if (field.type === 'file' && value && !/resume|curriculum vitae|\bcv\b/i.test(field.label)) throw new Error(`The optional “${field.label}” attachment must be omitted or completed manually.`);
    }
    for (const field of form.fields) {
      assertApplicationURL(task);
      const value = task.planned_answers?.[field.key];
      if (!answerIsValid(field, value)) throw new Error(`Review the answer for “${field.label}” in Jobflow.`);
      const nodes = form.controls.get(field.key);
      if (field.type === 'file') {
        if (!value) continue;
        if (value !== task.resume.filename) throw new Error('The selected resume differs from the reviewed file. Review this application again.');
        const bytes = Uint8Array.from(atob(task.resume.content_base64), character => character.charCodeAt(0));
        const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), byte => byte.toString(16).padStart(2,'0')).join('');
        assertApplicationURL(task);
        if (hash !== task.resume.sha256 || bytes.length !== task.resume.size) throw new Error('The resume integrity check failed. Upload it again in Jobflow.');
        const transfer = new DataTransfer();
        transfer.items.add(new File([bytes], task.resume.filename, {type:task.resume.mime}));
        nodes[0].files = transfer.files;
        nodes[0].dispatchEvent(new Event('input', {bubbles:true}));
        nodes[0].dispatchEvent(new Event('change', {bubbles:true}));
      } else if (field.type === 'checkbox') {
        if (nodes[0].checked !== value) nodes[0].click();
      } else if (field.type === 'radio') {
        if (value !== '' && value !== undefined) {
          const node = nodes[field.options.indexOf(value)];
          if (!node) throw new Error('An approved choice is no longer available.');
          if (!node.checked) node.click();
        }
      } else if (field.type === 'select') {
        const option = Array.from(nodes[0].options).find(option => cleanLabel(option.textContent) === value);
        setNativeValue(nodes[0], value ? option?.value ?? '' : '');
      } else setNativeValue(nodes[0], value || '');
    }
    // Resume uploads and React validation may keep the final button disabled.
    // Wait for that specific transient state, but never ignore changed questions.
    let checked;
    for (let attempt = 0; attempt < 20; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 500));
      checked = await validateFilled(task);
      if (checked.ok || !checked.waiting) break;
    }
    if (!checked.ok) throw new Error(checked.message);
    assertApplicationURL(task);
    globalThis.__jobflowPrepared = {attempt_id:task.attempt_id,signature:task.form_signature,clicked:false};
    return {ok:true};
  }
  async function validateFilled(task) {
    assertApplicationURL(task);
    const form = readForm();
    if (await signature(form.fields) !== task.form_signature) return {ok:false,message:'New or changed questions appeared after filling. Review this form again.'};
    assertApplicationURL(task);
    for (const field of form.fields) {
      const nodes = form.controls.get(field.key), value = task.planned_answers?.[field.key];
      if (field.type === 'file' && value && !(nodes[0].files.length === 1 && nodes[0].files[0].name === task.resume.filename)) return {ok:false,message:'The employer did not accept the selected resume upload.'};
      if (field.type === 'checkbox' && nodes[0].checked !== value) return {ok:false,message:`Check “${field.label}” on the employer form.`};
      if (field.type === 'radio' && value && !nodes[field.options.indexOf(value)]?.checked) return {ok:false,message:`The employer did not retain “${field.label}”.`};
      if (field.type === 'select' && value && cleanLabel(nodes[0].selectedOptions[0]?.textContent) !== value) return {ok:false,message:`The employer did not retain “${field.label}”.`};
      if (['text','email','tel','url','number','date','textarea'].includes(field.type) && String(nodes[0].value) !== String(value || '')) return {ok:false,message:`The employer changed “${field.label}”. Review it manually.`};
      if (nodes.some(node => typeof node.checkValidity === 'function' && !node.checkValidity())) return {ok:false,message:`The employer requires a correction to “${field.label}”.`};
    }
    if (form.button.disabled) return {ok:false,waiting:true,message:'The employer is still validating or uploading the reviewed materials. Open this tab, wait for it to finish, then review again.'};
    return {ok:true};
  }
  async function clickSubmit(task) {
    assertApplicationURL(task);
    const prepared = globalThis.__jobflowPrepared;
    if (!prepared || prepared.attempt_id !== task.attempt_id || prepared.clicked || prepared.signature !== task.form_signature) throw new Error('This application is not prepared for a single submission click.');
    const valid = await validateFilled(task);
    if (!valid.ok) throw new Error(valid.message);
    assertApplicationURL(task);
    const form = readForm();
    // Mark in the isolated document before triggering any native handlers.
    assertApplicationURL(task);
    prepared.clicked = true;
    form.button.click();
    return {clicked:true};
  }
  function confirmation(task = {}) {
    const targets = Array.from(document.querySelectorAll('[role="status"],[role="alert"],h1,h2,h3,.application-confirmation,.application-success,.confirmation-message,.success-message,main'));
    const candidates = [];
    for (const node of targets.filter(visible)) {
      const candidate = text(node);
      if (candidate.length > 5000) continue;
      const evidence = confirmationText(candidate);
      const phrase = candidate.match(confirmPattern)?.[0];
      if (evidence && phrase) candidates.push({text:evidence,receipt:folded(phrase)});
    }
    // Keep only standard phrases in the checkpoint, never surrounding applicant
    // content. A phrase already present before clicking cannot become proof
    // merely because an error later hid the application form.
    const receipts = [...new Set(candidates.map(candidate => candidate.receipt))];
    if (submitButtons(document.body,true).length) return {confirmed:false,receipts};
    const fresh = candidates.find(candidate => !(task.baseline_receipts || []).includes(candidate.receipt));
    return fresh ? {confirmed:true,text:fresh.text,url:location.href,receipts} : {confirmed:false,receipts};
  }
  const api = {assertApplicationURL,normalize,folded,planValue,answerIsValid,schema,signature,confirmationText,inspect,fill,validateFilled,clickSubmit,confirmation};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else globalThis.JobflowForm = api;
})();

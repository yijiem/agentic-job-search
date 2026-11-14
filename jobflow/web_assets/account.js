'use strict';

// Account credentials stay in HttpOnly cookies. This page never stores tokens,
// resumes, or profile data in localStorage or sessionStorage.
const accountState = {account:null, draft:null, step:'email', email:'', busy:false,
  pendingUpload:null, pendingSuggestions:[], uploadMessage:'', error:'', dirty:false};
const accountNode = selector => document.querySelector(selector);
const accountEsc = value => String(value ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const accountCopy = value => JSON.parse(JSON.stringify(value));
const splitValues = value => String(value || '').split(/[,\n]/).map(s => s.trim()).filter(Boolean);
const editAccount = () => new URLSearchParams(location.search).get('edit') === '1';
function draftFromOnboarding(onboarding = {}) {
  const profile = accountCopy(onboarding.profile || {});
  const preferences = accountCopy(onboarding.preferences || {});
  profile.name ??= ''; profile.headline ??= ''; profile.skills ??= []; profile.evidence ??= [];
  preferences.preferred_locations ??= [];
  preferences.allowed_levels ??= ['mid','senior','staff'];
  preferences.target_tc ??= 0; preferences.currency ??= 'USD';
  preferences.location_mode ??= 'any';
  preferences.daily_minutes ??= 90;
  preferences.timezone ??= Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  return {profile, preferences, complete:Boolean(onboarding.complete), uploads:accountCopy(onboarding.uploads || [])};
}
function draftPayload(draft) {
  // The server merges these editable preferences into the existing settings.
  // Do not send unrelated backend settings as changes.
  const keys = ['preferred_locations','allowed_levels','target_tc','currency','daily_minutes','timezone','location_mode'];
  const preferences = Object.fromEntries(keys.filter(key => draft.preferences[key] !== undefined).map(key => [key,accountCopy(draft.preferences[key])]));
  return {profile:accountCopy(draft.profile), preferences};
}
function textSuggestions(text, sourceFileId = '') {
  return String(text || '').replace(/\r\n?/g, '\n').split(/\n\s*\n/)
    .map(value => value.trim()).filter(Boolean).map((value, index) => ({
      id:'background-' + Date.now().toString(36) + '-' + index,
      text:value, tags:[], source_file_id:sourceFileId, source_url:'',
    }));
}
function normalizedSuggestions(upload) {
  return (upload.suggestions || []).map((item, index) => typeof item === 'string'
    ? {id:'resume-' + upload.id + '-' + index, text:item, tags:[], source_file_id:upload.id, source_url:''}
    : {...item, text:String(item.text || ''), tags:Array.isArray(item.tags) ? item.tags : [],
       source_file_id:item.source_file_id || upload.id, source_url:item.source_url || ''});
}
function appendSuggestions(draft, suggestions) {
  const existing = new Set(draft.profile.evidence.map(item => item.id));
  let added = 0;
  for (const item of suggestions) {
    if (!item.text.trim()) continue;
    const copy = accountCopy(item);
    if (!copy.id || existing.has(copy.id)) copy.id = 'evidence-' + Date.now().toString(36) + '-' + draft.profile.evidence.length;
    draft.profile.evidence.push(copy); existing.add(copy.id); added++;
  }
  return added;
}
async function accountAPI(path, payload) {
  const options = {credentials:'same-origin', cache:'no-store', headers:{'Accept':'application/json'}};
  if (payload !== undefined) {
    options.method = 'POST'; options.headers['Content-Type'] = 'application/json';
    if (accountState.account?.csrf_token) options.headers['X-CSRF-Token'] = accountState.account.csrf_token;
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); }
  catch { throw new Error('The server did not return a usable response. Please try again.'); }
  if (!response.ok || data.error) {
    if (response.status === 401 && accountState.account?.authenticated) {
      accountState.account = null;
      accountState.step = 'email';
      accountState.draft = null;
      renderAccount();
    }
    throw new Error(typeof data.error === 'string' ? data.error : 'This request could not be completed. Please try again.');
  }
  return data;
}
function accountNotice(message, error = false) {
  const node = accountNode('#account-status');
  node.textContent = message; node.className = 'status' + (error ? ' error' : '');
  clearTimeout(accountState.noticeTimer);
  accountState.noticeTimer = setTimeout(() => { node.textContent = ''; }, 6500);
}
function setAccountError(message) {
  accountState.error = message;
  const error = accountNode('#account-error');
  if (error) { error.textContent = message; if (message) error.focus(); }
  else if (message) accountNotice(message, true);
}
async function withAccountBusy(action) {
  if (accountState.busy) return;
  accountState.busy = true;
  setAccountError('');
  document.querySelectorAll('#account-main button, #account-navigation button').forEach(node => { node.disabled = true; });
  try { await action(); }
  catch (error) { setAccountError(error.message || 'Please try again.'); }
  finally {
    accountState.busy = false;
    document.querySelectorAll('#account-main button, #account-navigation button').forEach(node => { node.disabled = false; });
  }
}
function reuseNote() {
  return accountState.draft?.complete ? '<div class="reuse-note"><strong>Your saved profile is already here.</strong>Your existing preferences and background are loaded below. Applications, contacts, preparation notes, and completion history stay in your workspace.</div>' : '';
}
function stepList() {
  const current = accountState.step === 'preferences' ? 2 : accountState.account?.authenticated ? 1 : 0;
  return '<ol class="step-list">' + [
    ['Your account','A private place for your search'],
    ['Your background','The experience you bring'],
    ['Your preferences','Make the search work for you'],
  ].map(([label, description], index) => `<li class="${index === current ? 'active' : index < current ? 'done' : ''}" ${index === current ? 'aria-current="step"' : ''}><span class="step-number" aria-hidden="true">${index < current ? '✓' : index + 1}</span><span><strong>${label}</strong><small>${description}</small></span></li>`).join('') + '</ol>';
}
function accountStory() {
  const authenticated = accountState.account?.authenticated;
  return `<aside class="account-story"><div class="eyebrow">YOUR NEXT CHAPTER</div><h1>${authenticated ? 'A search that starts with you.' : 'Your next move, with intention.'}</h1><p class="story-description">${authenticated ? 'Bring your experience and priorities together. Jobflow turns them into a focused search and a plan you can follow.' : 'Find relevant opportunities, keep your applications in order, and make room to prepare.'}</p>${stepList()}<div class="story-note"><span class="story-symbol" aria-hidden="true">✧</span><strong>Your experience stays yours.</strong>Your profile and job-search records belong to your private account. You choose what to add and when to connect a service.</div></aside>`;
}
function signInCard() {
  if (accountState.step === 'code') return `<section class="account-card"><div class="eyebrow">CHECK YOUR EMAIL</div><h2>Welcome back.</h2><p class="card-description">If <strong>${accountEsc(accountState.email)}</strong> has an invitation, a sign-in code is on its way. Enter that code to open your workspace.</p><form id="verify-form"><div class="form-field"><label for="signin-code">Email code</label><input id="signin-code" class="code-input" name="token" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6,10}" minlength="6" maxlength="10" required aria-describedby="code-help"><small id="code-help">Use the most recent code. It expires after a short time.</small></div><p id="account-error" class="form-error" role="alert" tabindex="-1">${accountEsc(accountState.error)}</p><button class="button primary full-width" type="submit">Open my workspace <span aria-hidden="true">↗</span></button></form><div class="button-row"><button class="text-button" data-account-action="change-email">Use a different email</button><button class="text-button" data-account-action="resend">Send a new code</button></div></section>`;
  return `<section class="account-card"><div class="eyebrow">WELCOME TO JOBFLOW</div><h2>A place for your next chapter.</h2><p class="card-description">Sign in with your invited email address. Your profile, saved opportunities, and progress will be waiting for you.</p><form id="signin-form"><div class="form-field"><label for="signin-email">Email address</label><input id="signin-email" name="email" type="email" maxlength="254" autocomplete="email" placeholder="you@example.com" value="${accountEsc(accountState.email)}" required><small>We’ll send a one-time code. No password to remember.</small></div><p id="account-error" class="form-error" role="alert" tabindex="-1">${accountEsc(accountState.error)}</p><button class="button primary full-width" type="submit">Continue with email <span aria-hidden="true">→</span></button></form><p class="fine-print">Jobflow is currently available by invitation. Use the email address your workspace owner invited.</p></section>`;
}
function uploadList() {
  const uploads = accountState.draft.uploads || [];
  if (!uploads.length) return '';
  return '<ul class="upload-list" aria-label="Saved resume files">' + uploads.map(upload => `<li>↳ ${accountEsc(upload.filename || upload.name || 'Resume file')} <span>· saved privately</span></li>`).join('') + '</ul>';
}
function pendingUpload() {
  const upload = accountState.pendingUpload;
  if (!upload) return '';
  const pending = accountState.pendingSuggestions;
  if (upload.extraction_status === 'needs_text' || !pending.length) return `<div class="pending-upload"><h3>File saved. Add the text to review.</h3><p class="help">${upload.extraction_status === 'needs_text' ? 'This version stores PDF files privately but does not extract PDF text. Copy the relevant text from your resume below.' : 'No text statements were extracted. Paste your background below to add it to your profile.'}</p><p class="upload-name">${accountEsc(upload.filename)}</p><div class="form-field"><label for="resume-pasted-text">Resume or background text</label><textarea id="resume-pasted-text" rows="7" maxlength="100000" placeholder="Paste your experience and accomplishments here.">${accountEsc(upload.text || '')}</textarea><small>Separate statements with a blank line. You’ll review them before adding them.</small></div><button class="button" type="button" data-account-action="review-paste">Review statements</button></div>`;
  return `<div class="pending-upload"><h3>Choose what belongs in your profile.</h3><p class="help">These statements come from your uploaded or pasted text. Select the ones to keep, then edit them below before saving.</p><p class="upload-name">${accountEsc(upload.filename || 'Pasted background')}</p><div class="pending-list">${pending.map((item, index) => `<label class="pending-choice"><input type="checkbox" data-pending="${index}"><span>${accountEsc(item.text)}</span></label>`).join('')}</div><div class="button-row"><button type="button" class="text-button" data-account-action="select-suggestions">Select all</button><button type="button" class="button" data-account-action="add-suggestions">Add selected statements</button></div></div>`;
}
function evidenceFields() {
  const evidence = accountState.draft.profile.evidence;
  if (!evidence.length) return '<p class="empty-statements">Add a resume, paste your background, or write a statement about your experience. Only the statements you review and save will be used in your profile.</p>';
  return evidence.map((item, index) => `<section class="statement" data-evidence="${index}"><div class="statement-top"><span>STATEMENT ${index + 1}</span><button class="remove-button" type="button" data-account-action="remove-statement" data-index="${index}" aria-label="Remove statement ${index + 1}">Remove</button></div><label class="field-label" for="evidence-text-${index}">Experience or accomplishment</label><textarea id="evidence-text-${index}" data-evidence-field="text" rows="3" required>${accountEsc(item.text || '')}</textarea><div class="form-row"><div class="form-field"><label for="evidence-tags-${index}">Skills in this statement</label><input id="evidence-tags-${index}" data-evidence-field="tags" value="${accountEsc((item.tags || []).join(', '))}" placeholder="Python, distributed systems"></div><div class="form-field"><label for="evidence-source-${index}">Source or reference (optional)</label><input id="evidence-source-${index}" data-evidence-field="source_url" value="${accountEsc(item.source_url || '')}" placeholder="https://…"></div></div></section>`).join('');
}
function profileCard() {
  const profile = accountState.draft.profile;
  return `<section class="account-card"><div class="eyebrow">YOUR BACKGROUND</div><h2>${accountState.draft.complete ? 'Make yourself at home.' : 'Bring your experience.'}</h2><p class="card-description">Review the experience you want Jobflow to work with. Keep your accomplishments in your own words.</p>${reuseNote()}<form id="profile-form"><div class="form-field"><label for="profile-name">Your name</label><input id="profile-name" name="name" maxlength="200" autocomplete="name" value="${accountEsc(profile.name)}" required></div><div class="form-field"><label for="profile-headline">Professional focus</label><input id="profile-headline" maxlength="2000" value="${accountEsc(profile.headline)}" placeholder="e.g. Software engineer focused on infrastructure"></div><div class="form-field"><label for="profile-skills">Skills</label><input id="profile-skills" value="${accountEsc(profile.skills.join(', '))}" placeholder="e.g. Python, distributed systems, engineering leadership"><small>Separate skills with commas. These help match your experience to roles.</small></div><div class="upload-box"><h3>Add a resume</h3><p>TXT, Markdown, DOCX, or PDF · up to 5 MB. PDF text can be pasted after upload.</p><label class="field-label" for="resume-file">Choose a file</label><input id="resume-file" type="file" accept=".txt,.md,.docx,.pdf"><button class="button" type="button" data-account-action="upload">Upload and review</button>${uploadList()}${accountState.uploadMessage ? `<p class="upload-status" role="status">${accountEsc(accountState.uploadMessage)}</p>` : ''}</div>${pendingUpload()}<details class="background-details"><summary>Add background without a file</summary><div class="form-field"><label for="background-text">Experience and accomplishments</label><textarea id="background-text" rows="5" maxlength="100000" placeholder="Paste or write statements from your background."></textarea><small>Separate statements with a blank line. You can review and edit each one.</small></div><button class="button" type="button" data-account-action="review-background">Review statements</button></details><div class="statement-section"><div class="statement-heading"><h3>Your reviewed background</h3><button class="text-button" type="button" data-account-action="add-statement">+ Add statement</button></div><p class="help">These statements support your matches and drafts. Saving keeps your existing application and preparation history.</p><div id="evidence-fields">${evidenceFields()}</div></div><p id="account-error" class="form-error" role="alert" tabindex="-1">${accountEsc(accountState.error)}</p><div class="button-row"><span class="help">Step 2 of 3</span><button class="button primary" type="submit">Review preferences <span aria-hidden="true">→</span></button></div></form></section>`;
}
function preferencesCard() {
  const prefs = accountState.draft.preferences;
  return `<section class="account-card"><div class="eyebrow">YOUR PREFERENCES</div><h2>Focus on what matters to you.</h2><p class="card-description">Choose the opportunities you want to see and the time you want to set aside. You can come back and adjust these later.</p><form id="preferences-form"><div class="form-field"><label for="preferred-locations">Preferred locations</label><textarea id="preferred-locations" rows="3" placeholder="San Francisco\nRemote, US">${accountEsc(prefs.preferred_locations.join('\n'))}</textarea><small>One location per line, including remote preferences.</small></div><div class="form-field"><label for="location-mode">Location filtering</label><select id="location-mode">${[['any','Any location'],['custom','My preferred locations'],['bay_area_remote','Bay Area or US remote'],['us','United States'],['anywhere','Any location (existing setting)'],['all_us','United States (existing setting)']].map(([value,label]) => `<option value="${value}" ${prefs.location_mode === value ? 'selected' : ''}>${label}</option>`).join('')}</select></div><fieldset class="form-field" style="border:0;padding:0"><legend class="field-label">Role levels</legend><div class="level-choices">${[['entry','Entry-level'],['mid','Mid-level'],['senior','Senior'],['staff','Staff'],['principal','Principal'],['management','Management'],['unspecified','Unspecified']].map(([value,label]) => `<label class="level-choice"><input type="checkbox" name="allowed_levels" value="${value}" ${prefs.allowed_levels.includes(value) ? 'checked' : ''}>${label}</label>`).join('')}</div></fieldset><div class="form-row"><div class="form-field"><label for="target-compensation">Target annual total compensation</label><input id="target-compensation" type="number" min="0" max="100000000" step="any" value="${accountEsc(prefs.target_tc)}" required><small>Salary, bonus, and equity combined. Use 0 for no target.</small></div><div class="form-field"><label for="compensation-currency">Currency</label><input id="compensation-currency" value="${accountEsc(prefs.currency)}" readonly aria-describedby="currency-help"><small id="currency-help">Matching currently uses USD compensation targets.</small></div></div><div class="form-row"><div class="form-field"><label for="daily-minutes">Daily preparation time</label><input id="daily-minutes" type="number" min="15" max="480" step="1" value="${accountEsc(prefs.daily_minutes)}" required><small>Minutes for learning and interview practice.</small></div><div class="form-field"><label for="profile-timezone">Your time zone</label><input id="profile-timezone" list="timezones" value="${accountEsc(prefs.timezone)}" maxlength="100" required><datalist id="timezones"><option value="America/Los_Angeles"><option value="America/New_York"><option value="America/Chicago"><option value="Europe/London"><option value="Europe/Paris"><option value="Asia/Kolkata"><option value="Asia/Singapore"><option value="Asia/Tokyo"><option value="Australia/Sydney"><option value="UTC"></datalist><small>Used for your daily schedule.</small></div></div><div class="preferences-summary"><h3>${accountState.draft.complete ? 'Your progress carries forward.' : 'A foundation for your first search.'}</h3><p>${accountState.draft.complete ? 'Saving updates the profile and preferences you reviewed. Your saved jobs, applications, contacts, and completed work remain in place.' : 'Your profile and preferences will shape your matches. Scheduled searches use the employer sources available in this workspace.'}</p></div><p class="connection-note"><strong>Service connections are optional.</strong> You can use your profile and job tracker without connecting email or a calendar. Account context already imported into your workspace is preserved.</p><p id="account-error" class="form-error" role="alert" tabindex="-1">${accountEsc(accountState.error)}</p><div class="button-row"><button type="button" class="button" data-account-action="back-profile">← Your background</button><button class="button primary" type="submit">${accountState.draft.complete ? 'Save and open workspace' : 'Create my workspace'} <span aria-hidden="true">↗</span></button></div></form></section>`;
}
function renderAccount() {
  const account = accountState.account;
  accountNode('#account-navigation').innerHTML = account?.authenticated
    ? `<span class="account-email">${accountEsc(account.email)}</span>${accountState.draft?.complete ? '<a href="/" class="button ghost">Workspace ↗</a>' : ''}<button class="button ghost" type="button" data-account-action="logout">Sign out</button>`
    : '<span class="private-label"><span aria-hidden="true">◌</span> Your private workspace</span>';
  accountNode('#account-main').innerHTML = `<div class="account-layout">${accountStory()}${!account?.authenticated ? signInCard() : accountState.step === 'preferences' ? preferencesCard() : profileCard()}</div>`;
  document.title = account?.authenticated ? 'Your profile · Jobflow' : 'Sign in · Jobflow';
}
function focusAccountMain() {
  accountNode('#account-main').focus();
  window.scrollTo({top:0, behavior:'instant'});
}
function captureProfile() {
  if (!accountNode('#profile-form')) return;
  const profile = accountState.draft.profile;
  profile.name = accountNode('#profile-name').value.trim();
  profile.headline = accountNode('#profile-headline').value.trim();
  profile.skills = splitValues(accountNode('#profile-skills').value);
  document.querySelectorAll('[data-evidence]').forEach(node => {
    const index = Number(node.dataset.evidence), item = profile.evidence[index];
    if (!item) return;
    item.text = node.querySelector('[data-evidence-field="text"]').value;
    item.tags = splitValues(node.querySelector('[data-evidence-field="tags"]').value);
    item.source_url = node.querySelector('[data-evidence-field="source_url"]').value.trim();
  });
}
function capturePreferences() {
  if (!accountNode('#preferences-form')) return;
  Object.assign(accountState.draft.preferences, {
    preferred_locations:accountNode('#preferred-locations').value.split(/\r?\n/).map(value => value.trim()).filter(Boolean),
    location_mode:accountNode('#location-mode').value,
    allowed_levels:Array.from(document.querySelectorAll('input[name="allowed_levels"]:checked')).map(node => node.value),
    target_tc:Number(accountNode('#target-compensation').value),
    currency:accountNode('#compensation-currency').value.toUpperCase().trim(),
    daily_minutes:Number(accountNode('#daily-minutes').value),
    timezone:accountNode('#profile-timezone').value.trim(),
  });
}
async function acceptAccount(account) {
  if (!account.authenticated) throw new Error('Sign-in could not be completed. Request a new code and try again.');
  accountState.account = account;
  const onboarding = account.onboarding || await accountAPI('/api/onboarding');
  accountState.draft = draftFromOnboarding(onboarding);
  accountState.error = ''; accountState.dirty = false;
  if (onboarding.complete && !editAccount()) { location.replace('/'); return; }
  accountState.step = 'profile'; renderAccount(); focusAccountMain();
}
async function initializeAccount() {
  try {
    const account = await accountAPI('/api/account');
    if (account.authenticated) await acceptAccount(account);
    else { accountState.account = account; renderAccount(); }
  } catch (error) {
    accountNode('#account-main').innerHTML = `<section class="account-card failure"><div class="eyebrow">LET’S RECONNECT</div><h1>Your account is taking a moment.</h1><p>${accountEsc(error.message)}</p><button class="button primary" data-account-action="retry">Try again</button></section>`;
  }
}
function fileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',',2)[1]);
    reader.onerror = () => reject(new Error('This file could not be read. Please select it again.'));
    reader.readAsDataURL(file);
  });
}
async function accountAction(action, trigger) {
  if (accountState.busy) return;
  setAccountError('');
  if (action === 'retry') return initializeAccount();
  if (action === 'change-email') { accountState.step = 'email'; renderAccount(); accountNode('#signin-email').focus(); return; }
  if (action === 'resend') return withAccountBusy(async () => {
    await accountAPI('/api/auth/start', {email:accountState.email});
    accountNotice('If this email is invited, a new code is on its way.');
  });
  if (action === 'logout') return withAccountBusy(async () => {
    await accountAPI('/api/auth/logout', {});
    accountState.account = null; accountState.draft = null; accountState.pendingUpload = null;
    accountState.pendingSuggestions = []; accountState.step = 'email'; accountState.dirty = false;
    renderAccount(); focusAccountMain();
  });
  if (action === 'back-profile') { capturePreferences(); accountState.step = 'profile'; renderAccount(); focusAccountMain(); return; }
  if (!accountState.account?.authenticated || !accountState.draft) return;
  captureProfile();
  if (action === 'upload') return withAccountBusy(async () => {
    const file = accountNode('#resume-file').files[0];
    if (!file) throw new Error('Choose a resume file first.');
    if (!/\.(txt|md|docx|pdf)$/i.test(file.name)) throw new Error('Choose a TXT, Markdown, DOCX, or PDF file.');
    if (file.size > 5 * 1024 * 1024 || !file.size) throw new Error('Choose a non-empty file no larger than 5 MB.');
    const upload = await accountAPI('/api/resume', {filename:file.name, content_base64:await fileBase64(file)});
    accountState.pendingUpload = upload; accountState.pendingSuggestions = normalizedSuggestions(upload);
    accountState.draft.uploads.push({id:upload.id, filename:upload.filename || file.name});
    accountState.uploadMessage = 'File saved privately. Review its contents before adding statements to your profile.';
    renderAccount();
    accountNode('.pending-upload')?.scrollIntoView({block:'center'});
  });
  if (action === 'review-background' || action === 'review-paste') {
    const input = accountNode(action === 'review-background' ? '#background-text' : '#resume-pasted-text');
    const sourceId = action === 'review-paste' ? accountState.pendingUpload?.id || '' : '';
    const suggestions = textSuggestions(input?.value || '', sourceId);
    if (!suggestions.length) { setAccountError('Add some background text to review first.'); return; }
    accountState.pendingUpload = {id:sourceId, filename:action === 'review-paste' ? accountState.pendingUpload?.filename || 'Pasted resume text' : 'Pasted background', extraction_status:'review'};
    accountState.pendingSuggestions = suggestions; renderAccount();
    accountNode('.pending-upload')?.scrollIntoView({block:'center'}); return;
  }
  if (action === 'select-suggestions') {
    document.querySelectorAll('[data-pending]').forEach(node => { node.checked = true; }); return;
  }
  if (action === 'add-suggestions') {
    const selected = Array.from(document.querySelectorAll('[data-pending]:checked')).map(node => accountState.pendingSuggestions[Number(node.dataset.pending)]).filter(Boolean);
    if (!selected.length) { setAccountError('Select at least one statement to add.'); return; }
    const added = appendSuggestions(accountState.draft, selected);
    accountState.pendingUpload = null; accountState.pendingSuggestions = []; accountState.dirty = true;
    renderAccount(); accountNotice(`${added} statement${added === 1 ? '' : 's'} added for your review.`);
    accountNode('#evidence-fields')?.scrollIntoView({block:'start'}); return;
  }
  if (action === 'add-statement') {
    // Empty text is intentional for a new statement; it must be filled before saving.
    accountState.draft.profile.evidence.push({id:'manual-' + Date.now().toString(36) + '-' + accountState.draft.profile.evidence.length, text:'', tags:[], source_url:''});
    accountState.dirty = true; renderAccount();
    accountNode('#evidence-text-' + (accountState.draft.profile.evidence.length - 1))?.focus(); return;
  }
  if (action === 'remove-statement') {
    const index = Number(trigger.dataset.index);
    if (Number.isInteger(index) && index >= 0 && index < accountState.draft.profile.evidence.length) {
      accountState.draft.profile.evidence.splice(index, 1); accountState.dirty = true; renderAccount();
    }
  }
}
async function submitAccountForm(form) {
  if (accountState.busy) return;
  if (form.id === 'signin-form') return withAccountBusy(async () => {
    accountState.email = accountNode('#signin-email').value.trim().toLowerCase();
    await accountAPI('/api/auth/start', {email:accountState.email});
    accountState.step = 'code'; renderAccount(); accountNode('#signin-code').focus();
  });
  if (form.id === 'verify-form') return withAccountBusy(async () => {
    await acceptAccount(await accountAPI('/api/auth/verify', {email:accountState.email, token:accountNode('#signin-code').value.trim()}));
  });
  if (form.id === 'profile-form') {
    captureProfile();
    if (!accountState.draft.profile.evidence.length) { setAccountError('Add at least one statement from your background before continuing.'); return; }
    accountState.step = 'preferences'; accountState.error = ''; renderAccount(); focusAccountMain(); return;
  }
  if (form.id === 'preferences-form') return withAccountBusy(async () => {
    capturePreferences();
    if (!accountState.draft.preferences.allowed_levels.length) throw new Error('Choose at least one role level.');
    try { new Intl.DateTimeFormat('en-US', {timeZone:accountState.draft.preferences.timezone}).format(); }
    catch { throw new Error('Enter a valid time zone, such as America/Los_Angeles.'); }
    await accountAPI('/api/onboarding', draftPayload(accountState.draft));
    accountState.dirty = false; location.assign('/');
  });
}

// Bootstrap browser session.
document.addEventListener('click', event => {
  const trigger = event.target.closest('[data-account-action]');
  if (trigger) accountAction(trigger.dataset.accountAction, trigger).catch(error => setAccountError(error.message));
});
document.addEventListener('submit', event => {
  if (['signin-form','verify-form','profile-form','preferences-form'].includes(event.target.id)) {
    event.preventDefault(); submitAccountForm(event.target).catch(error => setAccountError(error.message));
  }
});
document.addEventListener('input', event => {
  if (event.target.closest('#profile-form, #preferences-form')) accountState.dirty = true;
});
initializeAccount();

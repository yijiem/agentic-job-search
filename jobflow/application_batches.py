"""Durable, explicitly reviewed browser application batches.

This module does not contact employers. The browser companion inspects forms and
reports observed results; only a reviewed authorization releases resume bytes for
submission. All mutations run under the CLI's existing workspace lock.
"""
import base64
import binascii
import copy
import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .storage import utcnow
from .tracking import validate_record

MAX_RESUME_BYTES = 5 * 1024 * 1024
MAX_BATCH = 100
LEASE_SECONDS = 600
WORKER_SEEN_SECONDS = 90
WORKER_RELOAD_SECONDS = 300
WORKER_KIND = 'application_workers'
BATCH_KIND = 'application_batches'
RESUME_KIND = 'application_resumes'
TERMINAL = frozenset(('submitted', 'skipped'))
BUSY = frozenset(('inspecting', 'running', 'submitting'))
IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')
APPLICANT_FIELDS = frozenset(('first_name', 'last_name', 'email', 'phone', 'location', 'linkedin', 'website'))


def _id(value, label='record'):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError('Choose a valid %s ID.' % label)
    return value


def _text(value, label, maximum=10000, required=False):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value or (required and not value.strip()):
        raise ValueError('%s must be %stext of at most %s characters.' % (label, 'nonempty ' if required else '', maximum))
    return value


def _url(value):
    _text(value, 'Application URL', 4096, True)
    parsed = urlsplit(value)
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or not host or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Browser applications require a public HTTPS application URL.')
    if host in ('localhost', 'localhost.localdomain') or host.endswith(('.localhost', '.local', '.internal')) or '.' not in host:
        raise ValueError('Private network application URLs are not supported.')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if all(re.fullmatch(r'(?:[0-9]+|0x[0-9a-f]+)', part) for part in host.split('.')):
            raise ValueError('Nonstandard numeric application hosts are not supported.')
    else:
        if not address.is_global:
            raise ValueError('Private network application URLs are not supported.')
    return value


def _answers(value):
    if not isinstance(value, dict) or len(value) > 200:
        raise ValueError('Answers must be an object with at most 200 fields.')
    result = {}
    for key, answer in value.items():
        _text(key, 'Question key', 500, True)
        if isinstance(answer, bool):
            result[key] = answer
        elif isinstance(answer, str):
            result[key] = _text(answer, 'Answer', 10000)
        elif isinstance(answer, list) and len(answer) <= 100 and all(isinstance(v, str) for v in answer):
            result[key] = [_text(v, 'Answer option', 1000) for v in answer]
        else:
            raise ValueError('Answers must contain text, booleans, or lists of text.')
    return result


def _applicant(value):
    if not isinstance(value, dict) or set(value) - APPLICANT_FIELDS:
        raise ValueError('Applicant details contain unsupported fields.')
    return {key: _text(answer, 'Applicant ' + key, 1000) for key, answer in value.items()}


def _fields(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 200:
        raise ValueError('Inspect between 1 and 200 application fields.')
    result, seen = [], set()
    for field in value:
        if not isinstance(field, dict):
            raise ValueError('Application fields must be objects.')
        key = _text(field.get('key'), 'Question key', 500, True)
        if key in seen:
            raise ValueError('Question keys must be unique within the form.')
        seen.add(key)
        options = field.get('options', [])
        if not isinstance(options, list) or len(options) > 1000:
            raise ValueError('Question options must be a bounded list.')
        if not isinstance(field.get('required', False), bool) or not isinstance(field.get('supported', True), bool):
            raise ValueError('Field required and supported flags must be booleans.')
        result.append({'key': key, 'label': _text(field.get('label', key), 'Question label', 1000),
                       'type': _text(field.get('type', 'text'), 'Question type', 40, True),
                       'required': field.get('required', False), 'supported': field.get('supported', True),
                       'options': [_text(v, 'Question option', 1000) for v in options]})
    return result


def _present(value):
    return value is True or isinstance(value, (str, list)) and bool(value)


def _missing(fields, answers, resume):
    missing = []
    for field in fields:
        value = answers.get(field['key'])
        has_value = bool(resume) if field['type'] == 'file' and _present(value) else _present(value)
        if field['required'] and not has_value:
            missing.append(field['label'])
        elif not field['supported']:
            missing.append(field['label'] + ' (complete in browser)')
        elif field['type'] == 'checkbox' and not isinstance(value, bool):
            missing.append(field['label'] + ' (choose yes or no)')
        elif has_value and field['type'] != 'checkbox' and not isinstance(value, str):
            missing.append(field['label'] + ' (enter a single value)')
        elif has_value and field['type'] == 'email' and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
            missing.append(field['label'] + ' (enter a valid email address)')
        elif has_value and field['type'] == 'file' and not re.search(r'resume|curriculum vitae|\bcv\b', field['label'], re.I):
            missing.append(field['label'] + ' (additional documents require the employer form)')
        elif has_value and field['type'] == 'file' and value != resume['filename']:
            missing.append(field['label'] + ' (choose the reviewed resume)')
        elif has_value and field['options'] and field['type'] not in ('file', 'checkbox'):
            values = value if isinstance(value, list) else [value]
            if any(v not in field['options'] for v in values):
                missing.append(field['label'] + ' (choose a listed option)')
    return missing


def _codex_missing(fields, answers, resume):
    """Validate an independently observed Codex form, including multiselects."""
    missing = []
    for field in fields:
        if field['type'] != 'multiselect':
            missing.extend(_missing([field], answers, resume))
            continue
        value = answers.get(field['key'], [])
        if not field['supported']:
            missing.append(field['label'] + ' (inspect the browser control)')
        elif not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            missing.append(field['label'] + ' (choose a list of options)')
        elif field['required'] and not value:
            missing.append(field['label'])
        elif any(v not in field['options'] for v in value) or len(value) != len(set(value)):
            missing.append(field['label'] + ' (choose listed options once each)')
    return missing


def _codex_status(batch):
    run = batch.get('codex_run')
    if not run:
        return
    items = [item for item in batch['items'] if item['job_id'] in run['job_ids']]
    if batch['status'] in ('paused', 'cancelled'):
        status = batch['status']
    elif items and all(item['status'] in TERMINAL for item in items):
        status = 'complete'
    elif any(item['status'] == 'uncertain' for item in items):
        status = 'uncertain'
    elif not run.get('worker_id'):
        status = 'awaiting_start'
    elif any(item['status'] in BUSY or item['status'] in ('pending', 'queued') for item in items):
        status = 'running'
    elif any(item['status'] == 'ready' for item in items):
        status = 'ready'
    else:
        status = 'needs_input'
    run['status'] = status


def _public_codex_run(run):
    return {**{key: copy.deepcopy(value) for key, value in run.items() if key not in ('materials', 'worker_id')},
            'started': bool(run.get('worker_id'))}


def workspace_id(root):
    from .remote import workspace_id as remote_workspace_id
    remote_id = remote_workspace_id(root, required=False)
    if remote_id:
        return remote_id
    return hashlib.sha256(str(Path(root).resolve()).encode('utf-8')).hexdigest()[:32]


def worker_release():
    """Report packaged files, never a version supplied by the website/client."""
    directory = Path(__file__).with_name('browser_worker')
    if not directory.is_dir():
        directory = Path(__file__).resolve().parents[1] / 'browser_worker'
    try:
        value = json.loads((directory / 'manifest.json').read_text(encoding='utf-8')).get('version')
        return {'version': value if isinstance(value, str) else None}
    except (OSError, ValueError):
        return {'version': None}


def _worker_request(record, now=None):
    request = copy.deepcopy((record or {}).get('reload_request'))
    if not request:
        return None
    if request.get('status') in ('pending', 'reloading'):
        try:
            expires = datetime.fromisoformat(request['expires_at'])
            expired = expires <= (now or datetime.now(timezone.utc))
        except (KeyError, TypeError, ValueError):
            expired = True
        if expired:
            request.update(status='expired', message='The reload request expired. Reconnect the worker and try again.')
    return request


def _public_worker(record, now=None):
    if not record:
        return None
    result = copy.deepcopy(record)
    result['reload_request'] = _worker_request(record, now)
    return result


def _worker_record(store, worker):
    record = store.get(WORKER_KIND, worker)
    if not record:
        current = store.get('application_worker', 'current')
        if current and current.get('worker_id') == worker:
            record = {**current, 'id': worker}
    return _public_worker(record)


def _save_worker(store, record, make_current=False):
    groups = {WORKER_KIND: [record]}
    current = store.get('application_worker', 'current')
    if make_current or not current or current.get('worker_id') == record['worker_id']:
        groups['application_worker'] = [{**record, 'id': 'current'}]
    store.put_groups(groups)


def _worker_response(record):
    return {'ok': True, 'worker': _public_worker(record), 'worker_release': worker_release(),
            'reload_request': _worker_request(record)}


def _worker_heartbeat(store, payload):
    worker = _id(payload.get('worker_id'), 'worker')
    version = payload.get('version')
    if not isinstance(version, str) or len(version) > 64 or not re.fullmatch(r'\d+(?:\.\d+){0,3}', version):
        raise ValueError('Report a valid browser worker version.')
    for name in ('reload_supported', 'enabled', 'active'):
        if not isinstance(payload.get(name), bool):
            raise ValueError('Worker %s must be a boolean.' % name)
    completed_id = payload.get('reload_completed_id')
    failed_id = payload.get('reload_failed_id')
    if completed_id is not None and failed_id is not None:
        raise ValueError('Report either a reload completion or a reload failure, not both.')
    record = _worker_record(store, worker) or {'id': worker, 'worker_id': worker}
    record.update(version=version, reload_supported=payload['reload_supported'], enabled=payload['enabled'],
                  active=payload['active'], last_seen_at=utcnow())
    if completed_id is not None:
        _id(completed_id, 'reload request')
        request = _worker_request(record)
        if not request or request['id'] != completed_id or request.get('worker_id') != worker:
            raise ValueError('This reload completion does not match the browser worker request.')
        if request['status'] == 'reloading':
            complete = version == request['target_version']
            request.update(status='complete' if complete else 'outdated', completed_at=utcnow(),
                           reported_version=version,
                           message='Browser worker reloaded with the current version.' if complete else
                           'The worker reloaded, but its installed files are still older. Replace the extracted files with the latest download, then reload again.')
            record['reload_request'] = request
        elif request['status'] not in ('complete', 'outdated', 'expired'):
            raise ValueError('This reload request has not been acknowledged by the worker.')
    if failed_id is not None:
        _id(failed_id, 'reload request')
        request = _worker_request(record)
        if not request or request['id'] != failed_id or request.get('worker_id') != worker:
            raise ValueError('This reload failure does not match the browser worker request.')
        if request['status'] in ('pending', 'reloading'):
            request.update(status='failed', completed_at=utcnow(),
                           message='The worker could not finish reloading. Check its connection and request another reload.')
            record['reload_request'] = request
    _save_worker(store, record, make_current=True)
    return _worker_response(record)


def _worker_reload(store, payload):
    worker = _id(payload.get('worker_id'), 'worker')
    record = _worker_record(store, worker)
    if not record or record.get('reload_supported') is not True:
        raise ValueError('Update this browser worker manually once to enable reloading from Jobflow.')
    try:
        recent = datetime.now(timezone.utc) - datetime.fromisoformat(record['last_seen_at']) < timedelta(seconds=WORKER_SEEN_SECONDS)
    except (KeyError, TypeError, ValueError):
        recent = False
    if not recent:
        raise ValueError('The browser worker is not connected. Open its popup, reconnect, and try again.')
    if _worker_request(record) and record['reload_request']['status'] in ('pending', 'reloading'):
        return _worker_response(record)
    version = worker_release()['version']
    if not version:
        raise ValueError('The latest browser worker release is unavailable in this installation.')
    now = datetime.now(timezone.utc)
    record['reload_request'] = {'id': 'reload-' + secrets.token_hex(16), 'worker_id': worker,
                                'status': 'pending', 'target_version': version, 'requested_at': now.isoformat(),
                                'expires_at': (now + timedelta(seconds=WORKER_RELOAD_SECONDS)).isoformat(),
                                'message': 'Waiting for the connected worker to finish its current application step before reloading.'}
    _save_worker(store, record)
    return _worker_response(record)


def _worker_reload_ack(store, payload):
    worker = _id(payload.get('worker_id'), 'worker')
    request_id = _id(payload.get('request_id'), 'reload request')
    record = _worker_record(store, worker)
    request = _worker_request(record)
    if not request or request['id'] != request_id or request.get('worker_id') != worker:
        raise ValueError('This reload request does not belong to the connected worker.')
    if request['status'] not in ('pending', 'reloading'):
        raise ValueError('This reload request is no longer pending. Request another reload in Jobflow.')
    if record.get('active') is not False or any(
            item.get('worker_id') == worker and item.get('status') in BUSY
            for batch in store.all(BATCH_KIND) for item in batch.get('items', [])):
        raise ValueError('The worker has an active application step. Reload will wait until it finishes.')
    request.update(status='reloading', message='Reloading the browser worker; waiting for its version check.')
    record['reload_request'] = request
    _save_worker(store, record)
    return _worker_response(record)


def _public_batch(batch):
    result = copy.deepcopy(batch)
    _codex_status(result)
    if result.get('codex_run'):
        result['codex_run'] = _public_codex_run(result['codex_run'])
    if result.get('codex_history'):
        result['codex_history'] = [_public_codex_run(run) for run in result['codex_history']]
    for item in result.get('items', []):
        item.pop('attempt_id', None)
        item.pop('authorization', None)
    return result


def _read_kinds(root):
    path = Path(root) / 'data/jobflow.sqlite3'
    result = {BATCH_KIND: [], RESUME_KIND: [], 'application_worker': []}
    from .remote_files import is_remote_root
    if is_remote_root(root):
        from .storage import Store
        store = Store(path)
        try:
            return {kind: store.all(kind) for kind in result}
        finally:
            store.close()
    if not path.exists():
        return result
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    try:
        for kind, body in connection.execute("SELECT kind,body FROM records WHERE kind IN (?,?,?) ORDER BY created_at,id", tuple(result)):
            result[kind].append(json.loads(body))
    finally:
        connection.close()
    return result


def batch_snapshot(root):
    records = _read_kinds(root)
    from .resumes import resume_library
    return {'workspace_id': workspace_id(root),
            'batches': [_public_batch(b) for b in reversed(records[BATCH_KIND])],
            **resume_library(root),
            'worker': _public_worker(next(iter(records['application_worker']), None)),
            'worker_release': worker_release()}


def batch_detail(root, batch_id):
    _id(batch_id, 'batch')
    return next((batch for batch in batch_snapshot(root)['batches'] if batch['id'] == batch_id), None)


def _resume_directory(root, create=False):
    root = Path(root).resolve()
    directory = root / 'inputs/application-resumes'
    # Refuse symlinked private storage rather than writing through user-created links.
    for component in (root / 'inputs', directory):
        if component.is_symlink():
            raise ValueError('Resume storage must not be a symbolic link.')
        if create:
            component.mkdir(mode=0o700, exist_ok=True)
    if directory.resolve().parent != (root / 'inputs').resolve():
        raise ValueError('Resume storage is outside this workspace.')
    return directory


def _resume_record(store, value):
    record = store.get(RESUME_KIND, _id(value, 'resume'))
    if not record:
        raise ValueError('Choose a resume uploaded to this workspace.')
    return record


def _resume_bytes(root, record):
    directory = _resume_directory(root)
    path = directory / record['id']
    from .remote_files import ensure_path
    path = ensure_path(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(str(path), flags)
        with os.fdopen(descriptor, 'rb') as stream:
            content = stream.read(MAX_RESUME_BYTES + 1)
    except OSError:
        raise ValueError('The saved resume is unavailable; upload it again.') from None
    if len(content) != record['size'] or hashlib.sha256(content).hexdigest() != record['sha256']:
        raise ValueError('The saved resume changed; upload and review it again.')
    return content


def resume_content(root, resume_id):
    """Return an owned resume for authenticated preview, with integrity checks."""
    _id(resume_id, 'resume')
    record = next((row for row in _read_kinds(root)[RESUME_KIND] if row['id'] == resume_id), None)
    if not record:
        raise ValueError('This resume does not exist in this workspace.')
    return record, _resume_bytes(root, record)


def _upload(root, store, payload):
    filename = _text(payload.get('filename'), 'Resume filename', 200, True)
    if filename != Path(filename).name or '/' in filename or '\\' in filename or any(ord(c) < 32 for c in filename):
        raise ValueError('Choose a plain resume filename without a directory.')
    extension = Path(filename).suffix.lower()
    types = {'.pdf': 'application/pdf', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', '.txt': 'text/plain', '.md': 'text/plain'}
    if extension not in types:
        raise ValueError('Upload a PDF, DOCX, Markdown, or UTF-8 text resume.')
    encoded = payload.get('content_base64')
    if not isinstance(encoded, str) or not 0 < len(encoded) <= ((MAX_RESUME_BYTES + 2) // 3) * 4:
        raise ValueError('Resume files must be no larger than 5 MiB.')
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('The resume could not be decoded.') from None
    if not 0 < len(content) <= MAX_RESUME_BYTES:
        raise ValueError('Resume files must be between 1 byte and 5 MiB.')
    if extension == '.pdf' and not content.startswith(b'%PDF-'):
        raise ValueError('This file does not have a PDF signature.')
    if extension in ('.txt', '.md'):
        try:
            text = content.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise ValueError('Text resumes must use UTF-8 encoding.') from None
        if '\x00' in text:
            raise ValueError('Text resumes must not contain binary data.')
    if extension == '.docx':
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = archive.namelist()
                if len(names) > 5000 or '[Content_Types].xml' not in names or 'word/document.xml' not in names:
                    raise ValueError('This file is not a DOCX document.')
                if any(name.lower().endswith('vbaproject.bin') for name in names):
                    raise ValueError('Macro-enabled documents are not supported.')
        except zipfile.BadZipFile:
            raise ValueError('This file is not a DOCX document.') from None
    from .resumes import extract_text
    extracted_text, extraction_status = extract_text(filename, content)
    if 'text' in payload:
        from .resumes import _text as resume_text
        extracted_text = resume_text(payload['text'], 'Resume text', required=False)
        extraction_status = 'provided_text' if extracted_text.strip() else 'needs_text'
    label = _text(payload.get('label', filename), 'Resume name', 200, True)
    source_url = payload.get('source_url')
    if source_url:
        _text(source_url, 'Resume source URL', 4000, True)
        parsed_source = urlsplit(source_url)
        if parsed_source.scheme not in ('http', 'https') or not parsed_source.hostname or parsed_source.username or parsed_source.password:
            raise ValueError('Resume source must be an HTTP(S) URL without credentials.')
    existing_resumes = store.all(RESUME_KIND)
    if len(existing_resumes) >= 200 or sum(row.get('size', 0) for row in existing_resumes) + len(content) > 100 * 1024 * 1024:
        raise ValueError('The resume library has reached its limit of 200 versions or 100 MiB.')
    resume_id = 'resume-' + secrets.token_hex(16)
    record = {'id': resume_id, 'filename': filename, 'mime': types[extension], 'size': len(content),
              'sha256': hashlib.sha256(content).hexdigest(), 'created_at': utcnow(),
              'label': label, 'text': extracted_text, 'extraction_status': extraction_status,
              'version': 1, 'parent_resume_id': None, 'job_id': None}
    if source_url:
        record['source'] = {'kind': 'supplied_source', 'url': source_url}
    directory = _resume_directory(root, True)
    descriptor = os.open(str(directory / resume_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
        from .remote_files import persist_file
        persist_file(root, directory / resume_id)
        store.put(RESUME_KIND, resume_id, record)
    except BaseException:
        (directory / resume_id).unlink(missing_ok=True)
        raise
    return {'ok': True, 'resume': record}


def _event(batch, action, item=None, message=''):
    entry = {'at': utcnow(), 'action': action}
    if item:
        entry['job_id'] = item['job_id']
    if message:
        entry['message'] = message
    batch.setdefault('history', []).append(entry)


def _save(store, batch, extra=None):
    batch['revision'] += 1
    batch['updated_at'] = utcnow()
    if batch['status'] not in ('paused', 'cancelled'):
        if batch['items'] and all(item['status'] in TERMINAL for item in batch['items']):
            batch['status'] = 'complete'
        else:
            batch['status'] = 'active'
    _codex_status(batch)
    groups = {BATCH_KIND: [batch]}
    groups.update(extra or {})
    store.put_groups(groups)
    return {'ok': True, 'batch': _public_batch(batch)}


def _batch(store, payload, revision=False):
    batch = store.get(BATCH_KIND, _id(payload.get('batch_id'), 'batch'))
    if not batch:
        raise ValueError('This application batch does not exist.')
    if revision and (type(payload.get('revision')) is not int or payload['revision'] != batch['revision']):
        raise ValueError('This batch changed. Refresh and review its latest version.')
    return batch


def _item(batch, value):
    _id(value, 'job')
    row = next((item for item in batch['items'] if item['job_id'] == value), None)
    if not row:
        raise ValueError('This role is not part of the batch.')
    return row


def _application_exists(store, job_id):
    application = store.get('applications', job_id)
    return bool(application and (application.get('stage') not in (None, 'saved') or application.get('applied_at')))


def _duplicate_batch(store, job_id, batch_id=None):
    for batch in store.all(BATCH_KIND):
        if batch['id'] == batch_id:
            continue
        for item in batch['items']:
            if item['job_id'] == job_id and item['status'] != 'skipped':
                if batch['status'] != 'cancelled' or item['status'] in ('submitting', 'submitted', 'uncertain'):
                    return True
    return False


def _clear_attempt(item):
    for key in ('attempt_id', 'lease_until', 'worker_id', 'attempt_mode', 'codex_click_granted_at'):
        item.pop(key, None)


def _clear_review(item):
    _clear_attempt(item)
    for key in ('authorization', 'fields', 'form_signature', 'planned_answers', 'missing_fields', 'inspected_at', 'evidence'):
        item.pop(key, None)
    item['status'] = 'pending'
    item['message'] = 'Waiting for Codex to inspect the application form.'


def _create(store, payload):
    ids = payload.get('job_ids')
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_BATCH:
        raise ValueError('Choose between 1 and 100 roles for a batch.')
    ids = list(dict.fromkeys(_id(value, 'job') for value in ids))
    resume = _resume_record(store, payload.get('resume_id'))
    changes = payload.get('items', [])
    if not isinstance(changes, list) or len(changes) > MAX_BATCH:
        raise ValueError('Role resume overrides must be a bounded list.')
    overrides = {}
    for change in changes:
        if not isinstance(change, dict) or set(change) - {'job_id', 'resume_id'} or 'resume_id' not in change:
            raise ValueError('Role resume overrides must contain a job ID and resume ID.')
        job_id = _id(change.get('job_id'), 'job')
        if job_id not in ids:
            raise ValueError('A resume override is for a role outside this batch.')
        if job_id in overrides:
            raise ValueError('Each role may have only one resume override.')
        value = change['resume_id']
        overrides[job_id] = _resume_record(store, value)['id'] if value != '' else ''
    now = utcnow()
    batch = {'id': 'batch-' + secrets.token_hex(12), 'name': _text(payload.get('name', 'Application batch'), 'Batch name', 160, True),
             'revision': 0, 'status': 'draft', 'created_at': now, 'updated_at': now,
             'executor': 'codex',
             'resume_id': resume['id'], 'applicant': _applicant(payload.get('applicant', {})),
             'answers': _answers(payload.get('answers', {})), 'items': [], 'history': []}
    for job_id in ids:
        job = store.get('jobs', job_id)
        if not job:
            raise ValueError('A selected role does not exist in this workspace.')
        item = {'job_id': job_id, 'company': str(job.get('company', '')), 'title': str(job.get('title', '')),
                'url': _url(job.get('url')), 'resume_id': overrides.get(job_id) or resume['id'],
                'resume_override': bool(overrides.get(job_id)), 'answers': {}, 'status': 'pending',
                'message': 'Waiting for form inspection. No application is authorized yet.'}
        if _application_exists(store, job_id):
            item.update(status='skipped', message='An application is already recorded for this role.')
        elif _duplicate_batch(store, job_id):
            item.update(status='skipped', message='This role already belongs to another retained application batch.')
        elif job.get('availability') == 'closed':
            item.update(status='skipped', message='This role is recorded as closed. Reverify it before applying.')
        batch['items'].append(item)
    _event(batch, 'created')
    return _save(store, batch)


def _update(store, payload):
    batch = _batch(store, payload, True)
    if batch['status'] in ('cancelled', 'complete'):
        raise ValueError('Completed or cancelled batches cannot be edited.')
    run = batch.get('codex_run')
    if run and run.get('status') not in ('complete', 'cancelled'):
        changes = payload.get('items', [])
        if any(key in payload for key in ('applicant', 'answers', 'resume_id')) or any(
                isinstance(change, dict) and change.get('job_id') in run['job_ids'] for change in (changes if isinstance(changes, list) else [])):
            raise ValueError('This run reserves the reviewed materials. Discard an unstarted prepared request before editing; for a started run, update explicit answers through its original Codex chat.')
    if any(key in payload for key in ('applicant', 'answers', 'resume_id')) and any(item['status'] in BUSY for item in batch['items']):
        raise ValueError('Wait for the current browser step to finish before editing this batch.')
    global_change = False
    if 'name' in payload:
        batch['name'] = _text(payload['name'], 'Batch name', 160, True)
    if 'applicant' in payload:
        batch['applicant'] = {**batch['applicant'], **_applicant(payload['applicant'])}
        global_change = True
    if 'answers' in payload:
        batch['answers'] = {**batch['answers'], **_answers(payload['answers'])}
        global_change = True
    if 'resume_id' in payload:
        batch['resume_id'] = _resume_record(store, payload['resume_id'])['id']
        global_change = True
    changes = payload.get('items', [])
    if not isinstance(changes, list) or len(changes) > MAX_BATCH:
        raise ValueError('Role edits must be a bounded list.')
    edited = set()
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError('Role edits must be objects.')
        item = _item(batch, change.get('job_id'))
        if item['status'] in BUSY:
            raise ValueError('Wait for this role\'s current browser step to finish before editing it.')
        if item['status'] in TERMINAL or item['status'] == 'uncertain':
            raise ValueError('Submitted, skipped, or uncertain roles cannot be edited.')
        if 'answers' in change:
            item['answers'] = {**item.get('answers', {}), **_answers(change['answers'])}
        if 'resume_id' in change:
            item['resume_id'] = _resume_record(store, change['resume_id'] or batch['resume_id'])['id']
            item['resume_override'] = bool(change['resume_id'])
        edited.add(item['job_id'])
    for item in batch['items']:
        if item['status'] in TERMINAL or item['status'] == 'uncertain':
            continue
        if global_change or item['job_id'] in edited:
            if 'resume_id' in payload and not item.get('resume_override', False):
                item['resume_id'] = batch['resume_id']
            _clear_review(item)
    _event(batch, 'edited', message='Changed materials require a fresh form inspection and authorization.')
    return _save(store, batch)


def _authorize(store, root, payload, codex=False):
    batch = _batch(store, payload, True)
    if payload.get('confirmed') is not True:
        raise ValueError('Explicitly authorize the reviewed applications before submitting.')
    if batch['status'] in ('paused', 'cancelled', 'complete'):
        raise ValueError('Resume the batch before authorizing applications.')
    ids = payload.get('job_ids')
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_BATCH:
        raise ValueError('Choose reviewed roles to authorize.')
    for job_id in dict.fromkeys(ids):
        item = _item(batch, job_id)
        if item.get('codex_run_id') and not codex:
            raise ValueError('Authorize this reserved role through its Codex workflow.')
        if item['status'] != 'ready' or not item.get('form_signature'):
            raise ValueError('Every authorized role must have an inspected, ready form.')
        if _application_exists(store, job_id) or _duplicate_batch(store, job_id, batch['id']):
            raise ValueError('A selected role already has an application or another batch.')
        job = store.get('jobs', job_id)
        if not job or job.get('availability') == 'closed' or job.get('url') != item['url']:
            raise ValueError('A selected listing changed. Review its current application URL.')
        resume = _resume_record(store, item['resume_id'])
        _resume_bytes(root, resume)
        if (_codex_missing if codex else _missing)(item['fields'], item['planned_answers'], resume):
            raise ValueError('Answer all required fields before authorizing this role.')
        item['authorization'] = {'at': utcnow(), 'revision': batch['revision'],
                                 'applicant': copy.deepcopy(batch['applicant']),
                                 'answers': {**batch['answers'], **item['answers']},
                                 'resume_id': resume['id'], 'resume_sha256': resume['sha256'],
                                 'form_signature': item['form_signature'], 'fields': copy.deepcopy(item['fields']),
                                 'planned_answers': copy.deepcopy(item['planned_answers'])}
        if codex:
            item['authorization'].update(executor='codex', run_id=item['codex_run_id'])
        item.update(status='queued', message='Authorized for Codex. Waiting to fill the reviewed application.' if codex else 'Authorized. Waiting for the browser worker.')
        _event(batch, 'authorized', item)
    return _save(store, batch)


def _expired(item, now):
    try:
        return datetime.fromisoformat(item['lease_until']) <= now
    except (ValueError, KeyError):
        return True


def _recover(store, now):
    for batch in store.all(BATCH_KIND):
        changed = False
        for item in batch['items']:
            if item['status'] not in BUSY or not _expired(item, now):
                continue
            prior = item['status']
            if prior == 'submitting':
                item.update(status='uncertain', message='The browser stopped after submission began. Check the employer result before retrying.')
                # Keep the attempt ID so a delayed receipt can safely settle it.
            else:
                _clear_attempt(item)
                item.pop('authorization', None)
                item.update(status='pending' if prior == 'inspecting' else 'needs_input',
                            message='The browser step was interrupted. Inspect or review this role again before submitting.')
            _event(batch, 'worker_interrupted', item)
            changed = True
        if changed:
            _save(store, batch)


def _claim(root, store, payload, codex=False):
    worker = _id(payload.get('worker_id'), 'worker')
    now = datetime.now(timezone.utc)
    if not codex:
        record = _worker_record(store, worker) or {'id': worker, 'worker_id': worker}
        record['last_seen_at'] = now.isoformat()
        _save_worker(store, record, make_current=True)
        if _worker_request(record, now) and record['reload_request']['status'] in ('pending', 'reloading'):
            return {'ok': True, 'task': None, 'message': 'The browser worker is waiting to reload.',
                    'reload_request': record['reload_request']}
    _recover(store, now)
    selected = payload.get('batch_id')
    if selected:
        _id(selected, 'batch')
    batches = store.all(BATCH_KIND)
    # One outstanding task per companion prevents parallel submits on restart.
    if any(item.get('worker_id') == worker and item['status'] in BUSY for batch in batches for item in batch['items']):
        return {'ok': True, 'task': None, 'message': 'This worker already has a current browser step.'}
    for batch in batches:
        if selected and batch['id'] != selected or batch['status'] in ('paused', 'cancelled', 'complete'):
            continue
        for item in batch['items']:
            if codex and (item.get('codex_run_id') != payload.get('run_id') or payload.get('job_id') and item['job_id'] != payload['job_id']):
                continue
            if not codex and item.get('codex_run_id'):
                continue
            if item['status'] not in ('pending', 'queued'):
                continue
            if _application_exists(store, item['job_id']):
                item.update(status='skipped', message='An application was recorded before this browser step began.')
                item.pop('authorization', None)
                _save(store, batch)
                continue
            mode = 'submit' if item['status'] == 'queued' else 'inspect'
            job = store.get('jobs', item['job_id'])
            if not job or job.get('availability') == 'closed' or job.get('url') != item['url']:
                item.update(status='needs_input', message='The listing or application URL changed. Check the current employer posting.')
                item.pop('authorization', None)
                _save(store, batch)
                continue
            material = item.get('authorization') if mode == 'submit' else {
                'applicant': batch['applicant'], 'answers': {**batch['answers'], **item['answers']},
                'resume_id': item['resume_id'], 'fields': [], 'form_signature': '', 'planned_answers': {}}
            if not material:
                raise ValueError('The application authorization is missing. Review the role again.')
            resume = _resume_record(store, material['resume_id'])
            file_result = dict(resume)
            if mode == 'submit':
                if material['resume_sha256'] != resume['sha256']:
                    raise ValueError('The authorized resume changed. Review it again.')
                if not codex:
                    file_result['content_base64'] = base64.b64encode(_resume_bytes(root, resume)).decode('ascii')
            item.update(attempt_id='attempt-' + secrets.token_hex(20), worker_id=worker, attempt_mode=mode,
                        lease_until=(now + timedelta(seconds=LEASE_SECONDS)).isoformat(),
                        status='running' if mode == 'submit' else 'inspecting',
                        message=('Codex is filling the reviewed application.' if mode == 'submit' else 'Codex is inspecting the form.') if codex else
                                ('Browser worker is preparing the reviewed application.' if mode == 'submit' else 'Browser worker is inspecting the form.'))
            _event(batch, 'claimed_' + mode, item)
            result = _save(store, batch)
            result['task'] = {'batch_id': batch['id'], 'job_id': item['job_id'], 'attempt_id': item['attempt_id'],
                              'mode': mode, 'url': item['url'], 'company': item['company'], 'title': item['title'],
                              'applicant': material['applicant'], 'answers': material['answers'], 'resume': file_result,
                              'fields': material.get('fields', []), 'form_signature': material.get('form_signature', ''),
                              'planned_answers': material.get('planned_answers', {}), 'revision': batch['revision'],
                              'lease_until': item['lease_until']}
            return result
    return {'ok': True, 'task': None}


def _evidence(value, item, manual=False):
    if not isinstance(value, dict):
        raise ValueError('Record the employer confirmation URL and visible confirmation text.')
    url = _url(value.get('url'))
    if not manual and urlsplit(url).hostname != urlsplit(item['url']).hostname:
        raise ValueError('Confirmation evidence must come from the reviewed application host.')
    return {'url': url, 'text': _text(value.get('text'), 'Confirmation evidence', 4000, True), 'observed_at': utcnow()}


def _record_submission(store, batch, item, evidence, manual=False):
    item.update(status='submitted', evidence=evidence, message='Submission confirmed by user.' if manual else 'Employer submission confirmation observed.', submitted_at=utcnow())
    materials = item.get('submission_materials') or item.get('authorization')
    if materials:
        item['submitted_materials'] = copy.deepcopy(materials)
    item.pop('submission_materials', None)
    _clear_attempt(item)
    item.pop('authorization', None)
    previous = store.get('applications', item['job_id']) or {'id': item['job_id'], 'job_id': item['job_id']}
    application = dict(previous)
    if application.get('stage') in (None, 'saved'):
        application['stage'] = 'applied'
        if not application.get('applied_at'):
            application['applied_at'] = item['submitted_at']
    application['updated_at'] = utcnow()
    receipts = list(application.get('browser_submissions', []))
    receipts.append({'batch_id': batch['id'], 'confirmed_at': item['submitted_at'], 'evidence': evidence, 'manual': manual})
    if item.get('codex_run_id'):
        receipts[-1].update(executor='codex' if not manual else 'manual', run_id=item['codex_run_id'])
    application['browser_submissions'] = receipts
    application = validate_record('applications', application, store)
    _event(batch, 'submission_confirmed', item)
    result = _save(store, batch, {'applications': [application]})
    result['application_updated'] = True
    return result


def _report(root, store, payload, codex=False):
    batch = _batch(store, payload)
    item = _item(batch, payload.get('job_id'))
    if item.get('codex_run_id') and not codex:
        raise ValueError('Report this reserved role through its Codex workflow.')
    attempt = payload.get('attempt_id')
    if not isinstance(attempt, str) or not secrets.compare_digest(item.get('attempt_id', ''), attempt):
        raise ValueError('This browser attempt is no longer current. Refresh its state.')
    status = payload.get('status')
    message = _text(payload.get('message', ''), 'Browser message', 2000)
    if item['status'] not in BUSY and not (item['status'] == 'uncertain' and status == 'submitted'):
        raise ValueError('This browser attempt has already finished.')
    if status == 'heartbeat':
        item['lease_until'] = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()
        return _save(store, batch)
    if status == 'submitting':
        if item['status'] != 'running' or batch['status'] in ('paused', 'cancelled'):
            raise ValueError('This application is no longer authorized to submit.')
        if _expired(item, datetime.now(timezone.utc)):
            raise ValueError('This browser attempt expired. Inspect and authorize the role again.')
        if _application_exists(store, item['job_id']):
            raise ValueError('An application is already recorded for this role.')
        job = store.get('jobs', item['job_id'])
        if not job or job.get('availability') == 'closed' or job.get('url') != item['url']:
            raise ValueError('The listing or application URL changed. Check it before submitting.')
        authorization = item.get('authorization', {})
        if payload.get('form_signature') != authorization.get('form_signature'):
            raise ValueError('The employer form changed. Inspect and review it again.')
        item['submission_materials'] = copy.deepcopy(authorization)
        item.update(status='submitting', message='Submission started; waiting for employer confirmation.')
        _event(batch, 'submission_started', item)
        return _save(store, batch)
    if status == 'submitted':
        if item['status'] not in ('submitting', 'uncertain'):
            raise ValueError('Submission must be recorded as started before accepting its confirmation.')
        return _record_submission(store, batch, item, _evidence(payload.get('evidence'), item))
    if status == 'inspected':
        if item['attempt_mode'] != 'inspect' or item['status'] != 'inspecting':
            raise ValueError('A submission attempt cannot replace the reviewed form.')
        fields = _fields(payload.get('fields'))
        planned = _answers(payload.get('planned_answers', {}))
        if set(planned) - {field['key'] for field in fields}:
            raise ValueError('Planned answers must reference inspected question keys.')
        signature = _text(payload.get('form_signature'), 'Form signature', 200, True)
        missing = (_codex_missing if codex else _missing)(fields, planned, _resume_record(store, item['resume_id']))
        if codex:
            item.setdefault('codex_review_history', []).append({'at': utcnow(), 'run_id': item['codex_run_id'],
                'observed_url': payload['observed_url'], 'fields': copy.deepcopy(fields),
                'planned_answers': copy.deepcopy(planned), 'form_signature': signature})
            item['form_executor'] = 'codex'
        item.update(fields=fields, form_signature=signature, planned_answers=planned, missing_fields=missing,
                    status='needs_input' if missing else 'ready', inspected_at=utcnow(),
                    message=message or ('Answer the required questions and inspect again.' if missing else 'Review the form, answers, and resume before authorizing submission.'))
        _clear_attempt(item)
    elif status in ('needs_input', 'failed', 'uncertain'):
        if item['status'] == 'submitting':
            status = 'uncertain'
        item.update(status=status, message=message or 'This application needs your attention.')
        item.pop('authorization', None)
        if status != 'uncertain':
            _clear_attempt(item)
    else:
        raise ValueError('Choose a valid browser result status.')
    _event(batch, ('codex_' if codex else 'browser_') + status, item, item['message'])
    return _save(store, batch)


def _control(store, payload):
    batch = _batch(store, payload, True)
    op = payload['op']
    if op == 'pause':
        if batch['status'] in ('cancelled', 'complete'):
            raise ValueError('This batch has already finished.')
        batch['status'] = 'paused'
    elif op == 'resume':
        if batch['status'] != 'paused':
            raise ValueError('Only a paused batch can be resumed.')
        batch['status'] = 'active'
    elif op == 'cancel':
        if batch['status'] == 'complete':
            raise ValueError('A completed batch cannot be cancelled.')
        batch['status'] = 'cancelled'
        for item in batch['items']:
            if item['status'] in TERMINAL or item['status'] == 'uncertain':
                continue
            if item['status'] == 'submitting':
                item.update(status='uncertain', message='Batch cancelled while a submission was in flight. Check its outcome.')
            else:
                item.update(status='skipped', message='Cancelled before submission.')
                _clear_attempt(item)
            item.pop('authorization', None)
    _event(batch, op)
    return _save(store, batch)


def _reconcile(store, payload):
    batch = _batch(store, payload, True)
    item = _item(batch, payload.get('job_id'))
    manual_completion = item['status'] in ('needs_input', 'failed') and payload.get('outcome') == 'submitted'
    if (item['status'] != 'uncertain' and not manual_completion) or payload.get('confirmed') is not True:
        raise ValueError('Explicitly confirm a checked uncertain outcome or a manual submission for a role needing attention.')
    # The user may have completed a legitimate employer-to-ATS redirect that
    # the automatic worker deliberately refused to follow. Preserve that
    # explicit observation with manual provenance; automatic receipts remain
    # bound to the original application host.
    evidence = _evidence(payload.get('evidence'), item, manual=True)
    if payload.get('outcome') == 'submitted':
        return _record_submission(store, batch, item, evidence, manual=True)
    if payload.get('outcome') != 'not_submitted':
        raise ValueError('Choose submitted or not_submitted after checking the employer result.')
    item['reconciliation'] = evidence
    item.setdefault('reconciliation_history', []).append({'outcome': 'not_submitted', **evidence,
                                                         'materials': item.pop('submission_materials', None)})
    _clear_review(item)
    if batch['status'] == 'cancelled':
        item.update(status='skipped', message='User confirmed no submission; cancelled batch retained.')
    _event(batch, 'confirmed_not_submitted', item)
    return _save(store, batch)


def _codex_prepare(root, store, payload):
    batch = _batch(store, payload)
    existing = batch.get('codex_run')
    ids = payload.get('job_ids')
    if ids is None:
        eligible = [item['job_id'] for item in batch['items'] if item['status'] in ('pending', 'ready', 'needs_input', 'failed', 'queued')]
        ids = existing['job_ids'] if existing and existing.get('status') not in ('complete', 'cancelled') else eligible or (existing['job_ids'] if existing else [])
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_BATCH:
        raise ValueError('Choose eligible roles for the Codex workflow.')
    ids = list(dict.fromkeys(_id(value, 'job') for value in ids))
    if existing and set(ids) == set(existing['job_ids']):
        return {'ok': True, 'batch': _public_batch(batch), 'run': _public_codex_run(existing)}
    batch = _batch(store, payload, True)
    if batch['status'] in ('paused', 'cancelled', 'complete'):
        raise ValueError('Resume an active batch before preparing a Codex workflow.')
    if existing and existing.get('status') not in ('complete', 'cancelled'):
        raise ValueError('This batch already has a reserved Codex run. Continue its original chat.')
    materials = {}
    for job_id in ids:
        item = _item(batch, job_id)
        if item['status'] not in ('pending', 'ready', 'needs_input', 'failed', 'queued'):
            raise ValueError('Busy, uncertain, or completed roles cannot be reserved for a new run.')
        if _application_exists(store, job_id) or _duplicate_batch(store, job_id, batch['id']):
            raise ValueError('A selected role already has an application or another batch.')
        job = store.get('jobs', job_id)
        if not job or job.get('availability') == 'closed' or job.get('url') != item['url']:
            raise ValueError('A selected listing changed. Reverify its employer page before continuing.')
        resume = _resume_record(store, item['resume_id'])
        _resume_bytes(root, resume)
        materials[job_id] = {'url': item['url'], 'resume_id': resume['id'], 'resume_sha256': resume['sha256'],
            'resume': copy.deepcopy(resume),
            'applicant': copy.deepcopy(batch['applicant']), 'answers': {**batch['answers'], **item['answers']}}
    run = {'id': 'codex-' + secrets.token_hex(12), 'status': 'awaiting_start', 'created_at': utcnow(),
           'job_ids': ids, 'materials': materials}
    if existing:
        batch.setdefault('codex_history', []).append(copy.deepcopy(existing))
    batch['codex_run'] = run
    for job_id in ids:
        item = _item(batch, job_id)
        _clear_review(item)
        item.update(executor='codex', codex_run_id=run['id'], message='Reserved for Codex. Open the workflow and send its prompt to begin. No submission is authorized yet.')
    _event(batch, 'codex_prepared', message='Reserved selected roles and materials; no submission authorized.')
    result = _save(store, batch)
    result['run'] = _public_codex_run(run)
    return result


def _codex_discard(store, payload):
    batch = _batch(store, payload, True)
    run = batch.get('codex_run')
    if not run or run.get('worker_id') or run.get('status') != 'awaiting_start':
        raise ValueError('Only an unstarted prepared Codex request can be discarded.')
    for job_id in run['job_ids']:
        if _item(batch, job_id)['status'] in BUSY | TERMINAL | {'uncertain'}:
            raise ValueError('Resolve the current application outcome before changing this reservation.')
    run.update(status='discarded', discarded_at=utcnow())
    batch.setdefault('codex_history', []).append(copy.deepcopy(run))
    batch.pop('codex_run')
    for job_id in run['job_ids']:
        item = _item(batch, job_id)
        _clear_review(item)
        item.pop('codex_run_id', None)
        item['message'] = 'Prepared request discarded. Review materials before opening a new Codex workflow.'
    _event(batch, 'codex_discarded', message='Unstarted reservation released; supplied answers and history retained.')
    return _save(store, batch)


def _codex_run(store, payload, bind=False):
    run_id = _id(payload.get('run_id'), 'Codex run')
    worker = _id(payload.get('worker_id'), 'Codex worker')
    batch = next((value for value in store.all(BATCH_KIND) if value.get('codex_run', {}).get('id') == run_id), None)
    if not batch or payload.get('batch_id', batch['id']) != batch['id']:
        raise ValueError('This Codex run does not belong to the selected batch or workspace.')
    run = batch['codex_run']
    if run.get('worker_id') not in (None, worker):
        raise ValueError('This run belongs to another Codex worker. Resume the original chat; do not start a duplicate application.')
    if not run.get('worker_id'):
        if not bind:
            raise ValueError('Read the Codex context and claim ownership before changing this run.')
        run.update(worker_id=worker, started_at=utcnow())
        _event(batch, 'codex_started')
        _save(store, batch)
    return batch, run


def _codex_upload_path(root, run_id, resume, content):
    """Stage a named upload through private, non-symlink directory descriptors."""
    filename = resume['filename']
    if filename != Path(filename).name or '/' in filename or '\\' in filename:
        raise ValueError('The saved resume filename is invalid.')
    parts = ('codex_uploads', _id(run_id, 'run'), _id(resume['id'], 'resume'))
    descriptor = None
    try:
        descriptor = os.open(str(Path(root).resolve() / 'inputs'), os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
        for part in parts:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        try:
            target = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600, dir_fd=descriptor)
        except FileExistsError:
            target = os.open(filename, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=descriptor)
            with os.fdopen(target, 'rb') as stream:
                if stream.read(MAX_RESUME_BYTES + 1) != content:
                    raise ValueError('The named upload copy changed. Restore it before continuing.')
        else:
            with os.fdopen(target, 'wb') as stream:
                stream.write(content)
    except OSError:
        raise ValueError('The private named upload copy is unavailable or uses a symbolic link.') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return str(Path(root).resolve().joinpath('inputs', *parts, filename))


def _codex_materials(root, store, batch, run, item):
    saved = run['materials'].get(item['job_id'])
    if not saved or item.get('codex_run_id') != run['id']:
        raise ValueError('This role is outside the reserved Codex run.')
    if saved['resume_id'] != item['resume_id'] or saved['applicant'] != batch['applicant'] or saved['url'] != item['url'] or saved['answers'] != {**batch['answers'], **item['answers']}:
        raise ValueError('The reserved application materials changed. Stop and review the run.')
    resume = _resume_record(store, saved['resume_id'])
    if resume['sha256'] != saved['resume_sha256']:
        raise ValueError('The reserved resume changed. Stop and review the run.')
    content = _resume_bytes(root, resume)
    return resume, _codex_upload_path(root, run['id'], resume, content)


def _codex_task(root, store, batch, run, item):
    if item['status'] in TERMINAL | {'submitting', 'uncertain'}:
        resume, path = copy.deepcopy(run['materials'][item['job_id']]['resume']), None
    else:
        resume, path = _codex_materials(root, store, batch, run, item)
    material = item.get('authorization') or run['materials'][item['job_id']]
    return {'batch_id': batch['id'], 'run_id': run['id'], 'job_id': item['job_id'], 'status': item['status'],
        'mode': item.get('attempt_mode'), 'attempt_id': item.get('attempt_id'), 'lease_until': item.get('lease_until'),
        'url': item['url'], 'company': item['company'], 'title': item['title'], 'resume': resume, 'resume_path': path,
        'applicant': copy.deepcopy(material['applicant']), 'answers': copy.deepcopy(material['answers']),
        'fields': copy.deepcopy(item.get('fields', [])), 'planned_answers': copy.deepcopy(item.get('planned_answers', {})),
        'form_signature': item.get('form_signature', ''), 'missing_fields': copy.deepcopy(item.get('missing_fields', [])),
        'click_granted': bool(item.get('codex_click_granted_at'))}


def _codex_observation_url(value, expected):
    current, original = urlsplit(_url(value)), urlsplit(expected)
    def base(path):
        return re.sub(r'/(?:apply|application)/?$', '', path).rstrip('/')
    if current.netloc != original.netloc or base(current.path) != base(original.path) or parse_qs(
            current.query, keep_blank_values=True) != parse_qs(original.query, keep_blank_values=True):
        raise ValueError('The observed form is for a different application. Verify the selected employer role.')
    return value


def _codex_check(root, store, batch, run, payload):
    item = _item(batch, payload.get('job_id'))
    _codex_materials(root, store, batch, run, item)
    if item.get('attempt_id') != payload.get('attempt_id') or item.get('worker_id') != payload['worker_id']:
        raise ValueError('This Codex application attempt is no longer current.')
    if _expired(item, datetime.now(timezone.utc)):
        _recover(store, datetime.now(timezone.utc))
        raise ValueError('This application attempt expired. Check its retained state and actual employer outcome.')
    if batch['status'] in ('paused', 'cancelled') or item['status'] not in ('running', 'submitting'):
        raise ValueError('This application is paused, expired, or not authorized to submit.')
    authorization = item.get('authorization', {})
    if authorization.get('executor') != 'codex' or authorization.get('run_id') != run['id']:
        raise ValueError('This application has no reviewed Codex authorization.')
    job = store.get('jobs', item['job_id'])
    if _application_exists(store, item['job_id']) or not job or job.get('availability') == 'closed' or job.get('url') != item['url']:
        raise ValueError('The listing or application state changed. Check the employer result before continuing.')
    if payload.get('phase') == 'before_click':
        if item['status'] != 'submitting':
            raise ValueError('Persist the submitting checkpoint before requesting the one-time click grant.')
        if item.get('codex_click_granted_at'):
            return {'ok': True, 'can_click': False, 'message': 'The click grant was already issued. Check the actual outcome; never click again automatically.'}
        item['codex_click_granted_at'] = utcnow()
        _event(batch, 'codex_click_granted', item)
        result = _save(store, batch)
        result['can_click'] = True
        return result
    return {'ok': True, 'can_fill': item['status'] == 'running', 'can_submit': item['status'] == 'running',
            'can_click': False, 'status': item['status'], 'form_signature': authorization['form_signature']}


def _codex_action(root, store, payload):
    op = payload['op']
    if op == 'codex_prepare':
        return _codex_prepare(root, store, payload)
    if op == 'codex_discard':
        return _codex_discard(store, payload)
    batch, run = _codex_run(store, payload, bind=op == 'codex_context')
    if op == 'codex_context':
        _recover(store, datetime.now(timezone.utc))
        batch = store.get(BATCH_KIND, batch['id'])
        run = batch['codex_run']
        return {'ok': True, 'run': _public_codex_run(run), 'batch': _public_batch(batch),
                'tasks': [_codex_task(root, store, batch, run, _item(batch, job_id)) for job_id in run['job_ids']]}
    if op == 'codex_claim':
        if payload.get('job_id') and payload['job_id'] not in run['job_ids']:
            raise ValueError('Choose a role reserved in this Codex run.')
        _recover(store, datetime.now(timezone.utc))
        batch = store.get(BATCH_KIND, batch['id'])
        run = batch['codex_run']
        for job_id in run['job_ids']:
            item = _item(batch, job_id)
            if item['status'] in ('pending', 'queued') and (not payload.get('job_id') or payload['job_id'] == job_id):
                _codex_materials(root, store, batch, run, item)
        result = _claim(root, store, {**payload, 'batch_id': batch['id']}, codex=True)
        batch = store.get(BATCH_KIND, batch['id'])
        if result.get('task'):
            result['task'] = _codex_task(root, store, batch, batch['codex_run'], _item(batch, result['task']['job_id']))
        result['run'] = _public_codex_run(batch['codex_run'])
        return result
    if op == 'codex_authorize':
        ids = payload.get('job_ids')
        if not isinstance(ids, list) or not ids or any(job_id not in run['job_ids'] for job_id in ids):
            raise ValueError('Authorize inspected roles from this Codex run.')
        for job_id in ids:
            item = _item(batch, job_id)
            _codex_materials(root, store, batch, run, item)
            if item.get('form_executor') != 'codex':
                raise ValueError('Inspect this form in Codex before authorizing it.')
        return _authorize(store, root, {**payload, 'batch_id': batch['id']}, codex=True)
    if op == 'codex_check':
        return _codex_check(root, store, batch, run, payload)
    item = _item(batch, payload.get('job_id'))
    if item.get('codex_run_id') != run['id']:
        raise ValueError('This role is outside the reserved Codex run.')
    if op == 'codex_answers':
        _codex_materials(root, store, batch, run, item)
        if batch['status'] in ('paused', 'cancelled', 'complete') or item['status'] in BUSY | TERMINAL | {'uncertain'}:
            raise ValueError('Resolve the current application step before changing its explicit answers.')
        item['answers'] = {**item['answers'], **_answers(payload.get('answers', {}))}
        run['materials'][item['job_id']]['answers'] = {**batch['answers'], **item['answers']}
        _clear_review(item)
        item['message'] = 'Explicit answers saved. Codex must inspect and review the form again.'
        _event(batch, 'codex_answers_updated', item)
        return _save(store, batch)
    if op != 'codex_report':
        raise ValueError('Choose a valid Codex workflow action.')
    if item.get('worker_id') != payload['worker_id']:
        raise ValueError('This Codex worker does not own the application attempt.')
    request = {**payload, 'batch_id': batch['id']}
    if payload.get('status') == 'inspected':
        _codex_materials(root, store, batch, run, item)
        fields = _fields(payload.get('fields'))
        allowed = {'text', 'email', 'tel', 'url', 'number', 'date', 'textarea', 'select', 'radio', 'checkbox', 'file', 'multiselect'}
        if any(field['supported'] and (field['type'] not in allowed or field['type'] in ('select', 'radio', 'multiselect') and not field['options']) for field in fields):
            raise ValueError('Supported Codex controls require a known type and observed choice options.')
        planned = _answers(payload.get('planned_answers', {}))
        request.update(fields=fields, planned_answers=planned,
            observed_url=_codex_observation_url(payload.get('observed_url'), item['url']),
            form_signature='codex-' + hashlib.sha256(json.dumps({'fields': fields, 'planned_answers': planned}, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
    if payload.get('status') == 'submitting':
        _codex_check(root, store, batch, run, payload)
    if payload.get('status') == 'heartbeat' and _expired(item, datetime.now(timezone.utc)):
        _recover(store, datetime.now(timezone.utc))
        raise ValueError('This attempt expired and cannot be revived. Read the run context before continuing.')
    return _report(root, store, request, codex=True)


def perform_batch_action(root, store, config, profile, payload):
    if not isinstance(payload, dict):
        raise ValueError('Batch requests must be JSON objects.')
    if 'workspace_id' in payload and payload['workspace_id'] != workspace_id(root):
        raise ValueError('This request belongs to a different Jobflow workspace.')
    op = payload.get('op')
    if isinstance(op, str) and op.startswith('codex_'):
        if op not in ('codex_prepare', 'codex_discard') and payload.get('workspace_id') != workspace_id(root):
            raise ValueError('Codex must identify the selected workspace.')
        return _codex_action(root, store, payload)
    if op in ('worker_heartbeat', 'worker_reload', 'worker_reload_ack'):
        if payload.get('workspace_id') != workspace_id(root):
            raise ValueError('The browser worker must identify this workspace. Connect it again.')
        return {'worker_heartbeat': _worker_heartbeat, 'worker_reload': _worker_reload,
                'worker_reload_ack': _worker_reload_ack}[op](store, payload)
    if op == 'upload_resume':
        return _upload(root, store, payload)
    if isinstance(op, str) and op.startswith('pdf_review_'):
        from .resume_reviews import perform_review_action
        return perform_review_action(root, store, config, profile, payload)
    if isinstance(op, str) and op.startswith('resume_'):
        from .resumes import perform_resume_action
        return perform_resume_action(root, store, config, profile, payload)
    if op == 'create':
        from .resumes import resume_library
        defaults = resume_library(root)
        payload = {**payload, 'resume_id': payload.get('resume_id') or defaults['default_resume_id'],
                   'applicant': {**defaults['applicant'], **_applicant(payload.get('applicant', {}))}}
        return _create(store, payload)
    if op == 'update':
        return _update(store, payload)
    if op == 'authorize':
        return _authorize(store, root, payload)
    if op == 'claim':
        return {'ok': True, 'task': None, 'deprecated': True, 'message': 'The browser extension is retired. Open this application batch in Codex.'}
    if op == 'report':
        return _report(root, store, payload)
    if op in ('pause', 'resume', 'cancel'):
        return _control(store, payload)
    if op == 'reconcile':
        return _reconcile(store, payload)
    raise ValueError('Choose a valid application batch action.')

"""Run-scoped desktop access to hosted batches, through the locked Jobflow CLI.

Only the cloud transport may issue credentials after session + CSRF validation.
The browser action endpoint never accepts these internal codex_hosted_* ops.
No database/service credential or server filesystem path crosses this boundary.
"""
import base64
import copy
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from . import application_batches as batches

ACCESS_SECONDS = 12 * 60 * 60
RUNNER_OPS = frozenset(('codex_context', 'codex_claim', 'codex_report', 'codex_answers',
                        'codex_authorize', 'codex_check', 'codex_bind_destination'))
_TOKEN = re.compile(r'^([0-9a-f-]{36})\.([A-Za-z0-9][A-Za-z0-9_-]{0,127})\.([A-Za-z0-9_-]{43})$')
ACCESS_ERROR = 'Codex access is invalid, expired, or revoked. Renew access in the hosted batch and continue the original chat.'


class AccessError(ValueError):
    pass


def token_route(token):
    match = _TOKEN.fullmatch(token) if isinstance(token, str) else None
    if not match:
        raise AccessError(ACCESS_ERROR)
    return match.group(1), match.group(2)


def authenticate(root, store, token, account_id):
    routed_account, batch_id = token_route(token)
    batch = store.get(batches.BATCH_KIND, batch_id)
    run = (batch or {}).get('codex_run') or {}
    access = run.get('hosted_access') or {}
    try:
        expiry = datetime.fromisoformat(access['expires_at'])
        valid_time = expiry.tzinfo is not None and expiry > datetime.now(timezone.utc)
    except (KeyError, TypeError, ValueError):
        valid_time = False
    if (routed_account != account_id or access.get('account_id') != account_id or
            access.get('workspace_id') != batches.workspace_id(root) or not valid_time or
            access.get('revoked_at') or not secrets.compare_digest(
                access.get('token_hash', ''), hashlib.sha256(token.encode('ascii')).hexdigest())):
        raise AccessError(ACCESS_ERROR)
    return batch, run


def _issue(root, store, payload):
    # Validate revision even when the underlying prepare operation is idempotent.
    batch = batches._batch(store, payload, True)
    op = payload['op']
    if op == 'codex_hosted_prepare':
        if batch['status'] in ('paused', 'cancelled', 'complete'):
            raise ValueError('Resume an active batch before preparing a Codex request.')
        result = batches._codex_prepare(root, store, {**payload, 'op': 'codex_prepare'})
        batch = store.get(batches.BATCH_KIND, batch['id'])
        if batch['codex_run'].get('worker_id'):
            return result
    run = batch.get('codex_run')
    if not run or run.get('status') in ('complete', 'cancelled', 'discarded'):
        raise ValueError('Choose an unfinished Codex run.')
    if op == 'codex_hosted_revoke':
        if not run.get('hosted_access'):
            raise ValueError('This run has no hosted Codex access.')
        now = datetime.now(timezone.utc).isoformat()
        run['hosted_access']['revoked_at'] = now
        run['handoff_revoked_at'] = now
        batches._event(batch, 'codex_access_revoked', message='Desktop access revoked; application history retained.')
        return batches._save(store, batch)
    account_id = payload.get('account_id')
    if not isinstance(account_id, str) or not re.fullmatch(r'[0-9a-f-]{36}', account_id):
        raise ValueError('Hosted access requires a verified account.')
    token = '%s.%s.%s' % (account_id, batch['id'], secrets.token_urlsafe(32))
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=ACCESS_SECONDS)).isoformat()
    workspace = batches.workspace_id(root)
    run['hosted_access'] = {'account_id': account_id, 'workspace_id': workspace,
        'token_hash': hashlib.sha256(token.encode('ascii')).hexdigest(), 'expires_at': expiry}
    run['handoff_expires_at'] = expiry
    run.pop('handoff_revoked_at', None)
    batches._event(batch, 'codex_access_issued', message='Expiring desktop access issued for the reserved run.')
    result = batches._save(store, batch)
    result.update(run=batches._public_codex_run(run), handoff_expires_at=expiry,
        connection={'version': 1, 'token': token, 'workspace_id': workspace,
                    'batch_id': batch['id'], 'run_id': run['id']})
    return result


def _strip_private(value):
    if isinstance(value, dict):
        return {k: _strip_private(v) for k, v in value.items()
                if k not in ('resume_path', 'path', 'hosted_access', 'worker_id')}
    if isinstance(value, list):
        return [_strip_private(v) for v in value]
    return value


def _scoped_result(root, result, batch, run):
    result = copy.deepcopy(result)
    tasks = result.get('tasks', []) + ([result['task']] if result.get('task') else [])
    for task in tasks:
        if task.get('job_id') not in run['job_ids']:
            raise ValueError('The runner response contains a role outside its reserved scope.')
        resume = task.get('resume', {})
        if task.get('resume_path') and batch['status'] not in ('paused', 'cancelled', 'complete'):
            task['resume_download'] = {key: resume[key] for key in ('id', 'filename', 'sha256', 'size')}
            task['resume_download']['url'] = '/api/codex/resumes/' + resume['id']
        elif task.get('resume_path'):
            task['resume_unavailable_reason'] = 'batch_paused' if batch['status'] == 'paused' else 'batch_finished'
    if 'batch' in result:
        public = result['batch']
        result['batch'] = {key: public[key] for key in
            ('id', 'name', 'status', 'revision', 'created_at', 'updated_at', 'codex_run') if key in public}
        result['batch']['items'] = [item for item in public.get('items', []) if item['job_id'] in run['job_ids']]
    result.update(workspace_id=batches.workspace_id(root), batch_id=batch['id'], run_id=run['id'])
    return _strip_private(result)


def perform_hosted_action(root, store, payload):
    op = payload['op']
    if op in ('codex_hosted_prepare', 'codex_hosted_renew', 'codex_hosted_revoke'):
        return _issue(root, store, payload)
    if op not in ('codex_hosted_execute', 'codex_hosted_resume'):
        raise ValueError('Unknown hosted Codex operation.')
    # Revalidate under the CLI workspace lock, closing races with revoke/renew.
    batch, run = authenticate(root, store, payload.get('token'), payload.get('account_id'))
    if op == 'codex_hosted_resume':
        if not run.get('worker_id') or batch['status'] in ('paused', 'cancelled', 'complete'):
            raise ValueError('Resume access is unavailable until the active run connects.')
        resume_id = batches._id(payload.get('resume_id'), 'resume')
        for job_id in run['job_ids']:
            item = batches._item(batch, job_id)
            if (run['materials'][job_id]['resume_id'] == resume_id and
                    item['status'] not in batches.TERMINAL | {'submitting', 'uncertain'}):
                resume, unused = batches._codex_materials(root, store, batch, run, item)
                return {'ok': True, 'content_base64': base64.b64encode(batches._resume_bytes(root, resume)).decode('ascii')}
        raise ValueError('This resume is not available to the selected Codex run.')
    request = payload.get('request')
    if not isinstance(request, dict) or request.get('op') not in RUNNER_OPS:
        raise ValueError('Choose a supported Codex runner operation.')
    scope = {'workspace_id': batches.workspace_id(root), 'batch_id': batch['id'], 'run_id': run['id']}
    if any(request.get(key) != value for key, value in scope.items()):
        raise ValueError('This request belongs to a different Codex run or workspace.')
    # Pausing/cancelling stops new work but leaves receipt reporting possible for
    # an in-flight click. Explicit revocation stops every request immediately.
    if batch['status'] in ('paused', 'cancelled', 'complete') and request['op'] not in ('codex_context', 'codex_report'):
        raise ValueError('The batch is paused or finished; no new application work is allowed.')
    result = batches._codex_action(root, store, request)
    batch = store.get(batches.BATCH_KIND, batch['id'])
    return _scoped_result(root, result, batch, batch['codex_run'])

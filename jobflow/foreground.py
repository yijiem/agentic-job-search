"""Small user commands committed independently from background workflows.

The CLI owns this allowlist. Submission/runner operations and paid research keep
their existing reservation and lease boundaries.
"""
from datetime import date, datetime, timezone

from .storage import utcnow
from .tracking import STAGES, STATUSES, explicit_timestamp, integer_minutes, validate_record


BATCH_ACTIONS = frozenset((
    'create', 'update', 'upload_resume', 'resume_profile', 'resume_default',
    'resume_draft', 'resume_save_version', 'pdf_review_create', 'pdf_review_update',
    'pdf_review_save',
))


def supported(payload):
    if not isinstance(payload, dict):
        return False
    action, op = payload.get('action'), payload.get('op')
    if not isinstance(action, str):
        return False
    if action in ('application', 'task', 'interview'):
        return True
    return action == 'batch' and isinstance(op, str) and (
        op in BATCH_ACTIONS or
        (op == 'pdf_review_suggest' and payload.get('mode', 'local') == 'local'))


def perform(root, store, config, profile, payload):
    # Reuse the HTTP adapter's bounded text/identifier validation. These helpers
    # are pure; all persistence remains within this CLI-owned unit of work.
    from .web import _identifier, _text, MAX_BATCH
    if not supported(payload):
        raise ValueError('This operation requires the normal workflow command.')
    action = payload['action']
    if action == 'batch':
        from .application_batches import perform_batch_action
        return perform_batch_action(root, store, config, profile, payload)
    if action == 'application':
        ids = payload.get('job_ids', [payload.get('job_id')])
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_BATCH:
            raise ValueError('Choose between 1 and %s roles.' % MAX_BATCH)
        ids = list(dict.fromkeys(_identifier(value) for value in ids))
        stage = payload.get('stage')
        if stage not in STAGES:
            raise ValueError('Choose a valid application stage.')
        fields = {key: _text(payload, key) for key in ('notes', 'next_action', 'response') if key in payload}
        if 'follow_up_date' in payload:
            fields['follow_up_date'] = _text(payload, 'follow_up_date', 10)
            if fields['follow_up_date']:
                date.fromisoformat(fields['follow_up_date'])
        if 'applied_at' in payload:
            fields['applied_at'] = explicit_timestamp(_text(payload, 'applied_at', 50), 'applied_at')
        rows, skipped = [], 0
        now = utcnow()
        for identifier in ids:
            if not store.get('jobs', identifier):
                raise ValueError('A selected role does not exist in this workspace.')
            previous = store.get('applications', identifier) or {}
            if ((stage == 'saved' and previous.get('stage') not in (None, 'saved')) or
                    (len(ids) > 1 and stage == 'applied' and previous.get('stage') not in (None, 'saved', 'applied'))):
                skipped += 1
                continue
            row = {'id': identifier, 'job_id': identifier, 'stage': stage, 'updated_at': now, **fields}
            if stage == 'applied' and previous.get('stage') in (None, 'saved') and not previous.get('applied_at') and 'applied_at' not in fields:
                row['applied_at'] = now
            rows.append(validate_record('applications', row, store))
        store.put_many('applications', rows)
        if rows:
            store.request_refresh()
        return {'ok': True, 'updated': len(rows), 'skipped': skipped, 'applications': rows, 'message': 'Saved.'}
    if action == 'task':
        identifier = _identifier(payload.get('id'))
        row = store.get('tasks', identifier) or {'id': identifier, 'status': 'todo', 'generated': False,
                                               'category': 'custom', 'company': '', 'source_url': ''}
        if 'status' in payload:
            if payload['status'] not in STATUSES:
                raise ValueError('Choose a valid task status.')
            if payload['status'] == 'done' and row.get('status') != 'done' and not row.get('completed_at'):
                row['completed_at'] = utcnow()
            row['status'] = payload['status']
        for key in ('due_date', 'title', 'notes', 'category', 'source_url'):
            if key in payload:
                row[key] = _text(payload, key)
        if 'date' in payload:
            row['due_date'] = _text(payload, 'date')
        for key, minimum in (('minutes', 1), ('actual_minutes', 0)):
            if key in payload:
                row[key] = integer_minutes(payload[key], key, minimum, 1440)
        row = validate_record('tasks', row, store)
        store.put('tasks', identifier, row)
        store.request_refresh()
        return {'ok': True, 'updated': 1, 'tasks': [row], 'message': 'Saved.'}
    import hashlib
    job_id = _identifier(payload.get('job_id'))
    starts_at = explicit_timestamp(_text(payload, 'starts_at', 50), 'starts_at')
    interview_round = _text(payload, 'round', 200)
    if not interview_round.strip():
        raise ValueError('Enter the interview round.')
    identifier = _identifier(payload['id']) if payload.get('id') else hashlib.sha256(
        (job_id + starts_at + interview_round).encode()).hexdigest()[:16]
    row = {'id': identifier, 'job_id': job_id, 'starts_at': starts_at, 'round': interview_round}
    if 'notes' in payload:
        row['notes'] = _text(payload, 'notes')
    row = validate_record('interviews', row, store)
    store.put('interviews', identifier, row)
    store.request_refresh()
    return {'ok': True, 'updated': 1, 'interviews': [row], 'message': 'Saved.'}

"""Account-scoped PDF annotation drafts and reviewed immutable revisions.

PDF.js supplies a document map; it is data, never executable instructions. Text
offsets use UTF-16 so browser selections and backend edits have identical anchors.
The PDF renderer builds a clean, reflowed PDF from ``resolved_text``; originals
are never covered with white rectangles or changed in place.
"""
import base64
import binascii
import copy
import hashlib
import json
import math
import os
import re
import secrets

from .resumes import MAX_TEXT, PROFILE_ID, PROFILE_KIND, RESUME_KIND, _public_resume, _records, _settings, _text
from .storage import utcnow


REVIEW_KIND = 'resume_reviews'
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_ANNOTATIONS = 200


def _integer(value, label, minimum=0, maximum=400000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('%s must be an integer from %s to %s.' % (label, minimum, maximum))
    return value


def _utf16(text):
    try:
        return text.encode('utf-16-le')
    except UnicodeEncodeError:
        raise ValueError('Resume text contains an invalid Unicode character.') from None


def _slice(text, start, end):
    encoded = _utf16(text)
    _integer(start, 'Selection start', 0, len(encoded) // 2)
    _integer(end, 'Selection end', start, len(encoded) // 2)
    try:
        # Decode prefix and suffix too: an empty selection must not split an
        # astral Unicode character even when its own empty slice is decodable.
        encoded[:start * 2].decode('utf-16-le')
        encoded[end * 2:].decode('utf-16-le')
        return encoded[start * 2:end * 2].decode('utf-16-le')
    except UnicodeDecodeError:
        raise ValueError('Select complete characters in the PDF text.') from None


def _rect(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError('Annotation rectangles require x, y, width and height.')
    if any(type(part) not in (int, float) or not math.isfinite(part) or not 0 <= part <= 1 for part in value):
        raise ValueError('Annotation rectangles must use finite normalized coordinates.')
    if value[0] + value[2] > 1.000001 or value[1] + value[3] > 1.000001:
        raise ValueError('Annotation rectangles must stay within the page.')
    return list(value)


def _document(value):
    if not isinstance(value, dict):
        raise ValueError('Provide the PDF text and page map.')
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ValueError('The PDF page map contains unsupported data.') from None
    if size > MAX_DOCUMENT_BYTES:
        raise ValueError('The PDF page map is too large (maximum 1 MiB).')
    text = _text(value.get('text'), 'PDF text', required=False)
    _utf16(text)
    raw_pages = value.get('pages')
    if not isinstance(raw_pages, list) or not 1 <= len(raw_pages) <= 50:
        raise ValueError('Review a PDF with between 1 and 50 pages.')
    pages = []
    for number, page in enumerate(raw_pages, 1):
        if not isinstance(page, dict) or page.get('number') != number:
            raise ValueError('PDF pages must be numbered in order starting at 1.')
        dimensions = []
        for key in ('width', 'height'):
            part = page.get(key)
            if type(part) not in (int, float) or not math.isfinite(part) or not 1 <= part <= 20000:
                raise ValueError('PDF page dimensions must be finite and positive.')
            dimensions.append(part)
        pages.append({'number': number, 'width': dimensions[0], 'height': dimensions[1]})
    raw_lines = value.get('lines', [])
    if not isinstance(raw_lines, list) or len(raw_lines) > 10000:
        raise ValueError('The PDF has too many text anchors.')
    lines = []
    previous_end, previous_page = 0, 1
    for raw in raw_lines:
        if not isinstance(raw, dict):
            raise ValueError('PDF text anchors must be objects.')
        page = _integer(raw.get('page'), 'Text page', 1, len(pages))
        start, end = raw.get('start'), raw.get('end')
        actual = _slice(text, start, end)
        if actual != raw.get('text') or not actual or start < previous_end or page < previous_page:
            raise ValueError('PDF text anchors must match the extracted text in page order.')
        lines.append({'page': page, 'start': start, 'end': end, 'text': actual,
                      'rect': _rect(raw.get('rect'))})
        previous_end, previous_page = end, page
    if text.strip() and not lines:
        raise ValueError('Extracted PDF text needs selectable page anchors.')
    return {'text': text, 'pages': pages, 'lines': lines}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _summary(review):
    return {key: copy.deepcopy(review[key]) for key in
            ('id', 'resume_id', 'job_id', 'job_title', 'company', 'source_sha256',
             'revision', 'status', 'created_at', 'updated_at', 'saved_resume_id') if key in review}


def _public(review):
    result = copy.deepcopy(review)
    result['resolved_text'] = resolve_text(result['document']['text'], result['annotations'])
    result['pending_count'] = sum(item['kind'] == 'replace' and item['status'] == 'pending'
                                  for item in result['annotations'])
    return result


def review_list(root, resume_id=None, job_id=None):
    from .application_batches import _id, resume_content, workspace_id
    if resume_id is not None:
        resume_content(root, _id(resume_id, 'resume'))
    if job_id is not None:
        _id(job_id, 'job')
    rows = _records(root, (REVIEW_KIND,))[REVIEW_KIND]
    return {'workspace_id': workspace_id(root), 'reviews': [_summary(row) for row in reversed(rows)
            if (resume_id is None or row['resume_id'] == resume_id) and
               (job_id is None or row.get('job_id') == job_id)]}


def review_detail(root, review_id):
    from .application_batches import _id, workspace_id
    _id(review_id, 'review')
    row = next((value for value in _records(root, (REVIEW_KIND,))[REVIEW_KIND]
                if value['id'] == review_id), None)
    if row is None:
        raise ValueError('This PDF review does not exist in this workspace.')
    return {'workspace_id': workspace_id(root), 'review': _public(row)}


def _review(store, payload, allow_saved=False):
    from .application_batches import _id
    review = store.get(REVIEW_KIND, _id(payload.get('review_id'), 'review'))
    if not review:
        raise ValueError('This PDF review does not exist in this workspace.')
    if allow_saved and review['status'] == 'saved':
        return review
    if type(payload.get('revision')) is not int or payload['revision'] != review['revision']:
        raise ValueError('This PDF review changed. Reload it before saving your changes.')
    if review['status'] != 'draft':
        raise ValueError('This review is already saved. Open the saved resume to create another revision.')
    return review


def _job(store, identifier):
    from .application_batches import _id
    if not identifier:
        return None
    job = store.get('jobs', _id(identifier, 'job'))
    if not job:
        raise ValueError('Choose a role saved in this workspace.')
    return job


def _anchored(document, raw, previous=None, suggestion=False):
    from .application_batches import _id
    if not isinstance(raw, dict):
        raise ValueError('Annotations must be objects.')
    identifier = raw.get('id') or 'edit-' + secrets.token_hex(12)
    _id(identifier, 'annotation')
    kind = raw.get('kind', 'replace')
    if kind not in ('replace', 'note'):
        raise ValueError('Choose a replacement or a note annotation.')
    start, end = raw.get('start'), raw.get('end')
    before = _slice(document['text'], start, end)
    if raw.get('before') != before or (kind == 'replace' and start == end):
        raise ValueError('An annotation no longer matches the selected original text.')
    relevant = [line for line in document['lines'] if line['start'] < end and line['end'] > start]
    page = raw.get('page')
    if page is None and relevant:
        page = relevant[0]['page']
    _integer(page, 'Annotation page', 1, len(document['pages']))
    if start < end and (not relevant or any(line['page'] != page for line in relevant)):
        raise ValueError('Keep each annotation selection on one PDF page.')
    rects = raw.get('rects')
    if rects is None:
        rects = [line['rect'] for line in relevant]
    if not isinstance(rects, list) or not 1 <= len(rects) <= 500:
        raise ValueError('An annotation needs between 1 and 500 page rectangles.')
    state = raw.get('status', 'pending')
    if state not in ('pending', 'accepted', 'rejected'):
        raise ValueError('Choose pending, accepted or rejected for each annotation.')
    source = raw.get('source', 'manual')
    if source not in ('manual', 'local', 'ai') or (not previous and not suggestion and source != 'manual'):
        raise ValueError('New manual annotations must use manual provenance.')
    after = _text(raw.get('after', ''), 'Replacement text', required=False)
    note = _text(raw.get('note', ''), 'Annotation note', maximum=10000, required=False)
    _utf16(after)
    _utf16(note)
    if kind == 'note' and not note.strip():
        raise ValueError('Enter a note for this annotation.')
    reason = _text(raw.get('reason', ''), 'Edit reason', maximum=5000, required=False)
    _utf16(reason)
    evidence = raw.get('evidence', [])
    if not isinstance(evidence, list) or len(evidence) > 30:
        raise ValueError('Edit evidence must be a bounded list of text references.')
    evidence = [_text(item, 'Edit evidence', maximum=2000) for item in evidence]
    for item in evidence:
        _utf16(item)
    row = {'id': identifier, 'kind': kind, 'start': start, 'end': end, 'before': before,
           'after': after if kind == 'replace' else '', 'note': note, 'page': page,
           'rects': [_rect(rect) for rect in rects], 'status': state,
           'source': source, 'reason': reason, 'evidence': evidence}
    if previous:
        # Suggestions retain their model/local provenance even when a user edits
        # them. The flag makes that intervention visible on later review.
        row['source'] = previous['source']
        if previous['source'] != 'manual':
            row['evidence'] = copy.deepcopy(previous.get('evidence', []))
            row['reason'] = previous.get('reason', '')
        if any(row.get(key) != previous.get(key) for key in ('before', 'after', 'start', 'end', 'note')):
            row['user_edited'] = True
        elif previous.get('user_edited'):
            row['user_edited'] = True
    return row


def _annotations(document, values, previous=(), suggestions=False):
    if not isinstance(values, list) or len(values) > MAX_ANNOTATIONS:
        raise ValueError('Keep at most 200 annotations in one PDF review.')
    known = {row['id']: row for row in previous}
    result, ids = [], set()
    for raw in values:
        row = _anchored(document, raw, known.get(raw.get('id')) if isinstance(raw, dict) else None, suggestions)
        if row['id'] in ids:
            raise ValueError('Annotation IDs must be unique.')
        ids.add(row['id'])
        result.append(row)
    active = sorted((row for row in result if row['kind'] == 'replace' and row['status'] != 'rejected'),
                    key=lambda row: (row['start'], row['end']))
    if any(left['end'] > right['start'] for left, right in zip(active, active[1:])):
        raise ValueError('Replacement annotations overlap. Reject or remove one before continuing.')
    resolve_text(document['text'], result)
    return result


def resolve_text(text, annotations):
    encoded = _utf16(text)
    edits = sorted((row for row in annotations if row['kind'] == 'replace' and row['status'] == 'accepted'),
                   key=lambda row: row['start'], reverse=True)
    for row in edits:
        encoded = encoded[:row['start'] * 2] + _utf16(row['after']) + encoded[row['end'] * 2:]
    resolved = encoded.decode('utf-16-le')
    if len(resolved) > MAX_TEXT:
        raise ValueError('The revised resume exceeds 200,000 characters.')
    return resolved


def _save_review(store, review):
    review['revision'] += 1
    review['updated_at'] = utcnow()
    store.put(REVIEW_KIND, review['id'], review)
    return {'ok': True, 'review': _public(review)}


def _save_pdf(root, store, review, payload):
    from .application_batches import MAX_RESUME_BYTES, _resume_bytes, _resume_directory, _resume_record
    if payload.get('confirmed') is not True:
        raise ValueError('Review and confirm the PDF revision before saving it.')
    if any(row['kind'] == 'replace' and row['status'] == 'pending' for row in review['annotations']):
        raise ValueError('Accept or reject every suggested text change before saving a revision.')
    accepted = [row for row in review['annotations'] if row['kind'] == 'replace' and row['status'] == 'accepted']
    if not accepted:
        raise ValueError('Accept at least one text edit to save a new PDF revision. Your notes remain saved in this review.')
    text = resolve_text(review['document']['text'], review['annotations'])
    if payload.get('text') != text:
        raise ValueError('The PDF revision text differs from the accepted edits. Refresh its preview before saving.')
    _text(text, 'Reviewed PDF text')
    parent = _resume_record(store, review['resume_id'])
    _resume_bytes(root, parent)
    if parent['sha256'] != review['source_sha256']:
        raise ValueError('The source PDF changed. Create a new review from the current original.')
    encoded = payload.get('content_base64')
    if not isinstance(encoded, str) or not 0 < len(encoded) <= ((MAX_RESUME_BYTES + 2) // 3) * 4:
        raise ValueError('PDF revisions must be no larger than 5 MiB.')
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('The revised PDF could not be decoded.') from None
    if not 0 < len(content) <= MAX_RESUME_BYTES or not content.startswith(b'%PDF-') or b'%%EOF' not in content[-4096:]:
        raise ValueError('Provide the generated PDF revision, up to 5 MiB.')
    label = _text(payload.get('label'), 'Revision name', maximum=200)
    _utf16(label)
    if 'set_default' in payload and not isinstance(payload['set_default'], bool):
        raise ValueError('Set default must be a boolean.')
    existing = store.all(RESUME_KIND)
    if len(existing) >= 200 or sum(row.get('size', 0) for row in existing) + len(content) > 100 * 1024 * 1024:
        raise ValueError('The resume library has reached its limit of 200 versions or 100 MiB.')
    stem = re.sub(r'[^\w .-]', '', label).strip(' .')[:160] or 'Resume revision'
    if stem.lower().endswith('.pdf'):
        stem = stem[:-4]
    identifier = 'resume-' + secrets.token_hex(16)
    now = utcnow()
    row = {'id': identifier, 'filename': stem + '.pdf', 'label': label, 'mime': 'application/pdf',
           'size': len(content), 'sha256': hashlib.sha256(content).hexdigest(), 'text': text,
           'created_at': now, 'reviewed_at': now, 'extraction_status': 'provided_text',
           'parent_resume_id': parent['id'], 'version': parent.get('version', 1) + 1,
           'job_id': review.get('job_id'), 'source': {'kind': 'pdf_review', 'review_id': review['id'],
           'original_sha256': review['source_sha256'], 'layout': 'reflowed', 'accepted_edits': len(accepted)}}
    review.update(status='saved', saved_resume_id=identifier, updated_at=now, revision=review['revision'] + 1)
    groups = {RESUME_KIND: [row], REVIEW_KIND: [review]}
    if payload.get('set_default'):
        settings = _settings(store)
        settings.update(id=PROFILE_ID, default_resume_id=identifier, updated_at=now)
        groups[PROFILE_KIND] = [settings]
    path = _resume_directory(root, True) / identifier
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        store.put_groups(groups)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {'ok': True, 'review': _public(review), 'resume': _public_resume(row)}


def perform_review_action(root, store, config, profile, payload):
    from .application_batches import _resume_record, _resume_bytes
    if not isinstance(payload, dict):
        raise ValueError('PDF review requests must be objects.')
    op = payload.get('op')
    if op == 'pdf_review_create':
        parent = _resume_record(store, payload.get('resume_id'))
        if parent.get('mime') != 'application/pdf':
            raise ValueError('Choose a PDF resume to open the annotation viewer.')
        _resume_bytes(root, parent)
        job = _job(store, payload.get('job_id'))
        document = _document(payload.get('document'))
        fingerprint = _digest({'sha256': parent['sha256'], 'job_id': job['id'] if job else None,
                               'document': document})
        reviews = store.all(REVIEW_KIND)
        existing = next((row for row in reversed(reviews) if row.get('fingerprint') == fingerprint
                         and row['status'] == 'draft'), None)
        if existing:
            return {'ok': True, 'review': _public(existing), 'reopened': True}
        if len(reviews) >= 500:
            raise ValueError('The workspace has reached its limit of 500 PDF reviews.')
        now = utcnow()
        review = {'id': 'review-' + secrets.token_hex(16), 'resume_id': parent['id'],
                  'job_id': job['id'] if job else None, 'job_title': job.get('title', '') if job else '',
                  'company': job.get('company', '') if job else '', 'source_sha256': parent['sha256'],
                  'document': document, 'annotations': [], 'fingerprint': fingerprint,
                  'revision': 1, 'status': 'draft', 'ai_attempts': 0, 'created_at': now, 'updated_at': now}
        store.put(REVIEW_KIND, review['id'], review)
        return {'ok': True, 'review': _public(review)}
    if op not in ('pdf_review_update', 'pdf_review_suggest', 'pdf_review_save'):
        raise ValueError('Choose a supported PDF review action.')
    review = _review(store, payload, allow_saved=op == 'pdf_review_save')
    if op == 'pdf_review_update':
        review['annotations'] = _annotations(review['document'], payload.get('annotations'), review['annotations'])
        return _save_review(store, review)
    if op == 'pdf_review_suggest':
        from .resume_ai import capabilities, suggest_edits
        job = _job(store, review.get('job_id'))
        if not job:
            raise ValueError('Choose a target role before requesting tailoring suggestions.')
        mode = payload.get('mode', 'local')
        if mode not in ('local', 'ai'):
            raise ValueError('Choose local or AI suggestions.')
        if mode == 'ai':
            capability = capabilities(root, config)
            if not capability.get('ai_available'):
                raise ValueError(capability.get('ai_reason') or 'AI suggestions are not configured for this workspace.')
            if review.get('ai_attempts', 0) >= 3:
                raise ValueError('This review has reached its limit of three AI suggestion attempts. Continue editing or use local suggestions.')
            # Consume one bounded attempt before contacting a paid provider;
            # failed requests also count and are never retried automatically.
            # This budget change does not revise the user's annotation content.
            review['ai_attempts'] = review.get('ai_attempts', 0) + 1
            review['updated_at'] = utcnow()
            store.put(REVIEW_KIND, review['id'], review)
        result = suggest_edits(root, review['document'], job, profile, config=config, mode=mode)
        suggestions = result.get('suggestions')
        if not isinstance(suggestions, list) or len(suggestions) > MAX_ANNOTATIONS:
            raise ValueError('The suggestion provider returned too many edits.')
        added = 0
        for raw in suggestions:
            item = _anchored(review['document'], {**raw, 'kind': 'replace', 'status': 'pending', 'source': mode}, suggestion=True)
            duplicates = any(row['kind'] == 'replace' and
                             (row['start'], row['end'], row['after']) == (item['start'], item['end'], item['after'])
                             for row in review['annotations'])
            overlaps = any(row['kind'] == 'replace' and row['status'] != 'rejected' and
                           row['start'] < item['end'] and row['end'] > item['start']
                           for row in review['annotations'])
            if duplicates or overlaps or len(review['annotations']) >= MAX_ANNOTATIONS:
                continue
            review['annotations'].append(item)
            added += 1
        review['last_suggestion'] = {key: result[key] for key in ('method', 'model', 'message') if key in result}
        response = _save_review(store, review)
        response.update(added=added, message=result.get('message', ''), method=result.get('method', mode))
        return response
    if review['status'] == 'saved':
        saved = store.get(RESUME_KIND, review['saved_resume_id'])
        if not saved:
            raise ValueError('The saved PDF revision is unavailable.')
        return {'ok': True, 'review': _public(review), 'resume': _public_resume(saved), 'already_saved': True}
    return _save_pdf(root, store, review, payload)

"""Private immutable resume versions and evidence-preserving review drafts.

All mutations are called under the workflow lock. Drafting is local and extractive:
job text is only used to score existing bullet points, never as instructions or a
source of candidate claims. Uploaded originals and submitted materials stay intact.
"""
import base64
import copy
import hashlib
import io
import json
import re
import secrets
import sqlite3
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from .storage import utcnow

RESUME_KIND = 'application_resumes'
PROFILE_KIND = 'resume_profile'
DRAFT_KIND = 'resume_drafts'
MAX_TEXT = 200000
PROFILE_ID = 'default'


def _records(root, kinds):
    result = {kind: [] for kind in kinds}
    path = Path(root) / 'data/jobflow.sqlite3'
    if not path.exists():
        return result
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    try:
        marks = ','.join('?' for _ in kinds)
        for kind, body in db.execute('SELECT kind,body FROM records WHERE kind IN (%s) ORDER BY created_at,id' % marks, tuple(kinds)):
            result[kind].append(json.loads(body))
    finally:
        db.close()
    return result


def _profile(root):
    path = Path(root) / 'config/profile.json'
    if not path.exists() or path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
        return {}
    value = json.loads(path.read_text(encoding='utf-8'))
    return value if isinstance(value, dict) else {}


def _applicant_from_profile(profile):
    full = str(profile.get('name', '')).strip().split(None, 1)
    result = {'first_name': full[0] if full else '', 'last_name': full[1] if len(full) > 1 else ''}
    for key, candidates in {'email': ('email',), 'phone': ('phone',), 'location': ('location',),
                            'linkedin': ('linkedin', 'linkedin_url'), 'website': ('website', 'website_url')}.items():
        result[key] = next((profile[name] for name in candidates if isinstance(profile.get(name), str)), '')
    return result


def _public_resume(record):
    value = {key: copy.deepcopy(item) for key, item in record.items() if key not in ('text', 'source_path')}
    value.setdefault('label', record['filename'])
    value.setdefault('version', 1)
    value.setdefault('parent_resume_id', None)
    value.setdefault('job_id', None)
    value.setdefault('extraction_status', 'needs_text')
    value['editable'] = bool(record.get('text', '').strip())
    return value


def resume_library(root):
    """Read without opening a writable database or importing files."""
    from .application_batches import workspace_id
    from .resume_ai import capabilities
    records = _records(root, (RESUME_KIND, PROFILE_KIND, DRAFT_KIND))
    settings = next((row for row in records[PROFILE_KIND] if row['id'] == PROFILE_ID), {})
    resumes = records[RESUME_KIND]
    default = settings.get('default_resume_id')
    if default not in {row['id'] for row in resumes}:
        default = resumes[0]['id'] if resumes else None
    applicant = _applicant_from_profile(_profile(root))
    # Upload extraction fills missing contact details only; explicit user fields,
    # including intentional blanks, always take precedence.
    default_row = next((row for row in resumes if row['id'] == default), {})
    applicant.update({key: value for key, value in _contact_suggestions(default_row.get('text', '')).items()
                      if not applicant.get(key)})
    applicant.update(settings.get('applicant', {}))
    drafts = [{key: copy.deepcopy(value) for key, value in row.items()
               if key not in ('original_text', 'text')} for row in reversed(records[DRAFT_KIND])]
    return {'workspace_id': workspace_id(root), 'pdf_ai': capabilities(root),
            'resumes': [_public_resume(row) for row in reversed(resumes)], 'applicant': applicant,
            'default_resume_id': default, 'drafts': drafts}


def resume_detail(root, resume_id):
    from .application_batches import resume_content
    record, content = resume_content(root, resume_id)
    text = record.get('text')
    if text is None:
        text, unused = extract_text(record['filename'], content)
    return {'resume': _public_resume({**record, 'text': text}), 'text': text}



def resume_draft_detail(root, draft_id):
    from .application_batches import _id
    _id(draft_id, 'draft')
    draft = next((row for row in _records(root, (DRAFT_KIND,))[DRAFT_KIND] if row['id'] == draft_id), None)
    if not draft:
        raise ValueError('This resume draft does not exist in this workspace.')
    return {'draft': draft}


def extract_text(filename, content):
    suffix = Path(filename).suffix.lower()
    if suffix in ('.txt', '.md'):
        try:
            text = content.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise ValueError('Text resumes must use UTF-8 encoding.') from None
    elif suffix == '.docx':
        from .onboarding import _docx_text
        try:
            text = _docx_text(content)
        except ValueError as error:
            # Keep previously accepted DOCX containers usable for applications;
            # unsafe archives still fail validation before extraction.
            if str(error) != 'The DOCX document text could not be read.':
                raise
            return '', 'needs_text'
    else:
        return '', 'needs_text'
    if any(ord(char) < 32 and char not in '\n\r\t' for char in text):
        raise ValueError('Resume text must not contain binary data.')
    if len(text) > MAX_TEXT:
        return '', 'needs_text'
    return text, 'extracted' if text.strip() else 'needs_text'


def _contact_suggestions(text):
    result = {}
    email = re.search(r'(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])', text)
    if email:
        result['email'] = email.group()
    linkedin = re.search(r'https?://(?:www\.)?linkedin\.com/in/[^\s<>]+', text, re.I)
    if linkedin:
        result['linkedin'] = linkedin.group().rstrip(').,;')
    phone = re.search(r'(?<!\d)(?:\+1[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}(?!\d)', text)
    if phone:
        result['phone'] = phone.group()
    return result


def _settings(store):
    return store.get(PROFILE_KIND, PROFILE_ID) or {'id': PROFILE_ID, 'applicant': {}}


def _save_settings(store, value):
    value['updated_at'] = utcnow()
    store.put(PROFILE_KIND, PROFILE_ID, value)


def import_resume(root, store, filename, content, provenance=None):
    """Copy a known upload into the shared library, deduplicating exact bytes."""
    from .application_batches import _upload
    digest = hashlib.sha256(content).hexdigest()
    existing = next((row for row in store.all(RESUME_KIND) if row.get('sha256') == digest), None)
    if existing:
        return existing
    if Path(filename).suffix.lower() == '.md':
        filename = Path(filename).stem + '.txt'
    if len(filename) > 200:
        filename = Path(filename).stem[:190] + Path(filename).suffix.lower()
    result = _upload(root, store, {'filename': filename, 'content_base64': base64.b64encode(content).decode('ascii')})
    row = result['resume']
    if provenance:
        row['source'] = copy.deepcopy(provenance)
        store.put(RESUME_KIND, row['id'], row)
    return row


def sync_existing(root, store, profile):
    """Reuse onboarding uploads and safe workspace-local configured sources.

    Source paths are never taken from an HTTP request. Even configured paths must
    stay within inputs: an account can edit its profile but cannot read host files.
    Remote source references remain provenance; this operation makes no requests.
    """
    from .onboarding import _uploads, _read_json, _root_path, _write_private, _json_bytes
    from .application_batches import _resume_bytes
    root = _root_path(root).resolve()
    imported, skipped = [], []
    for existing in store.all(RESUME_KIND):
        if 'text' in existing:
            continue
        try:
            text, status = extract_text(existing['filename'], _resume_bytes(root, existing))
            existing.update(text=text, extraction_status=status)
            existing.setdefault('label', existing['filename'])
            existing.setdefault('version', 1)
            store.put(RESUME_KIND, existing['id'], existing)
        except (ValueError, OSError) as error:
            skipped.append({'filename': existing['filename'], 'reason': str(error)})
    for metadata in _uploads(root):
        try:
            full = _read_json(root / 'inputs/resumes' / (metadata['id'] + '.json'))
            name = full['filename']
            path = root / 'inputs/resumes' / (full['id'] + Path(name).suffix.lower())
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 5 * 1024 * 1024:
                raise ValueError('The original upload is unavailable.')
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != full['sha256']:
                raise ValueError('The original upload changed.')
            row = import_resume(root, store, name, content, {'kind': 'onboarding', 'upload_id': full['id']})
            if full.get('application_resume_id') != row['id']:
                full['application_resume_id'] = row['id']
                _write_private(root / 'inputs/resumes' / (full['id'] + '.json'), _json_bytes(full))
            imported.append(row['id'])
        except (ValueError, OSError, KeyError) as error:
            skipped.append({'filename': metadata.get('filename', ''), 'reason': str(error)})
    for source in profile.get('resume_sources', []):
        if not isinstance(source, str) or urlsplit(source).scheme:
            continue
        path = Path(source)
        if not path.is_absolute():
            path = root / path
        try:
            relative = path.relative_to(root / 'inputs')
            chain = [root / 'inputs']
            for part in relative.parts:
                chain.append(chain[-1] / part)
            if any(item.is_symlink() for item in chain) or '..' in relative.parts:
                raise ValueError('Configured source must be a regular file inside workspace inputs.')
            if not path.is_file() or path.stat().st_size > 5 * 1024 * 1024:
                raise ValueError('Configured resume is unavailable or too large.')
            row = import_resume(root, store, path.name, path.read_bytes(), {'kind': 'configured_source'})
            imported.append(row['id'])
        except (ValueError, OSError) as error:
            skipped.append({'filename': path.name, 'reason': str(error)})
    return {'ok': True, 'imported_resume_ids': list(dict.fromkeys(imported)), 'skipped': skipped,
            **resume_library(root)}


def _text(value, label, maximum=MAX_TEXT, required=True):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()) or any(ord(c) < 32 and c not in '\n\r\t' for c in value):
        raise ValueError('%s must be %stext of at most %s characters.' % (label, 'nonempty ' if required else '', maximum))
    return value


def _contains(text, term):
    return bool(re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', text, re.I))


def _tailor(text, job, profile):
    description = str(job.get('title', '')) + '\n' + str(job.get('description', ''))
    skills = [value for value in profile.get('skills', []) if isinstance(value, str) and _contains(description, value)]
    common = {'experience', 'engineer', 'engineering', 'work', 'team', 'will', 'with', 'your', 'that', 'this', 'have', 'from', 'years', 'about', 'their', 'other', 'role', 'such', 'skills', 'build', 'working', 'including', 'strong', 'systems', 'ability'}
    terms = list(dict.fromkeys(skills + [word for word in re.findall(r'[A-Za-z][A-Za-z0-9+#.-]{3,}', description.lower()) if word not in common]))[:300]
    matched = [skill for skill in skills if _contains(text, skill)]
    missing = [skill for skill in skills if not _contains(text, skill)]
    lines = text.splitlines()
    changed = 0
    ambiguous_blocks = 0
    cursor = 0
    bullet = re.compile(r'^([ \t]*)(?:[-*•▪◦‣\x88])\s+')
    while cursor < len(lines):
        match = bullet.match(lines[cursor])
        if not match:
            cursor += 1
            continue
        indent = match.group(1)
        chunks = []
        ambiguous = False
        end = cursor
        while end < len(lines):
            marker = bullet.match(lines[end])
            if not marker or marker.group(1) != indent:
                break
            next_line = end + 1
            while next_line < len(lines) and lines[next_line].strip() and not bullet.match(lines[next_line]):
                previous = lines[next_line - 1].rstrip()
                current = lines[next_line].lstrip()
                # PDF extraction wraps bullets into physical lines. Move the
                # entire claim together, stopping at sentence-end/section
                # boundaries so employers, projects and dates stay in place.
                explicit_indent = len(lines[next_line]) - len(lines[next_line].lstrip()) > len(indent)
                continuation = explicit_indent or (not re.search(r'[.!?]$', previous) and (
                    len(previous) > 70 or previous.endswith('-') or current[:1].islower()))
                if not continuation:
                    # An unindented numeric/uppercase continuation cannot be
                    # distinguished from a heading reliably. Retain this whole
                    # block instead of moving a metric to another achievement.
                    if not re.search(r'[.!?]$', previous):
                        ambiguous = True
                    break
                next_line += 1
            chunks.append(lines[end:next_line])
            end = next_line
        ranked = sorted(enumerate(chunks), key=lambda pair: (-sum(
            _contains(' '.join(pair[1]), term) for term in terms), pair[0]))
        reordered = [chunk for _, chunk in ranked]
        if ambiguous:
            ambiguous_blocks += 1
        elif reordered != chunks:
            changed += 1
            lines[cursor:end] = [line for chunk in reordered for line in chunk]
        cursor = end
    changes = ['Prioritized role-relevant bullets within %s existing section%s; all wording is preserved.' % (changed, '' if changed == 1 else 's')] if changed else ['Reviewed the existing wording against the role. No bullet reordering was needed.']
    if ambiguous_blocks:
        changes.append('Kept %s section%s in the original order because wrapped-line boundaries need review.' % (ambiguous_blocks, '' if ambiguous_blocks == 1 else 's'))
    if missing:
        changes.append('No claims were added for terms absent from this resume: ' + ', '.join(missing) + '.')
    return '\n'.join(lines) + ('\n' if text.endswith(('\n', '\r')) else ''), changes, matched, missing


def _docx(text):
    paragraphs = []
    headings = {'skills', 'technical skills', 'work experience', 'experience', 'professional experience',
                'education', 'projects', 'publications', 'certifications', 'summary', 'professional summary'}
    for index, line in enumerate(text.splitlines()):
        visible = line.replace('\x88', '•')
        style = 'Name' if index == 0 else ('Heading1' if visible.strip().lower() in headings else 'Normal')
        if re.match(r'^\s*[-*•▪◦‣]\s+', visible):
            style = 'ResumeBullet'
        paragraphs.append('<w:p><w:pPr><w:pStyle w:val="%s"/></w:pPr><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % (style, escape(visible)))
    document = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' + ''.join(paragraphs) + '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="864" w:right="864" w:bottom="864" w:left="864"/></w:sectPr></w:body></w:document>'
    styles = '''<?xml version="1.0" encoding="UTF-8"?><w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:sz w:val="21"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="30" w:line="240" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Name"><w:name w:val="Candidate name"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:after="100"/></w:pPr><w:rPr><w:b/><w:sz w:val="34"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="Heading 1"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="160" w:after="60"/></w:pPr><w:rPr><w:b/><w:sz w:val="23"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="ResumeBullet"><w:name w:val="Resume bullet"/><w:basedOn w:val="Normal"/><w:pPr><w:ind w:left="150" w:hanging="150"/><w:spacing w:before="40" w:after="0"/></w:pPr></w:style>
</w:styles>'''
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>')
        archive.writestr('_rels/.rels', '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr('word/_rels/document.xml.rels', '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        archive.writestr('word/document.xml', document)
        archive.writestr('word/styles.xml', styles)
    return stream.getvalue()



def validate_applicant(value):
    from .application_batches import _applicant
    applicant = _applicant(value)
    for field in ('linkedin', 'website'):
        if applicant.get(field):
            parsed = urlsplit(applicant[field])
            if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('Applicant links must be HTTP(S) URLs without credentials.')
    if applicant.get('email') and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', applicant['email']):
        raise ValueError('Enter a valid applicant email address.')
    return applicant

def perform_resume_action(root, store, config, profile, payload):
    from .application_batches import _applicant, _id, _resume_record, _resume_bytes, _upload
    op = payload.get('op')
    if op == 'resume_sync':
        return sync_existing(root, store, profile)
    if op == 'resume_profile':
        applicant = validate_applicant(payload.get('applicant', {}))
        settings = _settings(store)
        settings['applicant'] = {**settings.get('applicant', {}), **applicant}
        if 'default_resume_id' in payload:
            settings['default_resume_id'] = _resume_record(store, payload['default_resume_id'])['id']
        _save_settings(store, settings)
        return {'ok': True, **resume_library(root)}
    if op == 'resume_default':
        resume = _resume_record(store, payload.get('resume_id'))
        _resume_bytes(root, resume)
        settings = _settings(store)
        settings['default_resume_id'] = resume['id']
        _save_settings(store, settings)
        return {'ok': True, **resume_library(root)}
    if op == 'resume_draft':
        resume = _resume_record(store, payload.get('resume_id'))
        original, status = extract_text(resume['filename'], _resume_bytes(root, resume))
        original = resume.get('text', original)
        if 'source_text' in payload:
            original = _text(payload['source_text'], 'Pasted resume text')
        if not original.strip():
            raise ValueError('This PDF is saved for applications. Upload a DOCX/TXT version to tailor or edit its text.')
        job = None
        job_id = payload.get('job_id')
        if job_id:
            job = store.get('jobs', _id(job_id, 'job'))
            if not job:
                raise ValueError('Choose a role saved in this workspace.')
        text, changes, matched, missing = _tailor(original, job, profile) if job else (original, ['Edit a copy, then confirm to save a separate version.'], [], [])
        draft = {'id': 'draft-' + secrets.token_hex(16), 'resume_id': resume['id'],
                 'job_id': job_id or None, 'job_title': job.get('title') if job else None,
                 'company': job.get('company') if job else None, 'original_text': original, 'text': text,
                 'changes': changes, 'matched_skills': matched, 'missing_skills': missing,
                 'sources': [{'label': resume.get('label', resume['filename']), 'resume_id': resume['id']}],
                 'created_at': utcnow(), 'status': 'pending', 'method': 'local_extractive', 'source_text_provided': 'source_text' in payload}
        if job and job.get('url'):
            draft['sources'].append({'label': 'Recorded job posting', 'url': job['url']})
        store.put(DRAFT_KIND, draft['id'], draft)
        return {'ok': True, 'draft': draft}
    if op == 'resume_save_version':
        draft = store.get(DRAFT_KIND, _id(payload.get('draft_id'), 'draft'))
        if not draft:
            raise ValueError('This resume draft does not exist in this workspace.')
        if draft['status'] != 'pending':
            raise ValueError('This draft has already been saved. Start a new edit to create another version.')
        if payload.get('confirmed') is not True:
            raise ValueError('Review and confirm the resume text before saving a new version.')
        text = _text(payload.get('text'), 'Reviewed resume text')
        parent = _resume_record(store, draft['resume_id'])
        label = _text(payload.get('label', parent.get('label', parent['filename']) + ' — revised'), 'Resume name', 200)
        if 'set_default' in payload and not isinstance(payload['set_default'], bool):
            raise ValueError('Set default must be a boolean.')
        stem = re.sub(r'[^\w .-]', '', label).strip(' .')[:160] or 'resume'
        if stem.lower().endswith('.docx'):
            stem = stem[:-5]
        row = _upload(root, store, {'filename': stem + '.docx', 'content_base64': base64.b64encode(_docx(text)).decode('ascii')})['resume']
        row.update(label=label, parent_resume_id=parent['id'], version=parent.get('version', 1) + 1,
                   job_id=draft.get('job_id'), reviewed_at=utcnow(), text=text,
                   source={'kind': 'reviewed_draft', 'draft_id': draft['id'], 'method': draft['method']})
        draft.update(status='saved', saved_resume_id=row['id'], reviewed_at=row['reviewed_at'])
        groups = {RESUME_KIND: [row], DRAFT_KIND: [draft]}
        if payload.get('set_default'):
            settings = _settings(store)
            settings.update(default_resume_id=row['id'], updated_at=utcnow())
            groups[PROFILE_KIND] = [settings]
        store.put_groups(groups)
        return {'ok': True, 'resume': _public_resume(row), 'draft': draft}
    raise ValueError('Choose a valid resume action.')

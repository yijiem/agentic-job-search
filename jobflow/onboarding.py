"""Private, evidence-preserving onboarding for an authenticated workspace.

Callers must select the workspace from the authenticated account, never from a
request field. This module does not send messages, call a model, or infer resume
achievements. Uploaded text is a set of suggestions until the user saves it.
"""
import copy
import fcntl
import hashlib
import io
import json
import math
import os
import re
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from .storage import Store, utcnow
from .workflow import load_config


MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_RESUME_UPLOADS = 20
MAX_RESUME_STORAGE_BYTES = 100 * 1024 * 1024
MAX_TEXT_CHARS = 200000
MAX_EVIDENCE = 500
EXAMPLE_SETTINGS = Path(__file__).resolve().parents[1] / 'config/settings.example.json'
PROFILE_FIELDS = ('name', 'headline', 'skills', 'evidence', 'resume_sources')
PREFERENCE_SECTIONS = ('search', 'schedule', 'preparation', 'daily_goals')
FLAT_PREFERENCES = {
    'preferred_locations': ('search', 'preferred_locations'),
    'allowed_levels': ('search', 'allowed_levels'),
    'target_tc': ('search', 'target_tc'),
    'currency': ('search', 'currency'),
    'location_mode': ('search', 'location_mode'),
    'min_score': ('search', 'min_score'),
    'daily_minutes': ('preparation', 'daily_minutes'),
    'horizon_days': ('preparation', 'horizon_days'),
    'timezone': ('schedule', 'timezone'),
}
SECTION_FIELDS = {
    'search': {'preferred_locations', 'allowed_levels', 'target_tc', 'currency',
               'location_mode', 'min_score'},
    'schedule': {'timezone', 'hour', 'minute', 'days'},
    'preparation': {'daily_minutes', 'horizon_days', 'timezone'},
    'daily_goals': {'enabled', 'applications', 'learning_minutes', 'coding_minutes',
                    'system_design_minutes', 'application_minutes_each'},
}


def _root_path(root):
    root = Path(root).absolute()
    if root.is_symlink():
        raise ValueError('A workspace cannot be a symbolic link.')
    for relative in ('config', 'data', 'inputs', 'inputs/resumes'):
        candidate = root / relative
        if candidate.is_symlink():
            raise ValueError('Workspace data directories cannot be symbolic links.')
    return root


def _read_json(path, default=None):
    from .remote_files import ensure_path
    path = ensure_path(path)
    if path.is_symlink():
        raise ValueError('Workspace files cannot be symbolic links.')
    if not path.exists():
        return copy.deepcopy(default)
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('Workspace configuration is too large.')
    return json.loads(path.read_text(encoding='utf-8'))


def _write_private(path, content):
    """Replace one file without making an intermediate public-readable file."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Workspace files cannot be symbolic links.')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        from .remote_files import persist_path
        persist_path(path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')


@contextmanager
def _workspace_lock(root):
    from .remote_files import is_remote_root
    if is_remote_root(root):
        from .remote import RemoteLease
        with RemoteLease(root):
            yield
        return
    (root / 'data').mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / 'data/workflow.lock'
    if path.is_symlink():
        raise ValueError('The workspace lock cannot be a symbolic link.')
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another workflow is updating this workspace. Try again shortly.')
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _text(value, label, maximum, required=False):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError('%s must be text of at most %s characters.' % (label, maximum))
    if any(ord(char) < 32 and char not in '\n\r\t' for char in value):
        raise ValueError('%s contains unsupported control characters.' % label)
    if required and not value.strip():
        raise ValueError('%s is required.' % label)
    return value


def _string_list(value, label, maximum_items=100, maximum_chars=200, required=False):
    if not isinstance(value, list) or len(value) > maximum_items or (required and not value):
        raise ValueError('%s must be a list with %s to %s entries.' % (
            label, 1 if required else 0, maximum_items))
    for item in value:
        _text(item, label, maximum_chars, required=True)
    return copy.deepcopy(value)


def _integer(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError('%s must be an integer from %s to %s.' % (label, minimum, maximum))


def _source_template(sources):
    """Copy only public source descriptors supplied by the deployment operator."""
    if not isinstance(sources, list) or len(sources) > 1000:
        raise ValueError('The source template must contain at most 1000 sources.')
    result = []
    seen = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError('Each source template entry must be an object.')
        row = {key: copy.deepcopy(source[key]) for key in
               ('id', 'company', 'kind', 'board', 'url', 'enabled') if key in source}
        for key in ('id', 'company', 'kind', 'url'):
            _text(row.get(key), 'Source ' + key, 2000 if key == 'url' else 200, required=True)
        if row['kind'] not in ('greenhouse', 'ashby', 'lever', 'manual'):
            raise ValueError('The source template contains an unsupported source kind.')
        parsed = urlsplit(row['url'])
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Source template URLs must be public HTTPS URLs without credentials.')
        if row['id'] in seen:
            raise ValueError('Source template IDs must be unique.')
        seen.add(row['id'])
        if 'enabled' in row and not isinstance(row['enabled'], bool):
            raise ValueError('Source enabled must be a boolean.')
        if 'board' in row:
            _text(row['board'], 'Source board', 200)
        result.append(row)
    return result


def initialize_workspace(root, sources=None):
    """Initialize only missing files; never seed another user's personal data."""
    root = _root_path(root)
    template = _source_template(sources) if sources is not None else None
    with _workspace_lock(root):
        from .remote_files import hydrate_workspace, is_remote_root
        hydrate_workspace(root)
        config_directory = root / 'config'
        config_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        settings_path = config_directory / 'settings.json'
        profile_path = config_directory / 'profile.json'
        if not settings_path.exists():
            settings = _read_json(EXAMPLE_SETTINGS)
            settings['sources'] = template or []
            settings.setdefault('research', {})['enabled'] = False
            _write_private(settings_path, _json_bytes(settings))
        elif settings_path.is_symlink():
            raise ValueError('Workspace files cannot be symbolic links.')
        if not profile_path.exists():
            _write_private(profile_path, _json_bytes({
                'name': '', 'headline': '', 'skills': [], 'evidence': [], 'resume_sources': []}))
        elif profile_path.is_symlink():
            raise ValueError('Workspace files cannot be symbolic links.')
        database = root / 'data/jobflow.sqlite3'
        if database.is_symlink():
            raise ValueError('Workspace files cannot be symbolic links.')
        if not database.exists():
            Store(database).close()
            if not is_remote_root(root):
                os.chmod(database, 0o600)
    return onboarding_state(root)


def _preferences(settings):
    result = {name: {key: copy.deepcopy(value) for key, value in settings.get(name, {}).items()
                     if key in SECTION_FIELDS[name]} for name in PREFERENCE_SECTIONS}
    for name, (section, field) in FLAT_PREFERENCES.items():
        if field in settings.get(section, {}):
            result[name] = copy.deepcopy(settings[section][field])
    return result


def _uploads(root):
    from .remote_files import hydrate_upload_metadata
    hydrate_upload_metadata(root)
    directory = root / 'inputs/resumes'
    if not directory.exists():
        return []
    result = []
    for path in sorted(directory.glob('*.json')):
        if not re.fullmatch(r'[a-f0-9]{32}\.json', path.name):
            continue
        value = _read_json(path)
        if isinstance(value, dict) and value.get('id') == path.stem:
            # Large extracted bodies are returned only by the upload call.
            result.append({key: value.get(key) for key in (
                'id', 'filename', 'original_name', 'uploaded_at', 'size',
                'extraction_status', 'message', 'sha256', 'application_resume_id')})
    return sorted(result, key=lambda item: item.get('uploaded_at', ''), reverse=True)


def onboarding_state(root):
    """Read onboarding state without changing configuration or workflow history."""
    root = _root_path(root)
    profile = _read_json(root / 'config/profile.json', {})
    settings = _read_json(root / 'config/settings.json', {})
    if not isinstance(profile, dict) or not isinstance(settings, dict):
        raise ValueError('Workspace configuration must contain JSON objects.')
    complete = False
    if profile.get('evidence'):
        try:
            load_config(root)
            complete = all(isinstance(row, dict) and isinstance(row.get('text'), str)
                           and row['text'].strip() for row in profile['evidence'])
        except (ValueError, KeyError, TypeError, OSError):
            pass
    metadata = _read_json(root / 'config/onboarding.json', {})
    from .resumes import resume_library
    library = resume_library(root)
    return {
        'complete': bool(complete),
        'profile': {key: copy.deepcopy(profile[key]) for key in PROFILE_FIELDS if key in profile},
        'preferences': _preferences(settings),
        'uploads': _uploads(root),
        'applicant': library['applicant'],
        'resumes': library['resumes'],
        'default_resume_id': library['default_resume_id'],
        'source_count': sum(1 for source in settings.get('sources', [])
                            if source.get('enabled', True) and source.get('kind') != 'manual'),
        'existing_data_reused': bool(complete),
        'migrated': bool(isinstance(metadata, dict) and metadata.get('migrated')),
        'connections': [],
    }


def _docx_text(content):
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            items = archive.infolist()
            if len(items) > 2000 or sum(item.file_size for item in items) > 20 * 1024 * 1024:
                raise ValueError('This DOCX expands beyond the supported size.')
            if len({item.filename for item in items}) != len(items):
                raise ValueError('This DOCX contains duplicate file entries.')
            for item in items:
                parts = item.filename.replace('\\', '/').split('/')
                if item.filename.startswith(('/', '\\')) or '..' in parts:
                    raise ValueError('This DOCX contains invalid file paths.')
                if item.flag_bits & 1 or item.file_size > 10 * 1024 * 1024:
                    raise ValueError('Encrypted or oversized DOCX entries are unsupported.')
                if item.file_size > 1024 * 1024 and item.file_size > max(item.compress_size, 1) * 200:
                    raise ValueError('This DOCX has an unsafe compression ratio.')
                if 'vbaproject' in item.filename.lower():
                    raise ValueError('Macro-enabled documents are unsupported.')
            if '[Content_Types].xml' not in archive.namelist() or 'word/document.xml' not in archive.namelist():
                raise ValueError('Upload a valid DOCX document.')
            document = archive.read('word/document.xml')
    except (zipfile.BadZipFile, KeyError, RuntimeError, NotImplementedError):
        raise ValueError('Upload an unencrypted, valid DOCX document.')
    # DOCX normally stores UTF-8 XML. Reject NUL-bearing encodings as well so a
    # UTF-16 declaration cannot hide an entity definition from this guard.
    if b'\x00' in document or b'<!DOCTYPE' in document.upper() or b'<!ENTITY' in document.upper():
        raise ValueError('DOCX entity declarations are unsupported.')
    try:
        tree = ElementTree.fromstring(document)
    except ElementTree.ParseError:
        raise ValueError('The DOCX document text could not be read.')
    namespace = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    if tree.tag != namespace + 'document':
        raise ValueError('The DOCX document text could not be read.')
    paragraphs = []
    for paragraph in tree.iter(namespace + 'p'):
        pieces = []
        for node in paragraph.iter():
            if node.tag == namespace + 't':
                pieces.append(node.text or '')
            elif node.tag == namespace + 'tab':
                pieces.append('\t')
            elif node.tag in (namespace + 'br', namespace + 'cr'):
                pieces.append('\n')
        if pieces:
            paragraphs.append(''.join(pieces))
    return '\n'.join(paragraphs)


def _suggestions(text, upload_id):
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        # Keep supplied text verbatim, including bullets, spacing and metrics.
        # A very long paragraph remains available as text for manual selection.
        if len(line) > 10000:
            continue
        rows.append({'id': 'resume-' + upload_id + '-' + str(len(rows) + 1),
                     'text': line, 'tags': [], 'source_file_id': upload_id,
                     'source_url': ''})
        if len(rows) >= MAX_EVIDENCE:
            break
    return rows


def upload_resume(root, filename, content_bytes):
    """Retain an upload privately and return unconfirmed exact-text suggestions."""
    root = _root_path(root)
    _text(filename, 'Filename', 255, required=True)
    if any(ord(char) < 32 for char in filename):
        raise ValueError('Use a filename without control characters.')
    if '/' in filename or '\\' in filename or filename in ('.', '..'):
        raise ValueError('Use a filename without a directory path.')
    if not isinstance(content_bytes, bytes) or not 0 < len(content_bytes) <= MAX_UPLOAD_BYTES:
        raise ValueError('Upload a nonempty resume of at most 5 MiB.')
    extension = Path(filename).suffix.lower()
    if extension not in ('.txt', '.md', '.docx', '.pdf'):
        raise ValueError('Supported resume formats are TXT, Markdown, DOCX, and PDF.')
    text = ''
    status = 'extracted'
    message = 'Review the extracted statements before adding them to your profile.'
    if extension in ('.txt', '.md'):
        try:
            text = content_bytes.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise ValueError('Text resumes must use UTF-8 encoding.')
    elif extension == '.docx':
        text = _docx_text(content_bytes)
    else:
        if not content_bytes.startswith(b'%PDF-') or b'%%EOF' not in content_bytes[-4096:]:
            raise ValueError('Upload a valid PDF document.')
        status = 'needs_text'
        message = ('Your PDF is saved privately. Automatic PDF text extraction is not configured. '
                   'Paste its text below or upload a DOCX/TXT version, then review the statements.')
    _text(text, 'Resume text', MAX_TEXT_CHARS)
    if extension != '.pdf' and not text.strip():
        status = 'needs_text'
        message = 'No document text was found. Paste your resume text to review it.'
    upload_id = uuid.uuid4().hex
    suggestions = _suggestions(text, upload_id)
    value = {'id': upload_id, 'filename': filename, 'original_name': filename,
             'size': len(content_bytes), 'uploaded_at': utcnow(),
             'sha256': hashlib.sha256(content_bytes).hexdigest(),
             'extraction_status': status, 'message': message, 'text': text,
             'suggestions': suggestions, 'evidence_suggestions': suggestions}
    with _workspace_lock(root):
        directory = root / 'inputs/resumes'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        files = list(directory.iterdir())
        if any(path.is_symlink() for path in files):
            raise ValueError('Stored resume files cannot be symbolic links.')
        existing_uploads = sum(bool(re.fullmatch(r'[a-f0-9]{32}\.json', path.name))
                               for path in files if path.is_file())
        stored_bytes = sum(path.stat().st_size for path in files if path.is_file())
        from .remote_files import is_remote_root, file_manifest
        if is_remote_root(root):
            durable = file_manifest(root, 'inputs/resumes/')
            existing_uploads = sum(bool(re.fullmatch(r'inputs/resumes/[a-f0-9]{32}\.json', row['path']))
                                   for row in durable)
            stored_bytes = sum(row['size'] for row in durable)
        metadata_bytes = _json_bytes(value)
        if existing_uploads >= MAX_RESUME_UPLOADS:
            raise ValueError('This workspace has reached its limit of %s resume uploads.' % MAX_RESUME_UPLOADS)
        if stored_bytes + len(content_bytes) + len(metadata_bytes) > MAX_RESUME_STORAGE_BYTES:
            raise ValueError('This workspace has reached its 100 MiB resume storage limit.')
        # Opaque storage filenames are unrelated to user-supplied names.
        content_path = directory / (upload_id + extension)
        _write_private(content_path, content_bytes)
        try:
            from .resumes import import_resume
            store = Store(root / 'data/jobflow.sqlite3')
            try:
                saved = import_resume(root, store, filename, content_bytes,
                                      {'kind': 'onboarding', 'upload_id': upload_id})
                value['application_resume_id'] = saved['id']
            finally:
                store.close()
            _write_private(directory / (upload_id + '.json'), _json_bytes(value))
        except Exception:
            content_path.unlink()
            raise
    return value


def _merged_profile(root, previous, changes):
    if not isinstance(changes, dict) or set(changes) - set(PROFILE_FIELDS):
        raise ValueError('Profile changes contain unsupported fields.')
    profile = copy.deepcopy(previous)
    for key in ('name', 'headline'):
        if key in changes:
            profile[key] = _text(changes[key], key.capitalize(), 200 if key == 'name' else 2000,
                                 required=(key == 'name'))
    if 'skills' in changes:
        profile['skills'] = _string_list(changes['skills'], 'Skills', 100, 200)
    if 'resume_sources' in changes:
        profile['resume_sources'] = _string_list(changes['resume_sources'], 'Resume sources', 100, 2000)
    if 'evidence' in changes:
        values = changes['evidence']
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_EVIDENCE:
            raise ValueError('Add between 1 and %s reviewed evidence statements.' % MAX_EVIDENCE)
        old = {str(row.get('id')): row for row in previous.get('evidence', []) if isinstance(row, dict)}
        evidence = []
        seen = set()
        total = 0
        for index, item in enumerate(values):
            if isinstance(item, str):
                item = {'text': item}
            if not isinstance(item, dict):
                raise ValueError('Each evidence statement must be an object or text.')
            text = _text(item.get('text'), 'Evidence text', 10000, required=True)
            total += len(text)
            key = item.get('id') or 'evidence-' + hashlib.sha256(
                (str(index) + '\n' + text).encode('utf-8')).hexdigest()[:24]
            _text(key, 'Evidence ID', 200, required=True)
            if key in seen:
                raise ValueError('Evidence IDs must be unique.')
            seen.add(key)
            row = copy.deepcopy(old.get(key, {}))
            # Existing metadata survives editing. Unknown new fields cannot be
            # used to attach credentials or pretend to be parser instructions.
            allowed = {'id', 'text', 'tags', 'source_url', 'source_file_id', 'employer'}
            for field in item:
                if field in allowed or field in old.get(key, {}):
                    row[field] = copy.deepcopy(item[field])
            row.update(id=key, text=text)
            if 'tags' in row:
                row['tags'] = _string_list(row['tags'], 'Evidence tags', 100, 200)
            if 'source_url' in row:
                _text(row['source_url'], 'Evidence source', 4000)
            if 'employer' in row:
                _text(row['employer'], 'Evidence employer', 500)
            if row.get('source_file_id'):
                source_id = row['source_file_id']
                if not isinstance(source_id, str) or not re.fullmatch(r'[a-f0-9]{32}', source_id):
                    raise ValueError('The evidence upload reference is invalid.')
                source = _read_json(root / 'inputs/resumes' / (source_id + '.json'))
                if not source or source.get('id') != source_id:
                    raise ValueError('The evidence upload is not in this workspace.')
            evidence.append(row)
        if total > MAX_TEXT_CHARS:
            raise ValueError('The reviewed evidence is too large.')
        profile['evidence'] = evidence
    _text(profile.get('name'), 'Name', 200, required=True)
    if not profile.get('evidence'):
        raise ValueError('Review and save at least one evidence statement.')
    return profile


def _merged_preferences(previous, changes):
    if not isinstance(changes, dict):
        raise ValueError('Preferences must be an object.')
    if set(changes) - set(PREFERENCE_SECTIONS) - set(FLAT_PREFERENCES):
        raise ValueError('Preferences contain unsupported fields.')
    settings = copy.deepcopy(previous)
    for section in PREFERENCE_SECTIONS:
        if section in changes:
            values = changes[section]
            if not isinstance(values, dict) or set(values) - SECTION_FIELDS[section]:
                raise ValueError('Unsupported %s preferences.' % section)
            settings.setdefault(section, {}).update(copy.deepcopy(values))
    for field, (section, key) in FLAT_PREFERENCES.items():
        if field in changes:
            settings.setdefault(section, {})[key] = copy.deepcopy(changes[field])
    if ('timezone' in changes
            and changes['timezone'] != previous.get('schedule', {}).get('timezone')
            and 'timezone' not in changes.get('preparation', {})):
        settings.setdefault('preparation', {})['timezone'] = changes['timezone']
    search = settings['search']
    _string_list(search.get('preferred_locations'), 'Preferred locations', 100, 200)
    levels = _string_list(search.get('allowed_levels'), 'Allowed levels', 10, 50, required=True)
    if set(levels) - {'entry', 'mid', 'senior', 'staff', 'principal', 'management', 'unspecified'}:
        raise ValueError('Select supported seniority levels.')
    target = search.get('target_tc')
    if isinstance(target, bool) or not isinstance(target, (int, float)) or not math.isfinite(target) or not 0 <= target <= 100000000:
        raise ValueError('Target compensation must be a finite number from 0 to 100,000,000.')
    # The matching engine currently interprets its target as USD.
    if search.get('currency') != 'USD':
        raise ValueError('Matching currently supports a USD compensation target.')
    if search.get('location_mode') not in ('bay_area_remote', 'any', 'anywhere', 'us', 'all_us', 'custom'):
        raise ValueError('Select a supported location preference.')
    _integer(search.get('min_score'), 'Minimum match score', 0, 100)
    schedule = settings['schedule']
    _integer(schedule.get('hour'), 'Schedule hour', 0, 23)
    _integer(schedule.get('minute'), 'Schedule minute', 0, 59)
    days = schedule.get('days')
    if not isinstance(days, list) or not days or len(days) > 7:
        raise ValueError('Schedule days must be unique weekdays from 0 through 6.')
    for day in days:
        _integer(day, 'Schedule day', 0, 6)
    if len(set(days)) != len(days):
        raise ValueError('Schedule days must be unique weekdays from 0 through 6.')
    for section in ('schedule', 'preparation'):
        zone = _text(settings[section].get('timezone'), 'Timezone', 100, required=True)
        try:
            ZoneInfo(zone)
        except (ValueError, KeyError):
            raise ValueError('Select a valid IANA timezone.')
    _integer(settings['preparation'].get('daily_minutes'), 'Daily preparation time', 15, 480)
    _integer(settings['preparation'].get('horizon_days'), 'Preparation horizon', 1, 90)
    return settings


def save_onboarding(root, payload):
    """Save explicitly reviewed profile/preferences without rewriting history."""
    root = _root_path(root)
    if not isinstance(payload, dict) or set(payload) - {'profile', 'preferences', 'applicant'}:
        raise ValueError('Send profile, preferences, and applicant objects.')
    applicant = None
    if 'applicant' in payload:
        from .resumes import validate_applicant
        applicant = validate_applicant(payload['applicant'])
    with _workspace_lock(root):
        profile_path = root / 'config/profile.json'
        settings_path = root / 'config/settings.json'
        previous_profile = _read_json(profile_path)
        previous_settings = _read_json(settings_path)
        if not isinstance(previous_profile, dict) or not isinstance(previous_settings, dict):
            raise ValueError('Initialize this workspace before onboarding.')
        profile = _merged_profile(root, previous_profile, payload.get('profile', {}))
        settings = _merged_preferences(previous_settings, payload.get('preferences', {}))
        # Use the same core configuration checks as CLI execution before replacing
        # either live file. The temporary directory contains no database/history.
        with tempfile.TemporaryDirectory(prefix='jobflow-profile-') as temporary:
            validation_root = Path(temporary)
            _write_private(validation_root / 'config/profile.json', _json_bytes(profile))
            _write_private(validation_root / 'config/settings.json', _json_bytes(settings))
            load_config(validation_root)
        metadata_path = root / 'config/onboarding.json'
        metadata = _read_json(metadata_path, {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.update(completed=True, reviewed_at=utcnow())
        old_profile_bytes = profile_path.read_bytes()
        old_settings_bytes = settings_path.read_bytes()
        try:
            _write_private(profile_path, _json_bytes(profile))
            _write_private(settings_path, _json_bytes(settings))
        except Exception:
            _write_private(profile_path, old_profile_bytes)
            _write_private(settings_path, old_settings_bytes)
            raise
        _write_private(metadata_path, _json_bytes(metadata))
        if applicant is not None:
            from .resumes import perform_resume_action
            store = Store(root / 'data/jobflow.sqlite3')
            try:
                perform_resume_action(root, store, settings, profile,
                                      {'op': 'resume_profile', 'applicant': applicant})
            finally:
                store.close()
    return onboarding_state(root)

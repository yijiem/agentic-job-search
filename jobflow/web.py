"""Local HTTP adapter for Jobflow; private records stay in the selected workspace.

The browser is a client of the existing CLI, not a second persistence layer.
Only explicit POST actions write state, through ``python -m jobflow``.
"""
import gzip
import hashlib
import io
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import webbrowser
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit
from zoneinfo import ZoneInfo

from .tracking import STAGES, STATUSES, explicit_timestamp, integer_minutes
from .salary_references import reference_index, salary_reference
from .preparation_guides import build_role_guide
from .service_context import service_catalog
from .pdf_assets import PDF_ASSETS
from .company_logo_assets import COMPANY_LOGO_ASSETS
from .locations import location_catalog, location_reviews, office_locations

ASSET_DIRECTORY = Path(__file__).with_name('web_assets')
# Deliberately enumerate public files instead of exposing the asset directory.
STATIC_ASSETS = frozenset((
    'index.html', 'app.js', 'styles.css', 'explore.css', 'company-logos.js', 'theme.js', 'my-account.js', 'my-account.css',
    'trends.js', 'trends.css', 'maps.js', 'maps.css', 'batches.js', 'batches.css',
    'resumes.js', 'resumes.css', 'pdf-review.js', 'pdf-review.css',
    'resume-controls.js', 'resume-controls.css',
    'vendor/leaflet/leaflet.js', 'vendor/leaflet/leaflet.css',
    'vendor/leaflet/images/layers.png', 'vendor/leaflet/images/layers-2x.png',
    'vendor/leaflet/images/marker-icon.png', 'vendor/leaflet/images/marker-icon-2x.png',
    'vendor/leaflet/images/marker-shadow.png',
    'vendor/maplibre/maplibre-gl-csp.js', 'vendor/maplibre/maplibre-gl-csp-worker.js',
    'vendor/maplibre/maplibre-gl.css', 'vendor/maplibre/leaflet-maplibre-gl.js',
    'fonts/dm-serif-display-latin.woff2', 'fonts/nunito-latin.woff2',
    'art/jobflow-cosmos.webp',
)) | PDF_ASSETS | COMPANY_LOGO_ASSETS
MAX_REQUEST_BYTES = 256 * 1024
MAX_UPLOAD_REQUEST_BYTES = 7 * 1024 * 1024
MAX_BATCH = 100
IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')
JOB_FIELDS = ('id', 'company', 'title', 'location', 'url', 'source_id', 'score',
              'eligible', 'availability', 'first_seen', 'last_seen', 'posted_at',
              'remote', 'department', 'employment_type', 'level', 'compensation',
              'compensation_status', 'reasons', 'gaps', 'matched_skills')


def app_build(asset_directory=ASSET_DIRECTORY):
    """Fingerprint the served UI so an already-open tab can offer a reload."""
    digest = hashlib.sha256()
    for name in sorted(STATIC_ASSETS):
        path = Path(asset_directory) / name
        digest.update(name.encode('utf-8'))
        digest.update(path.read_bytes() if path.is_file() else b'')
    return digest.hexdigest()[:16]


class WebError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _read_records(root, job_mode="matching", minimum=40, job_id=None):
    """Read a consistent SQLite snapshot without creating or changing anything."""
    from .remote import workspace_id
    if workspace_id(root):
        from .remote_store import read_records
        return read_records(root, job_mode=job_mode, minimum=minimum, job_id=job_id)
    path = Path(root) / 'data/jobflow.sqlite3'
    records = defaultdict(list)
    if not path.exists():
        return records, [], 0
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    try:
        connection.execute('BEGIN')
        count = connection.execute("SELECT count(*) FROM records WHERE kind='jobs'").fetchone()[0]
        for kind, body in connection.execute("SELECT kind,body FROM records WHERE kind != 'jobs' ORDER BY created_at,id"):
            records[kind].append(json.loads(body))
        if job_mode == 'detail':
            query, params = "SELECT body FROM records WHERE kind='jobs' AND id=?", (job_id,)
        elif job_mode == 'ids':
            records['jobs'] = [{'id': row[0]} for row in connection.execute("SELECT id FROM records WHERE kind='jobs'")]
            query, params = None, ()
        else:
            query = """SELECT json_remove(body, '$.description') FROM records WHERE kind='jobs' AND
                       ((json_extract(body,'$.eligible')=1 AND json_extract(body,'$.score')>=?) OR
                        id IN (SELECT json_extract(body,'$.job_id') FROM records WHERE kind IN ('applications','interviews')))
                       ORDER BY created_at,id"""
            params = (minimum,)
        if query:
            try:
                records['jobs'] = [json.loads(row[0]) for row in connection.execute(query, params)]
            except sqlite3.OperationalError as error:
                if 'no such function' not in str(error):
                    raise
                # Older Python 3.9 builds may ship SQLite without JSON functions.
                tracked = {r.get('job_id') for r in records['applications'] + records['interviews']}
                for body, in connection.execute("SELECT body FROM records WHERE kind='jobs'"):
                    row = json.loads(body)
                    if (row.get('eligible') and row.get('score', 0) >= minimum) or row['id'] in tracked:
                        row.pop('description', None)
                        records['jobs'].append(row)
        runs = []
        for row in connection.execute('SELECT id,started_at,finished_at,status,details FROM runs ORDER BY id DESC LIMIT 30'):
            runs.append(dict(zip(('id', 'started_at', 'finished_at', 'status', 'details'), row)))
        for run in runs:
            try:
                run['details'] = json.loads(run['details'])
            except (TypeError, ValueError):
                pass
        return records, runs, count
    finally:
        connection.close()


def _settings(root):
    path = Path(root) / 'config/settings.json'
    return json.loads(path.read_text()) if path.exists() else {}


def _local_day(value, tz):
    if not value:
        return ''
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(tz).date().isoformat() if parsed.tzinfo else parsed.date().isoformat()
    except (TypeError, ValueError):
        return ''


def profile_summary(root):
    """Read the displayed background without exposing private import metadata."""
    from .resumes import _profile
    profile = _profile(root)
    result = {key: profile.get(key, '') if isinstance(profile.get(key, ''), str) else ''
              for key in ('name', 'headline')}
    result['skills'] = [value for value in profile.get('skills', []) if isinstance(value, str)] \
        if isinstance(profile.get('skills', []), list) else []
    result['evidence'] = []
    evidence = profile.get('evidence', [])
    for item in evidence if isinstance(evidence, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get('text'), str):
            continue
        row = {key: item[key] for key in ('id', 'text', 'employer')
               if isinstance(item.get(key), str)}
        source = item.get('source_url')
        if isinstance(source, str):
            try:
                parsed = urlsplit(source)
                if parsed.scheme in ('http', 'https') and parsed.hostname and not (parsed.username or parsed.password):
                    row['source_url'] = source
            except ValueError:
                pass
        row['tags'] = [value for value in item.get('tags', []) if isinstance(value, str)] \
            if isinstance(item.get('tags', []), list) else []
        result['evidence'].append(row)
    return {'profile': result}


def snapshot(root):
    """Public API projection; no credentials or raw profile/config are returned."""
    config = _settings(root)
    minimum = config.get('search', {}).get('min_score', 40)
    records, runs, total_jobs = _read_records(root, minimum=minimum)
    timezone_name = config.get('schedule', {}).get('timezone', 'America/Los_Angeles')
    tz = ZoneInfo(timezone_name)
    now = datetime.now(tz)
    minimum = config.get('search', {}).get('min_score', 40)
    tracked = {row.get('job_id') for row in records['applications'] + records['interviews']}
    jobs = [job for job in records['jobs'] if
            (job.get('eligible') and job.get('score', 0) >= minimum) or job['id'] in tracked]
    jobs.sort(key=lambda job: (-job.get('score', 0), job.get('company', ''), job.get('title', '')))
    matching_jobs = [job for job in jobs if job.get('eligible') and
                     job.get('score', 0) >= minimum and job.get('availability') != 'closed']
    compact = []
    salary_index = reference_index(records['research'])
    locations = location_catalog(records['research'])
    by_day = defaultdict(Counter)
    for job in jobs:
        row = {key: job[key] for key in JOB_FIELDS if key in job}
        row['first_seen_date'] = _local_day(job.get('first_seen'), tz)
        row['last_seen_date'] = _local_day(job.get('last_seen'), tz)
        row['salary_reference'] = salary_reference(job, salary_index)
        row['office_locations'] = office_locations(job, locations)
        compact.append(row)
        if row['first_seen_date'] and job.get('eligible') and job.get('score', 0) >= minimum:
            by_day[row['first_seen_date']][job.get('company', 'Unknown')] += 1
    history, cumulative = [], Counter()
    for day in sorted(by_day):
        cumulative.update(by_day[day])
        history.append({'date': day, 'companies': dict(cumulative), 'total': sum(cumulative.values())})
    result = {kind: records[kind] for kind in ('applications', 'interviews', 'tasks', 'contacts',
              'research', 'packets', 'plans', 'source_health', 'activities', 'coaching')}
    # Account observations have their own allowlisted projection. Do not also
    # expose the raw imported metadata through ordinary research cards.
    result['research'] = [r for r in records['research'] if r.get('kind') not in ('service_context', 'company_location', 'company_location_review')]
    result.update(jobs=compact, runs=runs, timezone=timezone_name, today=now.date().isoformat(),
                  app_build=app_build(), services=service_catalog(records['research']), locations=locations,
                  location_reviews=location_reviews(records['research'], locations,
                      [job.get('company', 'Unknown') for job in matching_jobs], jobs=matching_jobs),
                  generated_at=now.isoformat(), initialized=bool(config),
                  settings={key: config.get(key, {}) for key in ('search', 'preparation', 'daily_goals')},
                  totals={'stored_jobs': total_jobs, 'visible_jobs': len(jobs),
                          'matches': sum(1 for j in jobs if j.get('eligible') and
                                         j.get('score', 0) >= minimum and j.get('availability') != 'closed')},
                  history=history, history_kind='cumulative_first_discoveries',
                  plan=next((p for p in records['plans'] if p.get('id') == 'current'), None))
    return result


def job_detail(root, job_id):
    _identifier(job_id)
    records, unused, unused_count = _read_records(root, job_mode='detail', job_id=job_id)
    job = next((j for j in records['jobs'] if j['id'] == job_id), None)
    if not job:
        raise WebError('This role is no longer in the local workspace.', 404)
    company = job.get('company', '').casefold()
    evidence = [r for r in records['research'] if r.get('kind') not in ('service_context', 'company_location', 'company_location_review')]
    linked_locations = office_locations(job, location_catalog(records['research']))
    job = {**job, 'salary_reference': salary_reference(job, reference_index(records['research'])),
           'office_locations': linked_locations}
    return {'job': job, 'locations': linked_locations,
            'preparation_guide': build_role_guide(job, _settings(root), evidence),
            'application': next((a for a in records['applications'] if a.get('job_id') == job_id), None),
            'packet': next((p for p in records['packets'] if p.get('job_id') == job_id), None),
            'research': [r for r in evidence if r.get('job_id') == job_id or
                         (r.get('company', '').casefold() == company and not r.get('job_id'))],
            'contacts': [c for c in records['contacts'] if c.get('company', '').casefold() == company],
            'interviews': [i for i in records['interviews'] if i.get('job_id') == job_id],
            'tasks': [t for t in records['tasks'] if t.get('job_id') == job_id or
                      (t.get('company', '').casefold() == company and not t.get('job_id'))]}


def _identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise WebError('Choose a valid record ID.')
    return value


def _text(payload, key, maximum=10000):
    value = payload[key]
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
        raise WebError('%s must be text of at most %s characters.' % (key, maximum))
    return value


def _run_cli(root, arguments, full_output=False):
    prefix = [sys.executable, '-m', 'jobflow', '--root', str(Path(root).resolve())]
    environment = None
    from .remote import workspace_id, lease_environment
    remote_workspace = workspace_id(root)
    if remote_workspace or (Path(root) / 'config/hosted.json').is_file():
        prefix.append('--hosted')
        environment = dict(os.environ)
        environment.pop('OPENAI_API_KEY', None)
        if remote_workspace:
            prefix.append('--hosted-interactive')
            environment = lease_environment(root)
            environment.pop('OPENAI_API_KEY', None)
    result = subprocess.run(prefix + arguments,
                            cwd=str(Path(__file__).resolve().parents[1]), capture_output=True,
                            text=True, timeout=180, check=False, env=environment)
    if 'Another workflow process is running' in result.stderr:
        raise WebError('A search or another update is running. Wait for it to finish and try again.', 409)
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or 'The local workflow could not save this change.'
        # CLI validation errors are actionable, but do not expose traceback paths.
        lines = [line for line in message.splitlines() if line.startswith('Error:')]
        raise WebError(lines[-1][6:] if lines else 'The local workflow could not save this change. Check the server terminal.', 400)
    return result.stdout if full_output else result.stdout[-4000:]


def browser_worker_archive():
    """Package only reviewed public extension files, never workspace data."""
    directory = Path(__file__).with_name('browser_worker')
    if not directory.is_dir():
        directory = Path(__file__).resolve().parents[1] / 'browser_worker'
    names = ('manifest.json', 'background.js', 'content.js', 'popup.html',
             'popup.js', 'popup.css', 'README.md')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            target = directory / name
            if target.is_symlink() or not target.is_file():
                if name == 'README.md':
                    continue
                raise WebError('The browser worker package is not available in this installation.', 503)
            archive.writestr('jobflow-browser-worker/' + name, target.read_bytes())
    return buffer.getvalue()


def validate_action_size(payload, length):
    if (isinstance(payload, dict) and payload.get('action') == 'batch' and
            payload.get('op') in ('pdf_review_create', 'pdf_review_update') and length <= 1024 * 1024):
        return
    if length > MAX_REQUEST_BYTES and not (
            isinstance(payload, dict) and payload.get('action') == 'batch' and
            payload.get('op') in ('upload_resume', 'pdf_review_save')):
        raise WebError('The request is too large.', 413)


def _batch_action(root, payload):
    from .application_batches import batch_snapshot
    if payload.get('workspace_id') is not None and payload['workspace_id'] != batch_snapshot(root)['workspace_id']:
        raise WebError('The connected workspace changed. Reload Jobflow before continuing.', 409)
    op = payload.get('op', '')
    if isinstance(op, str) and op.startswith('codex_'):
        if op not in ('codex_prepare', 'codex_discard'):
            raise WebError('Codex run operations are available only through the local Jobflow CLI.', 403)
        from .remote import workspace_id
        if workspace_id(root) or (Path(root) / 'config/hosted.json').is_file():
            raise WebError('Open your local Jobflow workspace to run applications with Codex on this computer.', 409)
    result = _json_action(root, payload, 'batch')
    if op == 'codex_prepare':
        from .codex_handoff import codex_handoff
        result.update(codex_handoff(root, result['batch']['id'], result['run']['id']))
    return result


def _json_action(root, payload, command):
    """Send bounded structured input to a CLI command under its workspace lock."""
    directory = Path(root) / 'inputs/web'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json', prefix=command + '-',
                                     dir=str(directory), delete=False) as stream:
        path = Path(stream.name)
        json.dump(payload, stream)
    try:
        return json.loads(_run_cli(root, [command, '--request', str(path)], full_output=True))
    finally:
        path.unlink(missing_ok=True)


def _import_rows(root, kind, rows):
    directory = Path(root) / 'inputs/web'
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json', prefix='update-',
                                     dir=str(directory), delete=False) as stream:
        path = Path(stream.name)
        json.dump(rows, stream)
    try:
        return _run_cli(root, ['import', str(path), '--kind', kind])
    finally:
        path.unlink(missing_ok=True)


def perform_action(root, payload):
    """Validate a browser command and dispatch only allowlisted CLI operations."""
    if not isinstance(payload, dict):
        raise WebError('The request must contain a JSON object.')
    action = payload.get('action')
    if action in ('batch', 'agent'):
        if not (Path(root) / 'config/settings.json').exists() or not (Path(root) / 'config/profile.json').exists():
            raise WebError('Initialize this workspace before creating application batches.', 409)
        return _batch_action(root, payload) if action == 'batch' else _json_action(root, payload, 'agent')
    if action not in ('application', 'task', 'interview', 'personalize', 'prep'):
        raise WebError('Unknown action.')
    if not (Path(root) / 'config/settings.json').exists() or not (Path(root) / 'config/profile.json').exists():
        raise WebError('Initialize this workspace and configure your profile before saving changes.', 409)
    records, unused, unused_count = _read_records(root, job_mode='ids')
    jobs = {j['id']: j for j in records['jobs']}
    output, count, skipped = '', 0, 0
    if action in ('application', 'personalize'):
        ids = payload.get('job_ids', [payload.get('job_id')])
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_BATCH:
            raise WebError('Choose between 1 and %s roles.' % MAX_BATCH)
        ids = list(dict.fromkeys(_identifier(value) for value in ids))
        if any(value not in jobs for value in ids):
            raise WebError('A selected role does not exist in this workspace.')
        if action == 'personalize':
            if len(ids) > 10:
                raise WebError('Prepare at most 10 role packets at a time.')
            for value in ids:
                output = _run_cli(root, ['personalize', '--job', value])
                count += 1
        else:
            stage = payload.get('stage')
            if stage not in STAGES:
                raise WebError('Choose a valid application stage.')
            fields = {key: _text(payload, key) for key in ('notes', 'next_action', 'response') if key in payload}
            if 'follow_up_date' in payload:
                value = _text(payload, 'follow_up_date', 10)
                if value:
                    try:
                        date.fromisoformat(value)
                    except ValueError:
                        raise WebError('Follow-up date must use YYYY-MM-DD.')
                fields['follow_up_date'] = value
            if 'applied_at' in payload:
                fields['applied_at'] = explicit_timestamp(_text(payload, 'applied_at', 50), 'applied_at')
            applications = {a['job_id']: a for a in records['applications']}
            rows = []
            now = datetime.now(timezone.utc).isoformat()
            for value in ids:
                previous = applications.get(value, {})
                # Queueing must not turn an existing application back into a saved role.
                # A bulk submission record must not demote later interview stages either.
                if ((stage == 'saved' and previous.get('stage') not in (None, 'saved')) or
                    (len(ids) > 1 and stage == 'applied' and previous.get('stage') not in (None, 'saved', 'applied'))):
                    skipped += 1
                    continue
                row = {'id': value, 'job_id': value, 'stage': stage, 'updated_at': now, **fields}
                if stage == 'applied' and previous.get('stage') in (None, 'saved') and not previous.get('applied_at') and 'applied_at' not in fields:
                    row['applied_at'] = now
                rows.append(row)
            if rows:
                output = _import_rows(root, 'applications', rows)
            count = len(rows)
    elif action == 'task':
        identifier = _identifier(payload.get('id'))
        args = ['task', identifier]
        if 'status' in payload:
            if payload['status'] not in STATUSES:
                raise WebError('Choose a valid task status.')
            args += ['--status', payload['status']]
        for key, flag in (('due_date', '--date'), ('date', '--date'), ('title', '--title'),
                          ('notes', '--notes'), ('category', '--category'), ('source_url', '--source-url')):
            if key in payload:
                args += [flag + '=' + _text(payload, key)]
        for key, flag, minimum in (('minutes', '--minutes', 1), ('actual_minutes', '--actual-minutes', 0)):
            if key in payload:
                args += [flag, str(integer_minutes(payload[key], key, minimum, 1440))]
        if len(args) == 2:
            raise WebError('Choose a task change to save.')
        output = _run_cli(root, args)
        count = 1
    elif action == 'interview':
        identifier = _identifier(payload.get('job_id'))
        if identifier not in jobs:
            raise WebError('Choose a role from this workspace.')
        starts_at = explicit_timestamp(_text(payload, 'starts_at', 50), 'starts_at')
        interview_round = _text(payload, 'round', 200)
        if not interview_round.strip():
            raise WebError('Enter an interview round.')
        args = ['interview', '--job', identifier, '--at', starts_at, '--round=' + interview_round]
        if payload.get('id'):
            args += ['--id', _identifier(payload['id'])]
        if 'notes' in payload:
            args += ['--notes=' + _text(payload, 'notes')]
        output = _run_cli(root, args)
        count = 1
    else:
        output = _run_cli(root, ['prep'])
    return {'ok': True, 'updated': count, 'skipped': skipped, 'message': output.strip() or 'Saved.'}


class JobflowServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, root, asset_directory=ASSET_DIRECTORY):
        if address[0] not in ('127.0.0.1', 'localhost'):
            raise ValueError('The private workspace server only binds to 127.0.0.1 or localhost.')
        self.root = Path(root).resolve()
        self.asset_directory = Path(asset_directory).resolve()
        self.csrf_token = secrets.token_urlsafe(32)
        self.action_lock = threading.Lock()
        super().__init__(address, JobflowHandler)


class JobflowHandler(BaseHTTPRequestHandler):
    server_version = 'Jobflow/1.0'

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        # Do not print private URLs, request bodies, or CSRF tokens in access logs.
        pass

    def _allowed_host(self):
        port = self.server.server_address[1]
        allowed = {'127.0.0.1:%s' % port, 'localhost:%s' % port}
        host = self.headers.get('Host', '')
        if host not in allowed:
            raise WebError('This private app is available only from its localhost address.', 403)
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + host:
            raise WebError('Cross-origin requests are not allowed.', 403)
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise WebError('Cross-site requests are not allowed.', 403)

    def _respond(self, status, body, content_type='application/json; charset=utf-8', extra_headers=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        encodings = self.headers.get('Accept-Encoding', '').lower().split(',')
        gzip_accepted = False
        for encoding in encodings:
            parts = [part.strip() for part in encoding.split(';')]
            if parts[0] == 'gzip':
                try:
                    quality = next((float(part[2:]) for part in parts[1:] if part.startswith('q=')), 1.0)
                except ValueError:
                    quality = 0
                gzip_accepted = quality > 0
        compressed = gzip_accepted and content_type.startswith('application/json') and len(body) > 1024
        if compressed:
            body = gzip.compress(body, compresslevel=5)
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header('Vary', 'Accept-Encoding')
        if compressed:
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; worker-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://tile.openstreetmap.org https://tiles.openfreemap.org; font-src 'self' blob:; connect-src 'self' https://tiles.openfreemap.org; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, error):
        self._respond(error.status if isinstance(error, WebError) else 400, {'error': str(error)})

    def _resume(self, root, identifier):
        from .application_batches import resume_content
        metadata, content = resume_content(root, _identifier(identifier))
        self._respond(200, content, metadata['mime'], {
            'Content-Disposition': "inline; filename*=UTF-8''" + quote(metadata['filename'], safe='')})

    def do_GET(self):
        try:
            self._allowed_host()
            path = unquote(urlsplit(self.path).path)
            if path == '/api/state':
                self._respond(200, {**snapshot(self.server.root), 'app_build': app_build(self.server.asset_directory),
                                    'csrf_token': self.server.csrf_token})
            elif path == '/api/version':
                self._respond(200, {'app_build': app_build(self.server.asset_directory)})
            elif path == '/api/profile':
                self._respond(200, profile_summary(self.server.root))
            elif path == '/api/batches':
                from .application_batches import batch_snapshot
                local_codex = not (Path(self.server.root) / 'config/hosted.json').is_file()
                self._respond(200, {**batch_snapshot(self.server.root), 'csrf_token': self.server.csrf_token,
                                    'codex': {'available': local_codex,
                                              'reason': '' if local_codex else 'Open your local Jobflow workspace to run applications with Codex on this computer.'}})
            elif path == '/api/resumes':
                from .resumes import resume_library
                self._respond(200, {**resume_library(self.server.root), 'csrf_token': self.server.csrf_token})
            elif path == '/api/resume-reviews':
                from .resume_reviews import review_list
                resume_id = parse_qs(urlsplit(self.path).query).get('resume_id', [None])[0]
                self._respond(200, review_list(self.server.root, resume_id))
            elif path.startswith('/api/resume-reviews/'):
                from .resume_reviews import review_detail
                self._respond(200, review_detail(self.server.root, _identifier(path[len('/api/resume-reviews/'):])) )
            elif path.startswith('/api/resumes/'):
                from .resumes import resume_detail
                self._respond(200, resume_detail(self.server.root, _identifier(path[len('/api/resumes/'):])) )
            elif path.startswith('/api/resume-drafts/'):
                from .resumes import resume_draft_detail
                self._respond(200, resume_draft_detail(self.server.root, _identifier(path[len('/api/resume-drafts/'):])) )
            elif path.startswith('/api/batches/'):
                from .application_batches import batch_detail
                batch = batch_detail(self.server.root, _identifier(path[len('/api/batches/'):]))
                if batch is None:
                    raise WebError('Application batch not found.', 404)
                self._respond(200, batch)
            elif path.startswith('/api/application-resumes/'):
                self._resume(self.server.root, path[len('/api/application-resumes/'):])
            elif path == '/browser-worker.zip':
                raise WebError('The browser worker is retired. Open Application batches and choose Open in Codex.', 410)
            elif path.startswith('/api/jobs/'):
                self._respond(200, job_detail(self.server.root, path[len('/api/jobs/'):]))
            elif path.startswith('/api/'):
                raise WebError('Unknown API route.', 404)
            else:
                relative = 'index.html' if path == '/' else path.lstrip('/')
                if relative not in STATIC_ASSETS:
                    raise WebError('File not found.', 404)
                target = (self.server.asset_directory / relative).resolve()
                if not target.is_relative_to(self.server.asset_directory) or not target.is_file():
                    raise WebError('File not found.', 404)
                mime = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
                body = target.read_bytes()
                if relative == 'index.html':
                    body = body.replace(b'__JOBFLOW_BUILD__', app_build(self.server.asset_directory).encode('ascii'))
                self._respond(200, body, mime + ('; charset=utf-8' if mime.startswith('text/') or mime in ('application/javascript', 'application/json') else ''))
        except (WebError, ValueError, KeyError) as error:
            self._error(error)
        except (OSError, sqlite3.Error):
            self._respond(500, {'error': 'The local workspace could not be read. Check the server terminal.'})

    def do_POST(self):
        try:
            self._allowed_host()
            if urlsplit(self.path).path != '/api/action':
                raise WebError('Unknown API route.', 404)
            if not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), self.server.csrf_token):
                raise WebError('Reload the app before saving changes.', 403)
            if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                raise WebError('Send changes as JSON.', 415)
            if self.headers.get('Transfer-Encoding'):
                raise WebError('Chunked requests are not supported.', 400)
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise WebError('Invalid request size.')
            if not 0 < length <= MAX_UPLOAD_REQUEST_BYTES:
                raise WebError('The request is too large or empty.', 413)
            payload = json.loads(self.rfile.read(length))
            validate_action_size(payload, length)
            if not self.server.action_lock.acquire(blocking=False):
                raise WebError('Another update is being saved. Please try again shortly.', 409)
            try:
                result = perform_action(self.server.root, payload)
            finally:
                self.server.action_lock.release()
            self._respond(200, result)
        except (WebError, ValueError, KeyError, TypeError) as error:
            self._error(error)
        except subprocess.TimeoutExpired:
            self._respond(504, {'error': 'The local workflow took too long. Reload to check its state before retrying.'})
        except (OSError, sqlite3.Error):
            self._respond(500, {'error': 'The local workflow could not save this change. Check the server terminal.'})


def serve(root, host='127.0.0.1', port=8765, open_browser=False):
    """Serve the private app until interrupted; never exposes the workspace tree."""
    server = JobflowServer((host, port), root)
    url = 'http://127.0.0.1:%s' % server.server_address[1]
    print('Jobflow is running at ' + url, flush=True)
    print('Private workspace: ' + str(Path(root).resolve()), flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped Jobflow.', flush=True)
    finally:
        server.server_close()

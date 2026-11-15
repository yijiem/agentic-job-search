import base64
import gzip
import io
import json
import tempfile
import threading
import unittest
import urllib.error
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from jobflow.cloud import CloudServer, validate_origin
from jobflow.cloud_auth import (AuthError, AuthRateLimitError, EmailSendCooldown,
                               RateLimiter, SupabaseEmailAuth)
from jobflow.onboarding import initialize_workspace, save_onboarding
from jobflow.storage import Store


class FakeEmailAuth:
    def __init__(self):
        self.sent = []
        self.verified = []

    def start(self, email):
        self.sent.append(email)

    def verify(self, email, token):
        self.verified.append((email, token))
        if token != '123456':
            raise AuthError('Provider rejected code; private detail must not be exposed')
        return {'id': 'verified:' + email, 'email': email}


class CloudHTTPTests(unittest.TestCase):
    origin = 'https://jobs.example.test'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = FakeEmailAuth()
        self.server = CloudServer(('127.0.0.1', 0), self.root / 'cloud', self.origin, auth=self.auth)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, path, payload=None, cookie=None, csrf=None, headers=None):
        request_headers = {'Host': 'jobs.example.test'}
        if method == 'POST':
            request_headers.update({'Origin': self.origin, 'Content-Type': 'application/json'})
        if cookie:
            request_headers['Cookie'] = cookie
        if csrf:
            request_headers['X-CSRF-Token'] = csrf
        for key, value in (headers or {}).items():
            if value is None:
                request_headers.pop(key, None)
            else:
                request_headers[key] = value
        body = json.dumps(payload).encode() if payload is not None else (b'{}' if method == 'POST' else None)
        connection = HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=10)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        response_headers = dict(response.getheaders())
        if response_headers.get('Content-Encoding') == 'gzip':
            data = gzip.decompress(data)
        parsed = json.loads(data) if response_headers.get('Content-Type', '').startswith('application/json') else data
        status = response.status
        connection.close()
        return status, response_headers, parsed

    def login(self, email, cookie=None):
        status, headers, data = self.request('POST', '/api/auth/verify',
            {'email': email, 'token': '123456'}, cookie=cookie)
        self.assertEqual(status, 200, data)
        return headers['Set-Cookie'].split(';', 1)[0], data['csrf_token'], data

    def seed(self, email, label):
        account = self.server.accounts.invite(email)
        root = self.server.accounts.workspace(account)
        initialize_workspace(root, sources=[])
        save_onboarding(root, {'profile': {'name': label, 'headline': 'Software engineer', 'skills': ['Python'],
            'evidence': [{'id': 'evidence', 'text': label + ' built an actual system.', 'tags': ['Python']}]}})
        store = Store(root / 'data/jobflow.sqlite3')
        job = {'id': 'shared-id', 'company': label + ' company', 'title': 'Senior Software Engineer',
               'location': 'San Francisco', 'url': 'https://example.com/jobs/shared',
               'description': label + ' private imported job description', 'score': 90, 'eligible': True,
               'availability': 'open', 'first_seen': '2026-11-01T01:00:00+00:00',
               'last_seen': '2026-11-01T01:00:00+00:00', 'matched_skills': ['Python'], 'reasons': [], 'gaps': [],
               'compensation': {'tc_status': 'unknown'}}
        store.put('jobs', job['id'], job)
        store.put('applications', 'shared-id', {'id': 'shared-id', 'job_id': 'shared-id', 'stage': 'saved',
            'notes': label + ' confidential notes', 'custom': {'preserve': label}})
        store.close()
        return account, root

    def test_unauthenticated_private_apis_denied_and_home_redirects(self):
        for name in ('styles.css', 'theme.js', 'fonts/nunito-latin.woff2', 'fonts/dm-serif-display-latin.woff2'):
            with self.subTest(public_asset=name):
                status, headers, body = self.request('GET', '/' + name + '?v=build')
                self.assertEqual(status, 200)
                self.assertEqual(body, (self.server.asset_directory / name).read_bytes())
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                if name.endswith('.woff2'):
                    self.assertEqual(headers['Content-Type'], 'font/woff2')
        for path in ('/api/state', '/api/onboarding', '/api/profile', '/api/jobs/shared-id', '/api/version', '/api/background', '/app.js',
                     '/my-account.js', '/my-account.css',
                     '/fonts/private.woff2', '/fonts/../config/profile.json',
                     '/api/batches', '/api/resumes', '/api/resumes/unknown',
                     '/api/resume-reviews', '/api/resume-reviews/unknown', '/pdf-review.js',
                     '/vendor/pdfjs/pdf.mjs', '/vendor/pdfjs/pdf.worker.mjs',
                     '/resumes.js', '/resumes.css', '/api/application-resumes/unknown', '/browser-worker.zip',
                     '/trends.js', '/maps.js', '/trends.css', '/maps.css', '/vendor/leaflet/leaflet.js',
                     '/vendor/maplibre/maplibre-gl-csp.js',
                     '/vendor/maplibre/maplibre-gl-csp-worker.js',
                     '/vendor/maplibre/maplibre-gl.css', '/vendor/maplibre/leaflet-maplibre-gl.js'):
            with self.subTest(path=path):
                status, unused, body = self.request('GET', path)
                self.assertEqual(status, 401)
                self.assertNotIn('profile', body)
        status, headers, unused = self.request('GET', '/')
        self.assertEqual(status, 303)
        self.assertEqual(headers['Location'], '/account')
        self.assertEqual(self.request('GET', '/api/account')[2], {'authenticated': False})

    def test_lightweight_polling_does_not_hydrate_private_workspace(self):
        self.seed('owner@example.com', 'Owner')
        cookie, unused, data = self.login('owner@example.com')
        with patch('jobflow.cloud.CloudHandler._root', side_effect=AssertionError('poll hydrated workspace')):
            status, unused, data = self.request('GET', '/api/background', cookie=cookie)
            self.assertEqual(status, 200)
            self.assertIn('background_job', data)
            status, unused, data = self.request('GET', '/api/version', cookie=cookie)
            self.assertEqual(status, 200)
            self.assertIn('app_build', data)
        self.assertEqual(self.request('POST', '/api/action', {'action': 'prep'})[0], 401)

    def test_profile_api_uses_signed_in_workspace_and_filters_private_metadata(self):
        unused, root_one = self.seed('one@example.com', 'One')
        self.seed('two@example.com', 'Two')
        profile_path = root_one / 'config/profile.json'
        profile = json.loads(profile_path.read_text())
        profile['private_note'] = 'Private internal note'
        profile['evidence'][0]['private_note'] = 'Private evidence metadata'
        profile['resume_sources'] = ['/private/one/resume.txt']
        profile_path.write_text(json.dumps(profile))
        before = profile_path.read_bytes()
        cookie_one, unused, unused = self.login('one@example.com')
        cookie_two, unused, unused = self.login('two@example.com')
        status, headers, first = self.request('GET', '/api/profile', cookie=cookie_one)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(first['profile']['name'], 'One')
        self.assertEqual(first['profile']['skills'], ['Python'])
        self.assertEqual(first['profile']['evidence'][0]['text'], 'One built an actual system.')
        self.assertNotIn('Private', json.dumps(first))
        self.assertNotIn('/private/', json.dumps(first))
        second = self.request('GET', '/api/profile?account=one@example.com', cookie=cookie_two)[2]
        self.assertEqual(second['profile']['name'], 'Two')
        self.assertNotIn('One', json.dumps(second))
        self.assertEqual(profile_path.read_bytes(), before)

    def test_application_batches_and_resume_files_are_account_isolated(self):
        self.seed('one@example.com', 'One')
        self.seed('two@example.com', 'Two')
        cookie_one, csrf_one, unused = self.login('one@example.com')
        cookie_two, csrf_two, unused = self.login('two@example.com')
        status, unused, first = self.request('GET', '/api/batches', cookie=cookie_one)
        self.assertEqual(status, 200)
        status, unused, second = self.request('GET', '/api/batches', cookie=cookie_two)
        self.assertEqual(status, 200)
        self.assertNotEqual(first['workspace_id'], second['workspace_id'])
        status, unused, upload = self.request('POST', '/api/action', {
            'action': 'batch', 'op': 'upload_resume', 'filename': 'private.txt',
            'content_base64': base64.b64encode(b'One private resume').decode('ascii'),
        }, cookie=cookie_one, csrf=csrf_one)
        self.assertEqual(status, 200, upload)
        resume_id = upload['resume']['id']
        status, unused, batch = self.request('POST', '/api/action', {
            'action': 'batch', 'op': 'create', 'name': 'One private batch',
            'job_ids': ['shared-id'], 'resume_id': resume_id,
            'applicant': {'first_name': 'One', 'last_name': 'Candidate', 'email': 'one@example.com'},
        }, cookie=cookie_one, csrf=csrf_one)
        self.assertEqual(status, 200, batch)
        second = self.request('GET', '/api/batches', cookie=cookie_two)[2]
        self.assertEqual(second['batches'], [])
        self.assertEqual(second['resumes'], [])
        self.assertNotIn('One private', json.dumps(second))
        self.assertNotEqual(self.request('GET', '/api/batches/' + batch['batch']['id'], cookie=cookie_two)[0], 200)
        self.assertNotEqual(self.request('GET', '/api/application-resumes/' + resume_id, cookie=cookie_two)[0], 200)
        self.assertNotEqual(self.request('GET', '/api/resumes/' + resume_id, cookie=cookie_two)[0], 200)
        self.assertEqual(self.request('GET', '/api/resumes', cookie=cookie_two)[2]['resumes'], [])
        self.assertEqual(self.request('GET', '/api/resumes/' + resume_id, cookie=cookie_one)[0], 200)
        self.assertEqual(self.request('GET', '/api/application-resumes/' + resume_id, cookie=cookie_one)[2], b'One private resume')
        status, unused, result = self.request('POST', '/api/action', {
            'action': 'batch', 'op': 'claim', 'worker_id': 'worker-one',
            'workspace_id': first['workspace_id'],
        }, cookie=cookie_two, csrf=csrf_two)
        self.assertEqual(status, 409, result)

    def test_authenticated_dashboard_assets_share_allowlist_and_preserve_binary_types(self):
        self.server.accounts.invite('owner@example.com')
        cookie, unused_csrf, unused_data = self.login('owner@example.com')
        assets = self.root / 'public'
        assets.mkdir()
        self.server.asset_directory = assets.resolve()
        public = {
            'trends.js': (b'window.JobflowTrends = {};', None),
            'resume-controls.js': (b'window.JobflowResumeControls = {};', None),
            'resume-controls.css': (b'.resume-picker {}', 'text/css; charset=utf-8'),
            'trends.css': (b'.jf-trend {}', 'text/css; charset=utf-8'),
            'maps.js': (b'window.JobflowMaps = {};', None),
            'maps.css': (b'.jf-map {}', 'text/css; charset=utf-8'),
            'vendor/leaflet/leaflet.js': (b'window.L = {};', None),
            'vendor/leaflet/leaflet.css': (b'.leaflet-container {}', 'text/css; charset=utf-8'),
            'vendor/maplibre/maplibre-gl-csp.js': (b'window.maplibregl = {};', None),
            'vendor/maplibre/maplibre-gl-csp-worker.js': (b'self.onmessage = function() {};', None),
            'vendor/maplibre/maplibre-gl.css': (b'.maplibregl-map {}', 'text/css; charset=utf-8'),
            'vendor/maplibre/leaflet-maplibre-gl.js': (b'window.L.maplibreGL = function() {};', None),
            'vendor/leaflet/images/marker-icon.png': (b'\x89PNG\r\n', 'image/png'),
            'fonts/nunito-latin.woff2': (b'wOF2binary', 'font/woff2'),
            'art/jobflow-cosmos.webp': (b'RIFFbinaryWEBP', 'image/webp'),
        }
        for name, (payload, mime) in public.items():
            target = assets / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            with self.subTest(name=name):
                status, headers, body = self.request('GET', '/' + name + '?v=build', cookie=cookie)
                self.assertEqual(status, 200)
                self.assertEqual(body, payload)
                if mime:
                    self.assertEqual(headers['Content-Type'], mime)
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                policy = {part.strip().split(' ', 1)[0]: part.strip().split(' ', 1)[1]
                          for part in headers['Content-Security-Policy'].split(';') if part.strip()}
                self.assertEqual(policy['img-src'].split(), ["'self'", 'data:', 'blob:',
                                 'https://tile.openstreetmap.org', 'https://tiles.openfreemap.org'])
                self.assertEqual(policy['connect-src'].split(), ["'self'", 'https://tiles.openfreemap.org'])
                self.assertEqual(policy['script-src'], "'self'")
                self.assertEqual(policy['worker-src'], "'self'")
                self.assertEqual(policy['default-src'], "'self'")
                self.assertEqual(policy['font-src'], "'self' blob:")
                self.assertEqual(policy['base-uri'], "'none'")
                self.assertEqual(policy['frame-ancestors'], "'none'")
                self.assertEqual(policy['form-action'], "'self'")
                if name.endswith('.js'):
                    self.assertIn(headers['Content-Type'],
                                  ('text/javascript; charset=utf-8', 'application/javascript; charset=utf-8'))
                self.assertEqual(headers['Referrer-Policy'], 'strict-origin-when-cross-origin')
        for name in ('vendor/leaflet/private.json', 'vendor/leaflet/leaflet.js.map', 'config/profile.json',
                     'vendor/maplibre/private.json', 'vendor/maplibre/maplibre-gl-csp.js.map',
                     'vendor/maplibre/unlisted-worker.js'):
            target = assets / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'not-public')
            self.assertEqual(self.request('GET', '/' + name, cookie=cookie)[0], 404)
        self.assertEqual(self.request('GET', '/vendor/leaflet/%2e%2e/leaflet/leaflet.js', cookie=cookie)[0], 404)
        self.assertEqual(self.request('GET', '/vendor/maplibre/%2e%2e/maplibre/maplibre-gl-csp.js', cookie=cookie)[0], 404)
        (assets / 'maps.js').unlink()
        private = self.root / 'private.js'
        private.write_bytes(b'not-public')
        (assets / 'maps.js').symlink_to(private)
        self.assertEqual(self.request('GET', '/maps.js', cookie=cookie)[0], 404)

    def test_host_origin_and_fetch_site_are_strict(self):
        self.server.accounts.invite('owner@example.com')
        for headers in ({'Host': 'attacker.example'}, {'Origin': 'https://attacker.example'}, {'Origin': None},
                        {'Sec-Fetch-Site': 'cross-site'},
                        {'Host': 'attacker.example', 'X-Forwarded-Host': 'jobs.example.test'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request('POST', '/api/auth/start', {'email': 'owner@example.com'},
                                              headers=headers)[0], 403)
        self.assertEqual(self.auth.sent, [])
        self.assertEqual(self.request('GET', '/api/account', headers={'Host': 'attacker.example'})[0], 403)
        self.assertEqual(self.request('GET', '/healthz', headers={'Host': 'internal-probe'})[2], {'status': 'ok'})

    def test_unknown_email_gets_generic_response_without_provider_send_or_binding(self):
        self.server.accounts.invite('owner@example.com')
        unknown = self.request('POST', '/api/auth/start', {'email': 'unknown@example.com'})
        known = self.request('POST', '/api/auth/start', {'email': 'owner@example.com'})
        self.assertEqual(unknown[0], known[0])
        self.assertEqual(unknown[2], known[2])
        self.assertEqual(self.auth.sent, ['owner@example.com'])
        self.assertEqual(self.request('POST', '/api/auth/verify', {'email': 'unknown@example.com', 'token': '123456'})[0], 401)
        self.assertEqual(self.auth.verified, [])
        self.assertEqual(len(self.server.accounts.list_accounts()), 1)
        self.assertEqual(self.server.accounts.list_accounts()[0]['status'], 'invited')

    def test_code_send_limit_is_429_without_blocking_existing_code_verification(self):
        self.server.accounts.invite('owner@example.com')
        with patch.object(self.auth, 'start', side_effect=AuthRateLimitError()) as send:
            known = self.request('POST', '/api/auth/start', {'email': 'owner@example.com'})
            unknown = self.request('POST', '/api/auth/start', {'email': 'unknown@example.com'})
        send.assert_called_once_with('owner@example.com')
        for status, headers, body in (known, unknown):
            self.assertEqual(status, 429)
            self.assertEqual(body, {'error': AuthRateLimitError.message})
            self.assertNotIn('__Host-jobflow=', headers.get('Set-Cookie', ''))
        self.assertEqual(known[1]['Set-Cookie'], unknown[1]['Set-Cookie'])
        self.assertEqual(self.server.accounts.lookup_email('owner@example.com')['status'], 'invited')
        # A message already received still works; the send quota is independent.
        self.assertEqual(self.request('POST', '/api/auth/verify',
                         {'email': 'owner@example.com', 'token': '123456'})[0], 200)

    def test_cookie_flags_csrf_rotation_and_logout_invalidate_session(self):
        self.server.accounts.invite('owner@example.com')
        status, headers, body = self.request('POST', '/api/auth/verify', {'email': 'owner@example.com', 'token': '123456'})
        self.assertEqual(status, 200)
        set_cookie = headers['Set-Cookie']
        for flag in ('__Host-jobflow=', 'Path=/', 'HttpOnly', 'SameSite=Strict', 'Secure', 'Max-Age=43200'):
            self.assertIn(flag, set_cookie)
        self.assertNotIn('Domain=', set_cookie)
        cookie = set_cookie.split(';', 1)[0]
        csrf = body['csrf_token']
        self.assertEqual(self.request('POST', '/api/auth/logout', {}, cookie=cookie)[0], 403)
        self.assertTrue(self.request('GET', '/api/account', cookie=cookie)[2]['authenticated'])
        next_cookie, next_csrf, unused = self.login('owner@example.com', cookie=cookie)
        self.assertNotEqual(cookie, next_cookie)
        self.assertNotEqual(csrf, next_csrf)
        self.assertFalse(self.request('GET', '/api/account', cookie=cookie)[2]['authenticated'])
        self.assertEqual(self.request('POST', '/api/auth/logout', {}, cookie=next_cookie, csrf=csrf)[0], 403)
        status, headers, body = self.request('POST', '/api/auth/logout', {}, cookie=next_cookie, csrf=next_csrf)
        self.assertEqual(status, 200)
        self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.assertEqual(body, {'authenticated': False})
        self.assertFalse(self.request('GET', '/api/account', cookie=next_cookie)[2]['authenticated'])

    def test_bad_provider_code_is_sanitized_and_does_not_create_session(self):
        self.server.accounts.invite('owner@example.com')
        status, headers, body = self.request('POST', '/api/auth/verify', {'email': 'owner@example.com', 'token': '000000'})
        self.assertEqual(status, 401)
        self.assertNotIn('Set-Cookie', headers)
        self.assertNotIn('private detail', json.dumps(body))
        self.assertEqual(self.server.accounts.lookup_email('owner@example.com')['status'], 'invited')

    def test_two_users_reads_and_writes_are_isolated_despite_crafted_routing_fields(self):
        first, first_root = self.seed('first@example.com', 'First')
        second, second_root = self.seed('second@example.com', 'Second')
        first_cookie, first_csrf, unused = self.login(first['email'])
        second_cookie, unused_csrf, unused = self.login(second['email'])
        first_state = self.request('GET', '/api/state?account_id=' + second['id'], cookie=first_cookie)
        self.assertEqual(first_state[0], 200)
        self.assertIn('First confidential notes', json.dumps(first_state[2]))
        self.assertNotIn('Second confidential notes', json.dumps(first_state[2]))
        detail = self.request('GET', '/api/jobs/shared-id?root=' + str(second_root), cookie=first_cookie)
        self.assertEqual(detail[0], 200)
        self.assertEqual(detail[2]['job']['company'], 'First company')
        payload = {'action': 'application', 'job_id': 'shared-id', 'stage': 'applied',
                   'notes': 'First updated this', 'account_id': second['id'], 'root': str(second_root),
                   'workspace_id': second['workspace_id']}
        status, unused, body = self.request('POST', '/api/action', payload, cookie=first_cookie, csrf=first_csrf)
        self.assertEqual(status, 200, body)
        for root, expected_stage, expected_note in ((first_root, 'applied', 'First updated this'),
                (second_root, 'saved', 'Second confidential notes')):
            store = Store(root / 'data/jobflow.sqlite3')
            application = store.get('applications', 'shared-id')
            self.assertEqual(application['stage'], expected_stage)
            self.assertEqual(application['notes'], expected_note)
            store.close()
        self.assertNotIn('First updated this', json.dumps(self.request('GET', '/api/state', cookie=second_cookie)[2]))

    def test_other_users_unique_record_cannot_be_read_or_changed(self):
        first, first_root = self.seed('first@example.com', 'First')
        second, second_root = self.seed('second@example.com', 'Second')
        store = Store(second_root / 'data/jobflow.sqlite3')
        original = store.get('jobs', 'shared-id')
        original['id'] = 'second-only'
        store.put('jobs', 'second-only', original)
        store.close()
        cookie, csrf, unused = self.login(first['email'])
        self.assertEqual(self.request('GET', '/api/jobs/second-only', cookie=cookie)[0], 404)
        status, unused, body = self.request('POST', '/api/action',
            {'action': 'application', 'job_id': 'second-only', 'stage': 'applied', 'account_id': second['id']},
            cookie=cookie, csrf=csrf)
        self.assertEqual(status, 400)
        store = Store(first_root / 'data/jobflow.sqlite3')
        self.assertIsNone(store.get('applications', 'second-only'))
        store.close()

    def test_missing_or_other_session_csrf_cannot_write(self):
        first, root = self.seed('first@example.com', 'First')
        second, unused_root = self.seed('second@example.com', 'Second')
        cookie, unused_csrf, unused = self.login(first['email'])
        unused_cookie, other_csrf, unused = self.login(second['email'])
        for csrf in (None, 'invalid', other_csrf):
            self.assertEqual(self.request('POST', '/api/action',
                {'action': 'application', 'job_id': 'shared-id', 'stage': 'applied'}, cookie=cookie, csrf=csrf)[0], 403)
        store = Store(root / 'data/jobflow.sqlite3')
        self.assertEqual(store.get('applications', 'shared-id')['stage'], 'saved')
        store.close()

    def test_existing_account_login_reuses_profile_preferences_and_history(self):
        account, root = self.seed('owner@example.com', 'Existing owner')
        before = {name: (root / 'config' / name).read_bytes() for name in ('settings.json', 'profile.json')}
        store = Store(root / 'data/jobflow.sqlite3')
        store.put('tasks', 'completed', {'id': 'completed', 'status': 'done', 'notes': 'Keep completion notes',
                                        'completed_at': '2025-01-01T10:00:00-08:00', 'actual_minutes': 31})
        records = [tuple(row) for row in store.db.execute('SELECT * FROM records ORDER BY kind,id')]
        store.close()
        cookie, unused_csrf, body = self.login(account['email'])
        self.assertTrue(body['onboarding']['complete'])
        self.assertTrue(body['onboarding']['existing_data_reused'])
        self.assertEqual(body['onboarding']['profile']['name'], 'Existing owner')
        self.assertEqual({name: (root / 'config' / name).read_bytes() for name in before}, before)
        store = Store(root / 'data/jobflow.sqlite3')
        self.assertEqual([tuple(row) for row in store.db.execute('SELECT * FROM records ORDER BY kind,id')], records)
        store.close()
        self.assertIsNone(self.server.queue.status(account['id']))
        self.assertEqual(self.request('GET', '/api/state', cookie=cookie)[0], 200)

    def test_new_account_requires_profile_before_search_and_never_seeds_owner_evidence(self):
        self.seed('existing@example.com', 'Existing owner')
        new = self.server.accounts.invite('new@example.com')
        cookie, csrf, body = self.login(new['email'])
        self.assertFalse(body['onboarding']['complete'])
        self.assertEqual(body['onboarding']['profile']['evidence'], [])
        self.assertNotIn('Existing owner', json.dumps(body))
        self.assertEqual(self.request('GET', '/api/state', cookie=cookie)[0], 409)
        self.assertEqual(self.request('POST', '/api/search', {}, cookie=cookie, csrf=csrf)[0], 409)
        self.assertIsNone(self.server.queue.status(new['id']))
        self.assertEqual(self.request('GET', '/', cookie=cookie)[1]['Location'], '/account')

    def test_request_shape_and_private_paths_are_rejected(self):
        account, unused_root = self.seed('owner@example.com', 'Owner')
        cookie, csrf, unused = self.login(account['email'])
        for path in ('/config/profile.json', '/data/accounts.sqlite3', '/inputs/resume.pdf', '/../README.md'):
            self.assertEqual(self.request('GET', path, cookie=cookie)[0], 404)
        self.assertEqual(self.request('POST', '/api/search', [], cookie=cookie, csrf=csrf)[0], 400)
        self.assertEqual(self.request('POST', '/api/search', {}, cookie=cookie, csrf=csrf,
                                      headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('POST', '/api/action', {'action': 'shell', 'command': 'echo nope'},
                                      cookie=cookie, csrf=csrf)[0], 400)


class CloudAuthTests(unittest.TestCase):
    def test_provider_only_returns_confirmed_matching_identity_and_discards_tokens(self):
        auth = SupabaseEmailAuth('https://project.supabase.co', 'publishable-test')
        user = {'id': 'provider-id', 'email': 'owner@example.com', 'email_confirmed_at': '2026-01-01T00:00:00Z'}
        with patch.object(auth, '_post', return_value={'user': user, 'access_token': 'never-expose',
                                                      'refresh_token': 'never-store'}) as post:
            self.assertEqual(auth.verify('owner@example.com', '123456'), {'id': 'provider-id', 'email': 'owner@example.com'})
            post.assert_called_once_with('verify', {'email': 'owner@example.com', 'token': '123456', 'type': 'email'})
        for value in ({'user': dict(user, email='another@example.com')}, {'user': dict(user, email_confirmed_at=None)},
                      {'user': dict(user, id='')}, {'access_token': 'not-identity'}):
            with self.subTest(value=value), patch.object(auth, '_post', return_value=value), self.assertRaises(AuthError):
                auth.verify('owner@example.com', '123456')
        for token in ('', 'abc123', '1' * 20, None):
            with self.subTest(token=token), patch.object(auth, '_post') as post, self.assertRaises(AuthError):
                auth.verify('owner@example.com', token)
            post.assert_not_called()

    def test_provider_errors_do_not_expose_credentials_or_response_bodies(self):
        auth = SupabaseEmailAuth('https://project.supabase.co', 'publishable-test')
        error = urllib.error.HTTPError('https://project.supabase.co', 400, 'private token', {},
                                      io.BytesIO(b'{"refresh_token":"private-provider-token"}'))
        with patch('jobflow.cloud_auth.urllib.request.urlopen', side_effect=error):
            with self.assertRaises(AuthError) as caught:
                auth.start('owner@example.com')
        self.assertNotIn('private', str(caught.exception))
        self.assertIs(type(caught.exception), AuthError)
        with patch('jobflow.cloud_auth.urllib.request.urlopen', return_value=io.BytesIO(b'{"ok":true}')) as request:
            auth.start('owner@example.com')
            sent = request.call_args.args[0]
            self.assertEqual(json.loads(sent.data), {'email': 'owner@example.com', 'create_user': True})
            self.assertEqual(sent.get_header('Apikey'), 'publishable-test')

    def test_provider_429_is_sanitized_and_remains_an_auth_error_without_retrying(self):
        auth = SupabaseEmailAuth('https://project.supabase.co', 'publishable-test')
        for method in (lambda: auth.start('owner@example.com'),
                       lambda: auth.start_link('owner@example.com', 'v' * 64, 'https://jobs.example.test/auth/callback'),
                       lambda: auth.verify('owner@example.com', '123456')):
            provider_body = io.BytesIO(b'{"email":"owner@example.com","secret":"private-provider-token"}')
            error = urllib.error.HTTPError('https://project.supabase.co', 429,
                                          'owner@example.com private-provider-token', {}, provider_body)
            with patch('jobflow.cloud_auth.urllib.request.urlopen', side_effect=error) as request:
                with self.assertRaises(AuthError) as caught:
                    method()
            self.assertIsInstance(caught.exception, AuthRateLimitError)
            self.assertEqual(str(caught.exception), AuthRateLimitError.message)
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__)
            self.assertEqual(provider_body.tell(), 0)
            request.assert_called_once()

    def test_email_cooldown_expires_and_checks_do_not_extend_it(self):
        cooldown = EmailSendCooldown()
        with patch('jobflow.cloud_auth.time.monotonic', return_value=100):
            cooldown.check()
            cooldown.defer()
        with patch('jobflow.cloud_auth.time.monotonic', return_value=159):
            with self.assertRaises(AuthRateLimitError):
                cooldown.check()
        with patch('jobflow.cloud_auth.time.monotonic', return_value=160):
            cooldown.check()

    def test_hosted_configuration_requires_https_and_public_provider_key(self):
        self.assertEqual(validate_origin('https://jobs.example.test/', '0.0.0.0'), 'https://jobs.example.test')
        self.assertEqual(validate_origin('http://127.0.0.1:8765', '127.0.0.1'), 'http://127.0.0.1:8765')
        for origin, host in (('http://jobs.example.test', '0.0.0.0'), ('http://127.0.0.1:8765', '0.0.0.0'),
                             ('https://name:secret@jobs.example.test', '0.0.0.0'), ('https://jobs.example.test/path', '0.0.0.0')):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                validate_origin(origin, host)
        for url, key in (('http://project.supabase.co', 'public'), ('https://project.supabase.co/path', 'public'),
                         ('https://project.supabase.co', 'sb_secret_not-allowed')):
            with self.subTest(url=url, key=key), self.assertRaises(AuthError):
                SupabaseEmailAuth(url, key)

    def test_rate_limiter_bounds_entries_and_resets_after_window(self):
        limiter = RateLimiter(maximum_keys=2)
        with patch('jobflow.cloud_auth.time.monotonic', return_value=100):
            self.assertTrue(limiter.allow('email', 2, 60))
            self.assertTrue(limiter.allow('email', 2, 60))
            self.assertFalse(limiter.allow('email', 2, 60))
            limiter.allow('other', 1, 60)
            limiter.allow('third', 1, 60)
            self.assertEqual(len(limiter.entries), 2)
        with patch('jobflow.cloud_auth.time.monotonic', return_value=161):
            self.assertTrue(limiter.allow('third', 1, 60))


if __name__ == '__main__':
    unittest.main()

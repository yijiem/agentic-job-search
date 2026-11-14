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
from jobflow.cloud_auth import AuthError, RateLimiter, SupabaseEmailAuth
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
        for path in ('/api/state', '/api/onboarding', '/api/jobs/shared-id', '/api/version', '/app.js',
                     '/trends.js', '/maps.js', '/trends.css', '/maps.css', '/vendor/leaflet/leaflet.js'):
            with self.subTest(path=path):
                status, unused, body = self.request('GET', path)
                self.assertEqual(status, 401)
                self.assertNotIn('profile', body)
        status, headers, unused = self.request('GET', '/')
        self.assertEqual(status, 303)
        self.assertEqual(headers['Location'], '/account')
        self.assertEqual(self.request('GET', '/api/account')[2], {'authenticated': False})
        self.assertEqual(self.request('POST', '/api/action', {'action': 'prep'})[0], 401)

    def test_authenticated_dashboard_assets_share_allowlist_and_preserve_binary_types(self):
        self.server.accounts.invite('owner@example.com')
        cookie, unused_csrf, unused_data = self.login('owner@example.com')
        assets = self.root / 'public'
        assets.mkdir()
        self.server.asset_directory = assets.resolve()
        public = {
            'trends.js': (b'window.JobflowTrends = {};', None),
            'trends.css': (b'.jf-trend {}', 'text/css; charset=utf-8'),
            'maps.js': (b'window.JobflowMaps = {};', None),
            'maps.css': (b'.jf-map {}', 'text/css; charset=utf-8'),
            'vendor/leaflet/leaflet.js': (b'window.L = {};', None),
            'vendor/leaflet/leaflet.css': (b'.leaflet-container {}', 'text/css; charset=utf-8'),
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
                self.assertIn("img-src 'self' data: https://tile.openstreetmap.org;",
                              headers['Content-Security-Policy'])
                self.assertEqual(headers['Referrer-Policy'], 'strict-origin-when-cross-origin')
        for name in ('vendor/leaflet/private.json', 'vendor/leaflet/leaflet.js.map', 'config/profile.json'):
            target = assets / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'not-public')
            self.assertEqual(self.request('GET', '/' + name, cookie=cookie)[0], 404)
        self.assertEqual(self.request('GET', '/vendor/leaflet/%2e%2e/leaflet/leaflet.js', cookie=cookie)[0], 404)
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
        with patch('jobflow.cloud_auth.urllib.request.urlopen', return_value=io.BytesIO(b'{"ok":true}')) as request:
            auth.start('owner@example.com')
            sent = request.call_args.args[0]
            self.assertEqual(json.loads(sent.data), {'email': 'owner@example.com', 'create_user': True})
            self.assertEqual(sent.get_header('Apikey'), 'publishable-test')

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

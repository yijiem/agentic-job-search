"""Security boundaries for browser-bound Supabase email links; no live email."""
import base64
import hashlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from jobflow.cloud import CloudServer
from jobflow.cloud_auth import AuthError, AuthRateLimitError, PendingSignIns, SupabaseEmailAuth


class PendingSignInTests(unittest.TestCase):
    def test_pending_flow_is_browser_secret_bound_and_single_use(self):
        pending = PendingSignIns()
        token, verifier = pending.start('owner@example.com')
        other_token, other_verifier = pending.start('other@example.com')
        self.assertNotEqual(token, other_token)
        self.assertNotEqual(token, verifier)
        self.assertNotEqual(verifier, other_verifier)
        self.assertGreaterEqual(len(token), 43)
        self.assertGreaterEqual(len(verifier), 43)
        self.assertIsNone(pending.consume('not-a-browser-secret'))
        self.assertEqual(pending.consume(token), {'email': 'owner@example.com', 'verifier': verifier})
        self.assertIsNone(pending.consume(token))
        self.assertEqual(pending.consume(other_token)['email'], 'other@example.com')

    def test_expired_replaced_and_revoked_flows_cannot_be_consumed(self):
        pending = PendingSignIns()
        with patch('jobflow.cloud_auth.time.monotonic', return_value=100):
            expired, unused = pending.start('owner@example.com')
            previous, unused = pending.start('owner@example.com')
            replacement, unused = pending.start('owner@example.com', old_token=previous)
            revoked, unused = pending.start('owner@example.com')
            pending.revoke(revoked)
            self.assertIsNone(pending.consume(previous))
            self.assertIsNone(pending.consume(revoked))
        with patch('jobflow.cloud_auth.time.monotonic', return_value=701):
            self.assertIsNone(pending.consume(expired))
            self.assertIsNone(pending.consume(replacement))

    def test_pending_store_is_bounded_and_consume_is_atomic(self):
        pending = PendingSignIns(maximum_keys=2)
        oldest, unused = pending.start('first@example.com')
        pending.start('second@example.com')
        token, unused = pending.start('third@example.com')
        self.assertIsNone(pending.consume(oldest))
        barrier = threading.Barrier(3)
        results = []

        def consume():
            barrier.wait()
            results.append(pending.consume(token))

        threads = [threading.Thread(target=consume) for unused in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(sum(result is not None for result in results), 1)


class SupabasePKCETests(unittest.TestCase):
    def setUp(self):
        self.auth = SupabaseEmailAuth('https://project.supabase.co', 'sb_publishable_test')
        self.email = 'owner@example.com'
        self.verifier = 'x' * 64
        self.code = '932955f3-a115-47dc-927c-e7bbe330b801'

    def test_link_send_uses_s256_challenge_and_exact_callback(self):
        redirect = 'https://jobs.example.test/auth/callback'
        with patch.object(self.auth, '_post', return_value={}) as post:
            self.auth.start_link(self.email, self.verifier, redirect)
        endpoint, payload = post.call_args[0]
        self.assertEqual(urlsplit(endpoint).path, 'otp')
        self.assertEqual(parse_qs(urlsplit(endpoint).query), {'redirect_to': [redirect]})
        expected = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(payload['code_challenge'], expected)
        self.assertEqual(payload['code_challenge_method'], 's256')
        self.assertEqual(payload['email'], self.email)
        self.assertTrue(payload['create_user'])
        self.assertNotIn(self.verifier, json.dumps(payload))

    def test_code_exchange_returns_only_confirmed_matching_identity(self):
        response = {'user': {'id': 'provider-user', 'email': self.email.upper(),
                            'email_confirmed_at': '2026-11-14T18:00:00Z'},
                    'access_token': 'private-access-token', 'refresh_token': 'private-refresh-token'}
        with patch.object(self.auth, '_post', return_value=response) as post:
            identity = self.auth.exchange_link(self.email, self.code, self.verifier)
        self.assertEqual(identity, {'id': 'provider-user', 'email': self.email})
        endpoint, payload = post.call_args[0]
        self.assertEqual(endpoint, 'token?grant_type=pkce')
        self.assertEqual(payload, {'auth_code': self.code, 'code_verifier': self.verifier})

    def test_wrong_email_unconfirmed_email_and_missing_identity_are_rejected(self):
        users = [None, {'id': 'provider-user', 'email': self.email},
                 {'email': self.email, 'email_confirmed_at': 'confirmed'},
                 {'id': 'other-user', 'email': 'other@example.com', 'email_confirmed_at': 'confirmed'}]
        for user in users:
            with self.subTest(user=user), patch.object(self.auth, '_post', return_value={'user': user}):
                with self.assertRaises(AuthError):
                    self.auth.exchange_link(self.email, self.code, self.verifier)


class FakeLinkAuth:
    def __init__(self):
        self.links = []
        self.exchanges = []
        self.codes = []
        self.failure = False
        self.other_email = None

    def start_link(self, email, verifier, redirect_uri):
        self.links.append((email, verifier, redirect_uri))

    def exchange_link(self, email, code, verifier):
        self.exchanges.append((email, code, verifier))
        if self.failure:
            raise AuthError('private provider details with a secret token')
        identity_email = self.other_email or email
        return {'id': 'verified:' + identity_email, 'email': identity_email}

    def start(self, email):
        self.codes.append(email)

    def verify(self, email, token):
        if token != '123456':
            raise AuthError('Invalid code')
        return {'id': 'verified:' + email, 'email': email}


class MagicLinkHTTPTests(unittest.TestCase):
    origin = 'https://jobs.example.test'
    code = '932955f3-a115-47dc-927c-e7bbe330b801'
    callback = '/auth/callback?code=' + code
    navigation = {'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate',
                  'Sec-Fetch-Dest': 'document'}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = FakeLinkAuth()
        self.server = CloudServer(('127.0.0.1', 0), self.root / 'cloud', self.origin, auth=self.auth)
        self.server.accounts.invite('owner@example.com')
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, path, payload=None, cookie=None, headers=None):
        request_headers = {'Host': 'jobs.example.test'}
        if method == 'POST':
            request_headers.update({'Origin': self.origin, 'Content-Type': 'application/json'})
        if cookie:
            request_headers['Cookie'] = cookie
        request_headers.update(headers or {})
        body = json.dumps(payload).encode() if payload is not None else (b'{}' if method == 'POST' else None)
        connection = HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=10)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        values = dict(response.getheaders())
        values['cookies'] = [value for key, value in response.getheaders() if key.casefold() == 'set-cookie']
        parsed = json.loads(data) if values.get('Content-Type', '').startswith('application/json') else data
        status = response.status
        connection.close()
        return status, values, parsed

    def start(self, email='owner@example.com', cookie=None):
        status, headers, body = self.request('POST', '/api/auth/start', {'email': email, 'mode': 'link'}, cookie=cookie)
        self.assertEqual(status, 200, body)
        pending = [value for value in headers['cookies'] if value.startswith('__Host-jobflow-pkce=')]
        self.assertEqual(len(pending), 1)
        return pending[0].split(';', 1)[0], headers, body

    def session_cookie(self, headers):
        return next(value.split(';', 1)[0] for value in headers['cookies'] if value.startswith('__Host-jobflow='))

    def assert_failed_link(self, result):
        status, headers, body = result
        self.assertEqual(status, 303)
        self.assertEqual(headers['Location'], '/account?signin=retry')
        self.assertFalse(any(value.startswith('__Host-jobflow=') for value in headers['cookies']))
        self.assertNotIn('private provider', str(body))
        self.assertNotIn(self.code, str(body))

    def test_link_start_is_generic_and_cookie_is_secure_browser_only(self):
        known_cookie, known_headers, known_body = self.start()
        unknown_cookie, unknown_headers, unknown_body = self.start('stranger@example.com')
        self.assertEqual(known_body, unknown_body)
        self.assertEqual([entry[0] for entry in self.auth.links], ['owner@example.com'])
        self.assertNotEqual(known_cookie, unknown_cookie)
        for headers in (known_headers, unknown_headers):
            value = headers['cookies'][0]
            for flag in ('HttpOnly', 'Secure', 'SameSite=Lax', 'Path=/', 'Max-Age=600'):
                self.assertIn(flag, value)
            self.assertNotIn('Domain=', value)
        self.assertEqual(self.auth.links[0][2], self.origin + '/auth/callback')
        self.assertNotIn(self.auth.links[0][1], json.dumps(known_body))

    def test_provider_429_revokes_pending_link_and_throttles_unknown_addresses_equally(self):
        self.server.auth = SupabaseEmailAuth('https://project.supabase.co', 'sb_publishable_test')
        provider_error = urllib.error.HTTPError('https://project.supabase.co', 429,
            'private email limit for owner@example.com', {},
            io.BytesIO(b'{"secret":"private-provider-token","email":"owner@example.com"}'))
        with patch('jobflow.cloud_auth.urllib.request.urlopen', side_effect=provider_error) as send:
            known = self.request('POST', '/api/auth/start', {'email': 'owner@example.com', 'mode': 'link'})
            with patch.object(self.server.accounts, 'lookup_email', side_effect=AssertionError('must not look up email')):
                unknown = self.request('POST', '/api/auth/start', {'email': 'stranger@example.com', 'mode': 'link'})
        send.assert_called_once()
        for status, headers, body in (known, unknown):
            self.assertEqual(status, 429)
            self.assertEqual(body, {'error': AuthRateLimitError.message})
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(len(headers['cookies']), 1)
            self.assertTrue(headers['cookies'][0].startswith('__Host-jobflow-pkce='))
            self.assertIn('Max-Age=0', headers['cookies'][0])
        self.assertEqual(known[1]['cookies'], unknown[1]['cookies'])
        self.assertEqual(len(self.server.pending_signins.entries), 0)
        self.assertEqual(self.server.accounts.lookup_email('owner@example.com')['status'], 'invited')
        with self.server.accounts._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)

    def test_callback_accepts_top_level_return_then_creates_strict_app_session(self):
        cookie, unused, unused = self.start()
        status, headers, body = self.request('GET', self.callback, cookie=cookie, headers=self.navigation)
        self.assertEqual(status, 200, body)
        self.assertIn('text/html', headers['Content-Type'])
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn(b'callback.js', body)
        self.assertNotIn(self.code.encode(), body)
        session = self.session_cookie(headers)
        self.assertTrue(any('SameSite=Strict' in value for value in headers['cookies'] if value.startswith('__Host-jobflow=')))
        self.assertTrue(any('Max-Age=0' in value for value in headers['cookies'] if value.startswith('__Host-jobflow-pkce=')))
        account = self.request('GET', '/api/account', cookie=session)[2]
        self.assertTrue(account['authenticated'])
        self.assertEqual(account['email'], 'owner@example.com')
        self.assertEqual(self.auth.exchanges, [('owner@example.com', self.code, self.auth.links[0][1])])
        self.assert_failed_link(self.request('GET', self.callback, cookie=cookie, headers=self.navigation))
        self.assertEqual(len(self.auth.exchanges), 1)

    def test_callback_requires_original_browser_cookie_and_does_not_consume_other_browser(self):
        cookie, unused, unused = self.start()
        self.assert_failed_link(self.request('GET', self.callback, headers=self.navigation))
        self.assert_failed_link(self.request('GET', self.callback, cookie='__Host-jobflow-pkce=forged', headers=self.navigation))
        self.assertEqual(self.auth.exchanges, [])
        self.assertEqual(self.request('GET', self.callback, cookie=cookie, headers=self.navigation)[0], 200)

    def test_expired_flow_and_replaced_cookie_do_not_authenticate(self):
        with patch('jobflow.cloud_auth.time.monotonic', return_value=100):
            old_cookie, unused, unused = self.start()
            new_cookie, unused, unused = self.start(cookie=old_cookie)
        self.assert_failed_link(self.request('GET', self.callback, cookie=old_cookie, headers=self.navigation))
        with patch('jobflow.cloud_auth.time.monotonic', return_value=701):
            self.assert_failed_link(self.request('GET', self.callback, cookie=new_cookie, headers=self.navigation))
        self.assertEqual(self.auth.exchanges, [])

    def test_provider_failure_is_private_and_consumes_attempt(self):
        cookie, unused, unused = self.start()
        self.auth.failure = True
        self.assert_failed_link(self.request('GET', self.callback, cookie=cookie, headers=self.navigation))
        self.auth.failure = False
        self.assert_failed_link(self.request('GET', self.callback, cookie=cookie, headers=self.navigation))
        self.assertEqual(len(self.auth.exchanges), 1)
        self.assertEqual(self.server.accounts.lookup_email('owner@example.com')['status'], 'invited')

    def test_provider_cannot_switch_pending_owner_to_another_invited_identity(self):
        self.server.accounts.invite('other@example.com')
        cookie, unused, unused = self.start()
        self.auth.other_email = 'other@example.com'
        self.assert_failed_link(self.request('GET', self.callback, cookie=cookie, headers=self.navigation))
        for email in ('owner@example.com', 'other@example.com'):
            self.assertEqual(self.server.accounts.lookup_email(email)['status'], 'invited')

    def test_missing_duplicate_and_provider_error_queries_do_not_exchange(self):
        for path in ('/auth/callback', '/auth/callback?code=',
                     '/auth/callback?code=first&code=second',
                     '/auth/callback?error_description=private+provider+details'):
            with self.subTest(path=path):
                cookie, unused, unused = self.start()
                self.assert_failed_link(self.request('GET', path, cookie=cookie, headers=self.navigation))
                self.assert_failed_link(self.request('GET', self.callback, cookie=cookie, headers=self.navigation))
        self.assertEqual(self.auth.exchanges, [])

    def test_invitation_disabled_after_request_cannot_establish_a_session(self):
        cookie, unused, unused = self.start()
        with self.server.accounts._connect() as db:
            db.execute("UPDATE accounts SET status='disabled' WHERE email=?", ('owner@example.com',))
        self.assert_failed_link(self.request('GET', self.callback, cookie=cookie, headers=self.navigation))
        self.assertEqual(self.auth.exchanges, [])
        with self.server.accounts._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)

    def test_blank_duplicate_code_and_blank_error_are_rejected(self):
        for suffix in ('&code=', '&error='):
            with self.subTest(suffix=suffix):
                cookie, unused, unused = self.start()
                self.assert_failed_link(self.request('GET', self.callback + suffix,
                                                    cookie=cookie, headers=self.navigation))
        self.assertEqual(self.auth.exchanges, [])

    def test_public_entry_allows_navigation_but_cross_site_private_apis_stay_closed(self):
        for path, expected in (('/', 303), ('/index.html', 303),
                               ('/account', 200), ('/account.html', 200)):
            with self.subTest(path=path):
                self.assertEqual(self.request('GET', path, headers=self.navigation)[0], expected)
                self.assertEqual(self.request('GET', path,
                    headers=dict(self.navigation, **{'Sec-Fetch-Mode': 'cors',
                                                    'Sec-Fetch-Dest': 'empty'}))[0], 403)
        self.assertEqual(self.request('GET', '/api/account', headers=self.navigation)[0], 403)
        self.assertEqual(self.request('GET', '/api/state', headers=self.navigation)[0], 403)

    def test_non_navigation_cross_origin_and_wrong_host_callbacks_are_rejected(self):
        cookie, unused, unused = self.start()
        cases = [dict(self.navigation, Host='attacker.example'),
                 dict(self.navigation, Origin='https://attacker.example'),
                 dict(self.navigation, **{'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty'}),
                 dict(self.navigation, **{'Sec-Fetch-Dest': 'iframe'})]
        for headers in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.request('GET', self.callback, cookie=cookie, headers=headers)[0], 403)
        self.assertEqual(self.request('POST', '/auth/callback', cookie=cookie, headers=self.navigation)[0], 403)
        self.assertEqual(self.auth.exchanges, [])

    def test_otp_code_path_remains_available(self):
        status, unused, unused = self.request('POST', '/api/auth/start', {'email': 'owner@example.com'})
        self.assertEqual(status, 200)
        self.assertEqual(self.auth.codes, ['owner@example.com'])
        status, headers, account = self.request('POST', '/api/auth/verify',
                                               {'email': 'owner@example.com', 'token': '123456'})
        self.assertEqual(status, 200)
        self.assertTrue(account['authenticated'])
        self.assertEqual(self.request('GET', '/api/account', cookie=self.session_cookie(headers))[2]['email'], 'owner@example.com')


if __name__ == '__main__':
    unittest.main()

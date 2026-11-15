"""Invite-only hosted HTTP transport, separate from the local loopback server."""
import base64
import binascii
import hashlib
import json
import mimetypes
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import threading
from http.cookies import CookieError, SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .cloud_accounts import Accounts
from .cloud_auth import AuthError, PendingSignIns, RateLimiter, SupabaseEmailAuth
from .cloud_jobs import WorkQueue, worker_loop
from .onboarding import initialize_workspace, onboarding_state, save_onboarding, upload_resume
from .remote import enabled as remote_enabled, RemoteError, RemoteBusyError
from .remote_interactive import execution_context
from .remote_files import hydrate_workspace
from .web import (ASSET_DIRECTORY, STATIC_ASSETS, MAX_REQUEST_BYTES, MAX_UPLOAD_REQUEST_BYTES,
                  JobflowHandler, WebError, app_build, browser_worker_archive, job_detail,
                  perform_action, profile_summary, snapshot, validate_action_size)

UPLOAD_REQUEST_LIMIT = 7 * 1024 * 1024


def validate_origin(value, host):
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username or parsed.password or
            parsed.query or parsed.fragment or parsed.path not in ('', '/') or
            parsed.scheme not in ('http', 'https')):
        raise ValueError('Set JOBFLOW_PUBLIC_ORIGIN to the exact HTTPS origin of your app.')
    if parsed.scheme == 'http' and not (
            parsed.hostname in ('localhost', '127.0.0.1') and host in ('127.0.0.1', 'localhost')):
        raise ValueError('Hosted access requires HTTPS. HTTP is allowed only on loopback for local development.')
    return '%s://%s' % (parsed.scheme, parsed.netloc)


def source_template(root):
    path = Path(root) / 'config/sources.json'
    if not path.exists():
        path = Path(__file__).resolve().parents[1] / 'config/cloud_sources.example.json'
    rows = json.loads(path.read_text()) if path.exists() else []
    if not isinstance(rows, list):
        raise ValueError('The hosted source template must be a JSON array.')
    allowed = ('id', 'company', 'kind', 'board', 'url', 'enabled')
    return [{k: row[k] for k in allowed if k in row} for row in rows]


class CloudServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, root, origin, auth=None, asset_directory=ASSET_DIRECTORY):
        self.origin = validate_origin(origin, address[0])
        self.secure_cookie = self.origin.startswith('https://')
        self.cookie_name = '__Host-jobflow' if self.secure_cookie else 'jobflow_local_session'
        self.pending_cookie_name = '__Host-jobflow-pkce' if self.secure_cookie else 'jobflow_local_pkce'
        self.pending_signins = PendingSignIns()
        if remote_enabled():
            from .remote_auth_state import RemotePendingSignIns
            self.pending_signins = RemotePendingSignIns()
        self.root = Path(root).resolve()
        self.accounts = Accounts(self.root)
        self.queue = WorkQueue(self.root)
        self.auth = auth if auth is not None else SupabaseEmailAuth()
        self.asset_directory = Path(asset_directory).resolve()
        self.sources = source_template(self.root)
        self.limiter = RateLimiter()
        if remote_enabled():
            from .remote_auth_state import RemoteRateLimiter
            self.limiter = RemoteRateLimiter()
        self.locks = {}
        self.locks_guard = threading.Lock()
        self.request_slots = threading.BoundedSemaphore(2 if remote_enabled() else 16)
        super().__init__(address, CloudHandler)

    def process_request(self, request, client_address):
        if not self.request_slots.acquire(timeout=10 if remote_enabled() else 0):
            # Bound memory/thread use on the public transport. Managed ingress
            # supplies the external TLS endpoint; all responses remain private.
            try:
                request.settimeout(1)
                request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()

    def account_lock(self, account_id):
        with self.locks_guard:
            return self.locks.setdefault(account_id, threading.RLock())


class CloudHandler(JobflowHandler):
    server_version = 'Jobflow'

    def handle_one_request(self):
        # Lazy remote downloads update the local cache too. Keep the entire
        # request inside the same account lock as cache writes and publication.
        self._held_account_locks = {}
        try:
            return super().handle_one_request()
        finally:
            for lock in self._held_account_locks.values():
                lock.release()
            self._held_account_locks = {}

    def send_header(self, keyword, value):
        if keyword.casefold() == 'referrer-policy' and getattr(self, 'auth_callback_response', False):
            value = 'no-referrer'
        super().send_header(keyword, value)

    def end_headers(self):
        for cookie in getattr(self, 'response_cookies', []):
            self.send_header('Set-Cookie', cookie)
        self.response_cookies = []
        location = getattr(self, 'response_location', None)
        if location:
            self.send_header('Location', location)
            self.response_location = None
        if self.server.secure_cookie:
            self.send_header('Strict-Transport-Security', 'max-age=31536000')
        super().end_headers()

    def _allowed_host(self, navigation=False):
        if self.headers.get('Host') != urlsplit(self.server.origin).netloc:
            raise WebError('This request does not match the configured application address.', 403)
        origin = self.headers.get('Origin')
        if origin and origin != self.server.origin:
            raise WebError('Cross-origin requests are not allowed.', 403)
        if self.command == 'POST' and origin != self.server.origin:
            raise WebError('Send changes from the application page.', 403)
        if navigation:
            # Public entry pages and email callbacks can arrive cross-site as
            # top-level navigation. Private APIs keep the strict origin checks.
            if (self.command != 'GET' or
                    self.headers.get('Sec-Fetch-Mode', 'navigate') != 'navigate' or
                    self.headers.get('Sec-Fetch-Dest', 'document') != 'document'):
                raise WebError('Open the sign-in email link in this browser.', 403)
        elif self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise WebError('Cross-site requests are not allowed.', 403)

    def _cookie(self, name):
        value = self.headers.get('Cookie', '')
        try:
            cookie = SimpleCookie()
            cookie.load(value)
            token = cookie[name].value if name in cookie else ''
        except (KeyError, ValueError, CookieError):
            token = ''
        return token if len(token) < 256 else ''

    def _session(self, required=True):
        token = self._cookie(self.server.cookie_name)
        session = self.server.accounts.session(token) if token else None
        if required and not session:
            raise WebError('Sign in to open your workspace.', 401)
        return session, token

    def _root(self, account, hydrate=True):
        # Only a server-side account record can select a workspace. Client
        # account IDs, URL paths, and request bodies never participate in routing.
        if remote_enabled() and account['id'] not in self._held_account_locks:
            lock = self.server.account_lock(account['id'])
            if not lock.acquire(timeout=10):
                raise WebError('Another workspace request is running. Please retry shortly.', 409)
            self._held_account_locks[account['id']] = lock
        root = self.server.accounts.workspace(account)
        if hydrate:
            hydrate_workspace(root)
        return root

    def _account_state(self, session):
        if not session:
            return {'authenticated': False}
        account = session['account']
        return {'authenticated': True, 'email': account['email'],
                'csrf_token': session['csrf_token'],
                'onboarding': onboarding_state(self._root(account))}

    def _csrf(self, session):
        if not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf_token']):
            raise WebError('Reload the app before saving changes.', 403)

    def _json(self, maximum=MAX_REQUEST_BYTES):
        if self.headers.get('Transfer-Encoding'):
            raise WebError('Chunked requests are not supported.')
        if len(self.headers.get_all('Content-Length', [])) != 1:
            raise WebError('Supply one Content-Length header.')
        if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            raise WebError('Send changes as JSON.', 415)
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise WebError('Invalid request size.') from None
        if not 0 < length <= maximum:
            raise WebError('The request is too large or empty.', 413)
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise WebError('Send a JSON object.')
        return payload

    def _set_session_cookie(self, token, expire=False):
        value = '%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%s' % (
            self.server.cookie_name, '' if expire else token, 0 if expire else 43200)
        if self.server.secure_cookie:
            value += '; Secure'
        self.response_cookies = getattr(self, 'response_cookies', []) + [value]

    def _set_pending_cookie(self, token, expire=False):
        value = '%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%s' % (
            self.server.pending_cookie_name, '' if expire else token,
            0 if expire else self.server.pending_signins.ttl)
        if self.server.secure_cookie:
            value += '; Secure'
        self.response_cookies = getattr(self, 'response_cookies', []) + [value]

    def _redirect(self, location):
        self.response_location = location
        self._respond(303, b'', 'text/plain')

    def _asset(self, name):
        if name not in STATIC_ASSETS and name not in ('account.html', 'account.js', 'account.css'):
            raise WebError('File not found.', 404)
        path = self.server.asset_directory / name
        if not path.is_file() or not path.resolve().is_relative_to(self.server.asset_directory):
            raise WebError('File not found.', 404)
        body = path.read_bytes()
        if name.endswith('.html'):
            body = body.replace(b'__JOBFLOW_BUILD__', app_build(self.server.asset_directory).encode('ascii'))
        mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        self._respond(200, body, mime + ('; charset=utf-8' if mime.startswith('text/') or mime in ('application/javascript', 'application/json') else ''))

    def do_GET(self):
        try:
            path = unquote(urlsplit(self.path).path)
            # This endpoint intentionally exposes only process readiness, never
            # account data; cloud health probes may use an internal Host header.
            if path == '/healthz':
                self._respond(200, {'status': 'ok'})
                return
            if path == '/auth/callback':
                self.auth_callback_response = True
                self._allowed_host(navigation=True)
                self._auth_callback()
                return
            self._allowed_host(navigation=path in ('/', '/index.html', '/account', '/account.html'))
            if path == '/auth/callback.js':
                # A fresh document navigation ensures Strict session cookies are
                # sent after returning from the provider's cross-site redirect.
                self._respond(200, b"history.replaceState(null, '', '/account'); location.replace('/');",
                              'application/javascript; charset=utf-8')
                return
            if path in ('/account', '/account.html', '/account.js', '/account.css', '/styles.css',
                        '/theme.js', '/fonts/nunito-latin.woff2', '/fonts/dm-serif-display-latin.woff2'):
                self._asset('account.html' if path == '/account' else path.lstrip('/'))
                return
            session, unused = self._session(required=False)
            if path == '/api/account':
                self._respond(200, self._account_state(session))
                return
            if not session:
                if path in ('/', '/index.html'):
                    self._redirect('/account')
                    return
                raise WebError('Sign in to open your workspace.', 401)
            account = session['account']
            if path == '/api/background':
                self._respond(200, {'background_job': self.server.queue.status(account['id'])})
                return
            if path == '/api/version':
                self._respond(200, {'app_build': app_build(self.server.asset_directory)})
                return
            root = self._root(account, hydrate=path.startswith('/api/'))
            if path == '/api/onboarding':
                self._respond(200, onboarding_state(root))
            elif path == '/api/state':
                if not onboarding_state(root)['complete']:
                    raise WebError('Complete your profile to open your workspace.', 409)
                self._respond(200, {**snapshot(root), 'csrf_token': session['csrf_token'],
                    'account': {'email': account['email'], 'hosted': True},
                    'background_job': self.server.queue.status(account['id']),
                    'app_build': app_build(self.server.asset_directory)})
            elif path == '/api/profile':
                self._respond(200, profile_summary(root))
            elif path == '/api/batches':
                from .application_batches import batch_snapshot
                if not onboarding_state(root)['complete']:
                    raise WebError('Complete your profile before preparing applications.', 409)
                self._respond(200, {**batch_snapshot(root), 'csrf_token': session['csrf_token'],
                                    'codex': {'available': False, 'reason': 'Open your local Jobflow workspace to run applications with Codex on this computer.'}})
            elif path == '/api/resumes':
                from .resumes import resume_library
                self._respond(200, {**resume_library(root), 'csrf_token': session['csrf_token']})
            elif path == '/api/resume-reviews':
                from .resume_reviews import review_list
                resume_id = parse_qs(urlsplit(self.path).query).get('resume_id', [None])[0]
                self._respond(200, review_list(root, resume_id))
            elif path.startswith('/api/resume-reviews/'):
                from .resume_reviews import review_detail
                self._respond(200, review_detail(root, path[len('/api/resume-reviews/'):]))
            elif path.startswith('/api/resumes/'):
                from .resumes import resume_detail
                self._respond(200, resume_detail(root, path[len('/api/resumes/'):]))
            elif path.startswith('/api/resume-drafts/'):
                from .resumes import resume_draft_detail
                self._respond(200, resume_draft_detail(root, path[len('/api/resume-drafts/'):]))
            elif path.startswith('/api/batches/'):
                from .application_batches import batch_detail
                batch = batch_detail(root, path[len('/api/batches/'):])
                if batch is None:
                    raise WebError('Application batch not found.', 404)
                self._respond(200, batch)
            elif path.startswith('/api/application-resumes/'):
                self._resume(root, path[len('/api/application-resumes/'):])
            elif path == '/browser-worker.zip':
                raise WebError('The browser worker is retired. Use Codex from your local Jobflow application batches.', 410)
            elif path.startswith('/api/jobs/'):
                self._respond(200, job_detail(root, path[len('/api/jobs/'):]))
            elif path.startswith('/api/'):
                raise WebError('Unknown API route.', 404)
            elif path in ('/', '/index.html') and not onboarding_state(root)['complete']:
                self._redirect('/account')
            else:
                self._asset('index.html' if path == '/' else path.lstrip('/'))
        except (WebError, ValueError, KeyError, TypeError) as error:
            self._error(error)
        except RemoteError:
            self._respond(503, {'error': 'Your private workspace is temporarily unavailable. Please retry shortly.'})
        except (OSError, sqlite3.Error):
            self._respond(500, {'error': 'Your workspace could not be read. Please try again.'})

    def _auth(self, path, payload):
        email = payload.get('email', '')
        if not isinstance(email, str) or len(email) > 254 or '@' not in email:
            raise WebError('Enter a valid email address.')
        email = email.strip().casefold()
        identity = hashlib.sha256(email.encode()).hexdigest()
        verify = path == '/api/auth/verify'
        if (not self.server.limiter.allow('auth-global', 1000, 900) or
                not self.server.limiter.allow(('verify:' if verify else 'start:') + identity,
                                               10 if verify else 5, 900)):
            raise WebError('Too many sign-in attempts. Please try again later.', 429)
        account = self.server.accounts.lookup_email(email)
        allowed = account and account.get('status') in ('active', 'invited')
        if not verify:
            if payload.get('mode') == 'link':
                previous = self._cookie(self.server.pending_cookie_name)
                pending, verifier = self.server.pending_signins.start(email, previous)
                # Identical cookies and JSON for invited and unknown addresses.
                self._set_pending_cookie(pending)
                if allowed:
                    try:
                        self.server.auth.start_link(email, verifier, self.server.origin + '/auth/callback')
                    except AuthError:
                        self.server.pending_signins.revoke(pending)
                        self._set_pending_cookie('', expire=True)
                        raise
                self._respond(200, {'message': 'If this email has an invitation, a sign-in email is on its way.'})
                return
            if allowed:
                self.server.auth.start(email)
            self._respond(200, {'message': 'If this email has an invitation, a sign-in code is on its way.'})
            return
        if not allowed:
            raise WebError('The sign-in code could not be verified.', 401)
        identity = self.server.auth.verify(email, payload.get('token'))
        account, csrf = self._complete_signin(identity)
        self._respond(200, self._account_state({'account': account, 'csrf_token': csrf}))

    def _complete_signin(self, identity):
        account = self.server.accounts.authenticate_identity(identity['id'], identity['email'])
        root = self._root(account)
        with execution_context(root):
            hydrate_workspace(root)
            initialize_workspace(root, sources=self.server.sources)
            from .reports import atomic_write
            atomic_write(root / 'config/hosted.json', '{"version": 1}\n')
        # Rotate any prior app session rather than retaining a fixed login token.
        unused, old_token = self._session(required=False)
        if old_token:
            self.server.accounts.revoke_session(old_token)
        pending_token = self._cookie(self.server.pending_cookie_name)
        if pending_token:
            self.server.pending_signins.revoke(pending_token)
            self._set_pending_cookie('', expire=True)
        token, csrf = self.server.accounts.create_session(account)
        self._set_session_cookie(token)
        return account, csrf

    def _auth_callback(self):
        pending = self.server.pending_signins.consume(self._cookie(self.server.pending_cookie_name))
        try:
            query = parse_qs(urlsplit(self.path).query, max_num_fields=8, keep_blank_values=True)
            codes = query.get('code', [])
            if (pending is None or len(codes) != 1 or 'error' in query or
                    not re.fullmatch(r'[A-Za-z0-9._~-]{1,2048}', codes[0])):
                raise AuthError('Invalid sign-in link.')
            email = pending['email']
            account = self.server.accounts.lookup_email(email)
            if not account or account.get('status') not in ('active', 'invited'):
                raise AuthError('Invalid sign-in link.')
            identity = self.server.auth.exchange_link(email, codes[0], pending['verifier'])
            if str(identity.get('email', '')).casefold() != email:
                raise AuthError('The sign-in provider did not confirm this email address.')
            self._complete_signin(identity)
        except (AuthError, ValueError, KeyError, TypeError, RemoteError):
            # Never echo provider error text, authorization codes, or email data.
            self._set_pending_cookie('', expire=True)
            self._redirect('/account?signin=retry')
            return
        self._respond(200, b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                      b'<meta name="referrer" content="no-referrer"><title>Opening Jobflow</title>'
                      b'<script src="/auth/callback.js" defer></script></head><body>'
                      b'<p>Opening your workspace. <a href="/">Continue to Jobflow</a></p></body></html>',
                      'text/html; charset=utf-8')

    def do_POST(self):
        try:
            self._allowed_host()
            path = urlsplit(self.path).path
            if path in ('/api/auth/start', '/api/auth/verify'):
                self._auth(path, self._json(8192))
                return
            session, token = self._session()
            self._csrf(session)
            if path == '/api/auth/logout':
                self._json(8192)
                self.server.accounts.revoke_session(token)
                self._set_session_cookie('', expire=True)
                self._respond(200, {'authenticated': False})
                return
            if path not in ('/api/action', '/api/onboarding', '/api/resume', '/api/search'):
                raise WebError('Unknown API route.', 404)
            payload = self._json(UPLOAD_REQUEST_LIMIT if path == '/api/resume' else
                                 MAX_UPLOAD_REQUEST_BYTES if path == '/api/action' else MAX_REQUEST_BYTES)
            if path == '/api/action':
                validate_action_size(payload, int(self.headers['Content-Length']))
            account = session['account']
            root = self._root(account)
            lock = self.server.account_lock(account['id'])
            if not lock.acquire(blocking=False):
                raise WebError('Another update is being saved. Please try again shortly.', 409)
            try:
                with execution_context(root):
                    hydrate_workspace(root)
                    if path == '/api/onboarding':
                        before = onboarding_state(root)
                        result = save_onboarding(root, payload)
                        self.server.accounts.mark_onboarding_complete(account)
                        if not before['complete']:
                            self.server.queue.enqueue(account['id'], 'run')
                        elif (before['profile'] != result['profile'] or before['preferences'] != result['preferences']):
                            self.server.queue.enqueue(account['id'], 'rematch')
                    elif path == '/api/resume':
                        encoded = payload.get('content_base64')
                        if not isinstance(encoded, str):
                            raise WebError('Upload a file as base64 text.')
                        try:
                            content = base64.b64decode(encoded, validate=True)
                        except (ValueError, binascii.Error):
                            raise WebError('The uploaded file could not be decoded.') from None
                        result = upload_resume(root, payload.get('filename', ''), content)
                    elif path == '/api/search':
                        if not onboarding_state(root)['complete']:
                            raise WebError('Complete your profile before starting a search.', 409)
                        if not self.server.limiter.allow('search:' + account['id'], 3, 3600):
                            raise WebError('Please wait before starting another search.', 429)
                        self.server.queue.enqueue(account['id'], 'run')
                        result = {'background_job': self.server.queue.status(account['id'])}
                    else:
                        if not onboarding_state(root)['complete']:
                            raise WebError('Complete your profile before saving workspace changes.', 409)
                        result = perform_action(root, payload)
                        if remote_enabled():
                            self.server.queue.enqueue(account['id'], 'refresh')
            finally:
                lock.release()
            self._respond(200, result)
        except RemoteBusyError:
            self._respond(409, {'error': 'Another workspace update is running. Please retry shortly.'})
        except RemoteError:
            self._respond(503, {'error': 'Your private workspace could not save this change. Please retry shortly.'})
        except AuthError:
            self._respond(401, {'error': 'Sign-in could not be completed. Request a fresh sign-in email and try again.'})
        except (WebError, ValueError, KeyError, TypeError) as error:
            self._error(error)
        except subprocess.TimeoutExpired:
            self._respond(504, {'error': 'The update took too long. Refresh to check its state before retrying.'})
        except (OSError, sqlite3.Error):
            self._respond(500, {'error': 'Your workspace could not save this change. Please try again.'})


def serve(root, host='127.0.0.1', port=8765, origin=None, worker=True):
    os.umask(0o077)
    server = CloudServer((host, port), root, origin or os.environ.get('JOBFLOW_PUBLIC_ORIGIN', ''))
    stop = threading.Event()
    thread = None
    if worker:
        thread = threading.Thread(target=worker_loop, args=(server.accounts, server.queue, stop), daemon=True)
        thread.start()
    def shutdown(unused_signum, unused_frame):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, shutdown)
    print('Hosted Jobflow is listening; invited accounts and HTTPS origin are enforced.', flush=True)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        if thread:
            thread.join(timeout=2)
        for signum, handler in previous.items():
            signal.signal(signum, handler)

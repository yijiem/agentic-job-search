"""Invite-only hosted HTTP transport, separate from the local loopback server."""
import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import signal
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from contextlib import contextmanager, ExitStack
from http.cookies import CookieError, SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .cloud_accounts import Accounts
from .cloud_auth import (AuthError, AuthRateLimitError, EmailSendCooldown,
                         PendingSignIns, RateLimiter, SupabaseEmailAuth)
from .cloud_jobs import WorkQueue, worker_loop
from .onboarding import (initialize_workspace, onboarding_complete, onboarding_state,
                         save_onboarding, upload_resume)
from .remote import enabled as remote_enabled, RemoteError, RemoteBusyError
from .remote_interactive import INTERACTIVE_LEASE_SECONDS, execution_context
from .remote_files import file_read_cache, hydrate_workspace
from .web import (ASSET_DIRECTORY, STATIC_ASSETS, MAX_REQUEST_BYTES, MAX_UPLOAD_REQUEST_BYTES,
                  JobflowHandler, WebError, app_build, browser_worker_archive, job_detail,
                  perform_action, profile_summary, snapshot, validate_action_size)

UPLOAD_REQUEST_LIMIT = 7 * 1024 * 1024
PDF_REVIEW_DRAFT_OPS = frozenset(('pdf_review_create', 'pdf_review_update', 'pdf_review_suggest'))


class WorkspaceBusyError(WebError):
    """A request could not acquire its account lock before changing any data."""

    def __init__(self):
        super().__init__('Another workspace update is running. Please retry shortly.', 409)


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
        self.email_send_cooldown = EmailSendCooldown()
        self.asset_directory = Path(asset_directory).resolve()
        self.sources = source_template(self.root)
        self.limiter = RateLimiter()
        if remote_enabled():
            from .remote_auth_state import RemoteRateLimiter
            self.limiter = RemoteRateLimiter()
        self.locks = {}
        self.locks_guard = threading.Lock()
        self.request_slots = threading.BoundedSemaphore(4 if remote_enabled() else 16)
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

    def _allowed_host(self, navigation=False, runner=False):
        if self.headers.get('Host') != urlsplit(self.server.origin).netloc:
            raise WebError('This request does not match the configured application address.', 403)
        origin = self.headers.get('Origin')
        if origin and origin != self.server.origin:
            raise WebError('Cross-origin requests are not allowed.', 403)
        if self.command == 'POST' and origin != self.server.origin and not runner:
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
        reads = getattr(self, '_read_workspaces', None)
        if remote_enabled() and reads is not None:
            roots, contexts = reads
            if account['id'] not in roots:
                roots[account['id']] = contexts.enter_context(self._user_workspace(account))
            return roots[account['id']]
        if remote_enabled() and account['id'] not in self._held_account_locks:
            lock = self.server.account_lock(account['id'])
            if not lock.acquire(timeout=10):
                raise WorkspaceBusyError()
            self._held_account_locks[account['id']] = lock
        root = self.server.accounts.workspace(account)
        if hydrate:
            hydrate_workspace(root)
        return root

    @contextmanager
    def _user_workspace(self, account):
        """An isolated disposable cache lets small commands bypass read locks.

        Canonical workspace routing still comes only from the account registry.
        Remote objects and the atomic record transaction remain authoritative.
        Legacy/config writers keep their existing shared-cache serialization.
        """
        root = self.server.accounts.workspace(account)
        if not remote_enabled():
            yield root
            return
        from .remote import workspace_id
        from .remote_files import CONFIG_FILES
        identifier = workspace_id(root, required=True)
        with tempfile.TemporaryDirectory(prefix='jobflow-user-') as directory:
            scratch = Path(directory)
            (scratch / 'config').mkdir(mode=0o700)
            (scratch / 'config/remote.json').write_text(json.dumps({'workspace_id': identifier}))
            for relative in CONFIG_FILES:
                source = root / relative
                if source.is_file() and not source.is_symlink():
                    try:
                        shutil.copyfile(source, scratch / relative)
                    except FileNotFoundError:
                        pass
            with file_read_cache():
                hydrate_workspace(scratch, uploads=False)
                yield scratch

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

    def _codex_identity(self):
        """A bearer is accepted only at the runner endpoints, never as a session."""
        from .hosted_codex import AccessError, ACCESS_ERROR, authenticate, token_route
        from .storage import Store
        if urlsplit(self.path).query or self.headers.get('Cookie'):
            raise WebError('Use a Codex bearer header without cookies or query parameters.', 403)
        headers = self.headers.get_all('Authorization', [])
        if len(headers) != 1 or not headers[0].startswith('Bearer '):
            raise WebError(ACCESS_ERROR, 401)
        token = headers[0][7:]
        try:
            account_id, unused = token_route(token)
            account = self.server.accounts.get_account(account_id)
            if not account or account.get('status') != 'active':
                raise AccessError(ACCESS_ERROR)
            root = self._root(account, hydrate=False)
            store = Store(root / 'data/jobflow.sqlite3')
            try:
                authenticate(root, store, token, account_id)
            finally:
                store.close()
        except (AccessError, ValueError):
            raise WebError(ACCESS_ERROR, 401) from None
        return account, root, token

    def _codex_runner(self, payload=None, resume_id=None):
        from .web import _json_action
        account, root, token = self._codex_identity()
        lock = self.server.account_lock(account['id'])
        if not lock.acquire(blocking=False):
            raise WebError('Another workspace update is running. Read the run again before continuing.', 409)
        try:
            with execution_context(root, seconds=INTERACTIVE_LEASE_SECONDS):
                hydrate_workspace(root)
                request = {'op': 'codex_hosted_resume' if resume_id is not None else 'codex_hosted_execute',
                           'account_id': account['id'], 'token': token}
                request.update({'resume_id': resume_id} if resume_id is not None else {'request': payload})
                result = _json_action(root, request, 'batch')
                if (remote_enabled() and payload and payload.get('op') == 'codex_report' and
                        payload.get('status') == 'submitted'):
                    try:
                        self.server.queue.enqueue(account['id'], 'refresh')
                    except (RemoteError, OSError, sqlite3.Error):
                        result['warning'] = 'Submission saved. Full reports will refresh on the next scheduled run.'
        finally:
            lock.release()
        if resume_id is not None:
            self._respond(200, base64.b64decode(result['content_base64'], validate=True), 'application/octet-stream')
        else:
            self._respond(200, result)

    def _codex_browser_action(self, root, account, payload):
        from .web import _json_action
        from .codex_handoff import hosted_handoff
        operation = payload['op']
        mapped = {'codex_prepare': 'codex_hosted_prepare', 'codex_renew': 'codex_hosted_renew',
                  'codex_revoke': 'codex_hosted_revoke', 'codex_discard': 'codex_discard'}[operation]
        request = {key: payload[key] for key in ('batch_id', 'revision', 'workspace_id', 'job_ids') if key in payload}
        request.update(op=mapped, account_id=account['id'])
        return hosted_handoff(self.server.origin, _json_action(root, request, 'batch'))

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
        return super()._asset(name, extra_assets=('account.html', 'account.js', 'account.css'))

    def do_GET(self):
        # The email callback is a GET that can initialize and publish a new
        # workspace. It must observe its own writes without a read-only cache.
        path = unquote(urlsplit(self.path).path)
        if path == '/auth/callback' or path.startswith('/api/codex/resumes/'):
            return self._get()
        with file_read_cache(), ExitStack() as contexts:
            # Normal reads get disposable caches too. They cannot overwrite a
            # configuration writer's unpublished files or block another page.
            self._read_workspaces = ({}, contexts)
            try:
                self._get()
            finally:
                self._read_workspaces = None

    def _get(self):
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
            if path in ('/codex/runner.py', '/codex/workflow.md'):
                source = (Path(__file__).with_name('hosted_codex_client.py') if path.endswith('.py') else
                          Path(__file__).resolve().parents[1] / 'HOSTED_CODEX_APPLICATIONS.md')
                self._respond(200, source.read_bytes(), 'text/plain; charset=utf-8')
                return
            if path.startswith('/api/codex/resumes/'):
                self._codex_runner(resume_id=path[len('/api/codex/resumes/'):])
                return
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
            # These source-controlled assets contain no account data. Serving
            # them does not need session RPCs or the account's workspace lock.
            if path.lstrip('/') in STATIC_ASSETS and not path.endswith('.html'):
                self._asset(path.lstrip('/'))
                return
            if path == '/api/version':
                self._respond(200, {'app_build': app_build(self.server.asset_directory)})
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
            root = self._root(account, hydrate=path.startswith('/api/') or path in ('/', '/index.html'))
            if path == '/api/onboarding':
                self._respond(200, onboarding_state(root))
            elif path == '/api/state':
                if not onboarding_complete(root):
                    raise WebError('Complete your profile to open your workspace.', 409, 'onboarding_required')
                self._respond(200, {**snapshot(root), 'csrf_token': session['csrf_token'],
                    'account': {'email': account['email'], 'hosted': True},
                    'background_job': self.server.queue.status(account['id']),
                    'app_build': app_build(self.server.asset_directory)})
            elif path == '/api/profile':
                self._respond(200, profile_summary(root))
            elif path == '/api/batches':
                from .application_batches import batch_snapshot
                if not onboarding_complete(root):
                    raise WebError('Complete your profile before preparing applications.', 409)
                self._respond(200, {**batch_snapshot(root), 'csrf_token': session['csrf_token'],
                                    'codex': {'available': True, 'mode': 'hosted'}})
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
                from .application_batches import workspace_id
                self._respond(200, {**batch, 'workspace_id': workspace_id(root)})
            elif path.startswith('/api/application-resumes/'):
                self._resume(root, path[len('/api/application-resumes/'):])
            elif path == '/browser-worker.zip':
                raise WebError('The browser worker is retired. Use Codex from your local Jobflow application batches.', 410)
            elif path.startswith('/api/jobs/'):
                self._respond(200, job_detail(root, path[len('/api/jobs/'):]))
            elif path.startswith('/api/'):
                raise WebError('Unknown API route.', 404)
            elif path in ('/', '/index.html') and not onboarding_complete(root):
                self._redirect('/account')
            else:
                self._asset('index.html' if path == '/' else path.lstrip('/'))
        except (WebError, ValueError, KeyError, TypeError) as error:
            self._error(error)
        except RemoteError:
            self._respond(503, {'error': 'Your private workspace is temporarily unavailable. Please retry shortly.'})
        except subprocess.TimeoutExpired:
            self._respond(504, {'error': 'The request took too long. Read the run state before continuing.'})
        except (OSError, sqlite3.Error):
            self._respond(500, {'error': 'Your workspace could not be read. Please try again.'})

    def _auth(self, path, payload):
        email = payload.get('email', '')
        if not isinstance(email, str) or len(email) > 254 or '@' not in email:
            raise WebError('Enter a valid email address.')
        email = email.strip().casefold()
        identity = hashlib.sha256(email.encode()).hexdigest()
        verify = path == '/api/auth/verify'
        if not verify:
            # Apply an observed provider limit equally before invitation lookup.
            self.server.email_send_cooldown.check()
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
                    except AuthError as error:
                        if isinstance(error, AuthRateLimitError):
                            self.server.email_send_cooldown.defer()
                        self.server.pending_signins.revoke(pending)
                        self._set_pending_cookie('', expire=True)
                        raise
                self._respond(200, {'message': 'If this email has an invitation, a sign-in email is on its way.'})
                return
            if allowed:
                try:
                    self.server.auth.start(email)
                except AuthRateLimitError:
                    self.server.email_send_cooldown.defer()
                    raise
            self._respond(200, {'message': 'If this email has an invitation, a sign-in code is on its way.'})
            return
        if not allowed:
            raise WebError('The sign-in code could not be verified.', 401)
        identity = self.server.auth.verify(email, payload.get('token'))
        account, csrf = self._complete_signin(identity)
        self._respond(200, self._account_state({'account': account, 'csrf_token': csrf}))

    def _complete_signin(self, identity):
        account = self.server.accounts.authenticate_identity(identity['id'], identity['email'])
        # Completed onboarding/adoption is durable account-registry state, not
        # a disposable cache marker. Returning users need only authentication:
        # workspace reads happen on the next request using an isolated cache.
        # Taking the worker's lease here would consume a valid email link before
        # a busy workspace could issue its session, including after cold starts.
        if not account.get('onboarding_complete'):
            root = self._root(account)
            with execution_context(root, seconds=INTERACTIVE_LEASE_SECONDS):
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
        try:
            pending = self.server.pending_signins.consume(self._cookie(self.server.pending_cookie_name))
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
        except (RemoteError, WorkspaceBusyError, OSError, sqlite3.Error):
            # A backend failure is not evidence that the email link was invalid.
            # The one-use attempt may already be consumed; never replay it.
            self._set_pending_cookie('', expire=True)
            self._redirect('/account?signin=unavailable')
            return
        except (AuthError, ValueError, KeyError, TypeError):
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
        path = urlsplit(self.path).path
        acquiring_workspace = False
        payload = None
        try:
            self._allowed_host(runner=path == '/api/codex/runner')
            if path == '/api/codex/runner':
                self._codex_runner(payload=self._json())
                return
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
            # Enqueueing search and committing small user edits do not borrow
            # the worker's renewable workspace lease. The CLI checks revisions
            # in a short atomic transaction before acknowledging a save.
            from .foreground import supported
            if path == '/api/search' or (path == '/api/action' and supported(payload)):
                with self._user_workspace(account) as root:
                    if not onboarding_complete(root):
                        raise WebError('Complete your profile before saving workspace changes.', 409, 'onboarding_required')
                    if path == '/api/search':
                        if not self.server.limiter.allow('search:' + account['id'], 3, 3600):
                            raise WebError('Please wait before starting another search.', 429)
                        self.server.queue.enqueue(account['id'], 'run')
                        result = {'background_job': self.server.queue.status(account['id'])}
                    else:
                        result = perform_action(root, payload)
                        if payload.get('action') in ('application', 'task', 'interview'):
                            try:
                                if not remote_enabled():
                                    self.server.queue.enqueue(account['id'], 'refresh')
                                result['background_job'] = self.server.queue.status(account['id'])
                            except (OSError, sqlite3.Error, RemoteError):
                                # The edit and durable refresh intent already
                                # committed. A status read cannot undo success.
                                pass
                self._respond(200, result)
                return
            # Only contention before entering the execution context is safe to
            # retry automatically. A later lease loss can follow a committed
            # write, so it must never invite a blind replay of the request.
            acquiring_workspace = True
            root = self._root(account)
            lock = self.server.account_lock(account['id'])
            if not lock.acquire(blocking=False):
                raise WorkspaceBusyError()
            try:
                with execution_context(root, seconds=INTERACTIVE_LEASE_SECONDS):
                    acquiring_workspace = False
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
                        if payload.get('action') == 'batch' and payload.get('op') in (
                                'codex_prepare', 'codex_discard', 'codex_renew', 'codex_revoke'):
                            result = self._codex_browser_action(root, account, payload)
                        else:
                            result = perform_action(root, payload)
                        # Draft annotations do not change preparation or report
                        # inputs. Queuing a full refresh here makes the worker
                        # lock out the user's next annotation unnecessarily.
                        review_draft = (payload.get('action') == 'batch' and
                                        payload.get('op') in PDF_REVIEW_DRAFT_OPS)
                        if remote_enabled() and not review_draft:
                            self.server.queue.enqueue(account['id'], 'refresh')
            finally:
                lock.release()
            self._respond(200, result)
        except (RemoteBusyError, WorkspaceBusyError):
            if acquiring_workspace:
                self._respond(409, {'error': 'Another workspace update is running. Please retry shortly.',
                                    'code': 'workspace_busy', 'retryable': True})
            else:
                self._respond(409, {'error': 'The workspace update could not be confirmed. Refresh to check its saved state before retrying.',
                                    'code': 'workspace_update_uncertain', 'retryable': False})
        except RemoteError:
            self._respond(503, {'error': 'Your private workspace could not save this change. Please retry shortly.'})
        except AuthRateLimitError:
            if path == '/api/auth/start':
                # Keep the response cookie shape independent of invitation status.
                self.response_cookies = []
                self._set_pending_cookie('', expire=True)
            self._respond(429, {'error': AuthRateLimitError.message})
        except AuthError:
            self._respond(401, {'error': 'Sign-in could not be completed. Request a fresh sign-in email and try again.'})
        except (WebError, ValueError, KeyError, TypeError) as error:
            if (path == '/api/action' and isinstance(payload, dict) and
                    payload.get('action') == 'batch' and
                    isinstance(payload.get('op'), str) and
                    payload.get('op') in PDF_REVIEW_DRAFT_OPS | {'pdf_review_save'} and
                    str(error).strip() == 'This PDF review changed. Reload it before saving your changes.'):
                self._respond(409, {'error': str(error).strip(), 'code': 'review_conflict', 'retryable': False})
            else:
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

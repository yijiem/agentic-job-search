"""Small server-only Supabase transport and fenced workspace leases.

The browser never receives this client's key. Remote workspace markers are made
from the administrative account registry, never from a browser-supplied path.
"""
import contextvars
import gzip
import io
import json
import os
import re
import secrets
import threading
import time
import uuid
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_RESPONSE_BYTES = 64 * 1024 * 1024
READ_ONLY_RPCS = frozenset((
    'jobflow_record_get', 'jobflow_records_page', 'jobflow_runs_recent',
    'jobflow_workspace_stats', 'jobflow_import_verify', 'jobflow_file_get',
    'jobflow_file_list', 'jobflow_account_lookup', 'jobflow_account_get',
    'jobflow_account_list', 'jobflow_session_get', 'jobflow_queue_status',
))
TRANSIENT_HTTP_STATUS = frozenset((408, 429, 500, 502, 503, 504))
RETRY_DELAYS = (0.25, 0.75)
_leases = contextvars.ContextVar('jobflow_remote_leases', default={})


class RemoteError(RuntimeError):
    """A sanitized service error, safe to include in administrative logs."""
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_exhausted = False


class RemoteResponseTooLarge(RemoteError):
    """A read exceeded the bounded encoded or decoded response size."""


class RemoteBusyError(RemoteError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        # Never forward the privileged API key to a redirect destination.
        raise RemoteError('Supabase unexpectedly redirected a private request.')


def enabled():
    return os.environ.get('JOBFLOW_BACKEND', '').casefold() == 'supabase'


def workspace_id(root, required=False):
    marker = Path(root) / 'config/remote.json'
    if not enabled():
        if required:
            raise RemoteError('Supabase persistence is not configured.')
        return None
    if not marker.exists():
        if required:
            raise RemoteError('The private workspace has not been initialized.')
        return None
    if marker.is_symlink() or marker.parent.is_symlink():
        raise RemoteError('The private workspace marker must not be a symbolic link.')
    try:
        value = json.loads(marker.read_text(encoding='utf-8'))['workspace_id']
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (OSError, KeyError, ValueError, TypeError):
        raise RemoteError('The private workspace marker is invalid.') from None
    return value


class SupabaseClient:
    def __init__(self, url, key, opener=None, timeout=45):
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment or
                parsed.path not in ('', '/') or parsed.port not in (None, 443)):
            raise RemoteError('Set SUPABASE_URL to the project HTTPS origin.')
        if not isinstance(key, str) or not key or any(c.isspace() for c in key):
            raise RemoteError('Set a server-only Supabase secret key.')
        if key.startswith('sb_publishable_'):
            raise RemoteError('A publishable key cannot access private server persistence.')
        self.url = url.rstrip('/')
        self._key = key
        self._opener = opener or build_opener(_NoRedirect())
        self.timeout = timeout
        self.requests = 0
        self.sent_bytes = 0
        self.received_bytes = 0

    @staticmethod
    def _retry_safe(operation):
        """Retry only a caller-proven idempotent operation, at most twice."""
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                return operation()
            except RemoteError as error:
                transient = (error.status in TRANSIENT_HTTP_STATUS or
                             (error.status is None and str(error) ==
                              'The private service could not be reached. Please retry.'))
                if not transient:
                    raise
                if attempt == len(RETRY_DELAYS):
                    error.retry_exhausted = True
                    raise
                time.sleep(RETRY_DELAYS[attempt])

    def request(self, method, path, payload=None, headers=None, raw=False):
        operation = lambda: self._request_once(method, path, payload, headers, raw)
        # POST is not generally idempotent: only reviewed read-only functions
        # qualify. Claims, counters, run starts, and one-time auth consumption
        # must never be replayed after an uncertain response.
        read_rpc = (method == 'POST' and isinstance(path, str) and
                    path in {'/rest/v1/rpc/' + name for name in READ_ONLY_RPCS})
        storage_read = (method == 'GET' and isinstance(path, str) and
                        path.startswith('/storage/v1/object/'))
        return self._retry_safe(operation) if read_rpc or storage_read else operation()

    def _request_once(self, method, path, payload=None, headers=None, raw=False):
        if not isinstance(path, str) or not path.startswith('/') or path.startswith('//'):
            raise RemoteError('Invalid private service request path.')
        values = {'apikey': self._key, 'Authorization': 'Bearer ' + self._key,
                  'Accept': 'application/json', 'Accept-Encoding': 'gzip',
                  'User-Agent': 'Jobflow/1.0'}
        data = payload
        if payload is not None and not isinstance(payload, bytes):
            data = json.dumps(payload, ensure_ascii=False, separators=(',', ':'),
                              allow_nan=False).encode('utf-8')
            values['Content-Type'] = 'application/json'
        if headers:
            if any(name.casefold() in ('apikey', 'authorization', 'host') for name in headers):
                raise RemoteError('Private service authentication cannot be overridden.')
            values.update(headers)
        request = Request(self.url + path, data=data, headers=values, method=method)
        self.requests += 1
        self.sent_bytes += len(data or b'')
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                content_encoding = response.headers.get('Content-Encoding', '').casefold()
                self.received_bytes += len(body)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise RemoteResponseTooLarge('The private service response exceeded its size limit.')
        except HTTPError as error:
            code = None
            try:
                detail = json.loads(error.read(16384))
                candidate = detail.get('code', '')
                code = candidate if isinstance(candidate, str) and re.fullmatch(r'[A-Za-z0-9_]{1,32}', candidate) else None
            except (ValueError, OSError, AttributeError):
                pass
            kind = RemoteBusyError if code == '55P03' else RemoteError
            raise kind('Private service request failed (HTTP %s%s).' %
                       (error.code, ', code ' + code if code else ''), error.code, code) from None
        except (URLError, TimeoutError, OSError, IncompleteRead):
            raise RemoteError('The private service could not be reached. Please retry.') from None
        # Invalid compression is a response integrity error, not a transport
        # failure. Keep it outside the network exception/retry classification.
        if content_encoding == 'gzip':
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                    body = stream.read(MAX_RESPONSE_BYTES + 1)
            except (OSError, EOFError):
                raise RemoteError('The private service returned invalid compressed content.') from None
            if len(body) > MAX_RESPONSE_BYTES:
                raise RemoteResponseTooLarge('The private service response exceeded its size limit.')
        if raw:
            return body
        if not body:
            return None
        try:
            return json.loads(body)
        except (ValueError, UnicodeError):
            raise RemoteError('The private service returned an invalid response.') from None

    def rpc(self, name, params):
        if not re.fullmatch(r'jobflow_[a-z0-9_]+', name):
            raise RemoteError('Invalid private service operation.')
        return self.request('POST', '/rest/v1/rpc/' + name, params)

    @staticmethod
    def _object_path(bucket, key):
        if (not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,99}', bucket) or
                not isinstance(key, str) or not key or key.startswith('/') or
                any(part in ('', '.', '..') for part in key.split('/'))):
            raise RemoteError('Invalid private storage object.')
        return '/storage/v1/object/' + quote(bucket, safe='') + '/' + quote(key, safe='/')

    def storage_download(self, bucket, key):
        return self.request('GET', self._object_path(bucket, key), raw=True)

    def storage_upload(self, bucket, key, data, content_type='application/octet-stream'):
        if not isinstance(data, bytes):
            raise TypeError('Storage uploads require bytes.')
        path = self._object_path(bucket, key)
        operation = lambda: self.request('POST', path, data,
                            {'Content-Type': content_type, 'x-upsert': 'true'})
        immutable = re.fullmatch(r'[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}/[a-f0-9]{64}(?:\.gz)?', key)
        # These objects are addressed by a workspace and content hash. Replaying
        # the same bytes to the same immutable key cannot create a second file.
        return self._retry_safe(operation) if immutable else operation()


def client_from_env():
    key = os.environ.get('SUPABASE_SECRET_KEY') or os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    return SupabaseClient(os.environ.get('SUPABASE_URL', ''), key)


def token_for(root):
    identifier = workspace_id(root, required=True)
    lease = _leases.get().get(identifier)
    if lease is not None:
        lease.check()
        return lease.token
    if os.environ.get('JOBFLOW_LEASE_WORKSPACE_ID') == identifier:
        token = os.environ.get('JOBFLOW_LEASE_TOKEN', '')
        if re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token):
            return token
    raise RemoteError('A workspace lease is required to save private data.')


def lease_environment(root):
    result = dict(os.environ)
    result['JOBFLOW_LEASE_WORKSPACE_ID'] = workspace_id(root, required=True)
    result['JOBFLOW_LEASE_TOKEN'] = token_for(root)
    return result


class RemoteLease:
    """Renewable per-workspace lock; every server-side write checks its token."""
    def __init__(self, root, seconds=1900, client=None):
        self.root = Path(root)
        self.workspace = workspace_id(root, required=True)
        self.seconds = max(30, min(int(seconds), 3600))
        self.client = client or client_from_env()
        self.token = None
        self._owned = False
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = None
        self._context_token = None
        self._parent = None

    def _request(self, action):
        return self.client.rpc('jobflow_workspace_lease', {
            'p_workspace': self.workspace, 'p_token': self.token,
            'p_seconds': self.seconds, 'p_action': action})

    def __enter__(self):
        existing = _leases.get().get(self.workspace)
        if existing is not None:
            existing.check()
            self._parent = existing
            self.token = existing.token
        elif os.environ.get('JOBFLOW_LEASE_WORKSPACE_ID') == self.workspace:
            candidate = os.environ.get('JOBFLOW_LEASE_TOKEN', '')
            if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', candidate):
                raise RemoteError('The inherited workspace lease is invalid.')
            self.token = candidate
            if not self._request('renew'):
                raise RemoteBusyError('The inherited workspace lease has expired.')
        else:
            self.token = secrets.token_urlsafe(32)
            if not self._request('acquire'):
                raise RemoteBusyError('Another workflow process is running. Please try again shortly.')
            self._owned = True
        self._context_token = _leases.set({**_leases.get(), self.workspace: self})
        if self._owned:
            self._thread = threading.Thread(target=self._renew, daemon=True)
            self._thread.start()
        return self

    def _renew(self):
        while not self._stop.wait(max(5, self.seconds / 3)):
            try:
                if not self._request('renew'):
                    self._lost.set()
                    return
            except RemoteError:
                # Fail closed after an uncertain lease renewal. The DB fences
                # any stale process even if it continues after this failure.
                self._lost.set()
                return

    def check(self):
        if self._parent is not None:
            self._parent.check()
        if self._lost.is_set():
            raise RemoteBusyError('The workspace lease was lost; retry this operation.')

    def __exit__(self, exc_type, exc_value, traceback):
        self._stop.set()
        if self._context_token is not None:
            _leases.reset(self._context_token)
        if self._thread:
            self._thread.join(timeout=1)
        if self._owned:
            try:
                self._request('release')
            except RemoteError:
                # The lease expires automatically. Do not replace a completed
                # mutation's result with a release error that invites a repeat.
                pass


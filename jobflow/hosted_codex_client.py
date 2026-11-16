"""Standalone, run-scoped client for a hosted Jobflow Codex workflow.

Download this file from the Jobflow origin and run it with Python 3.9 or newer.
The connection file is private (0600); credentials are never command arguments.
This client only moves Jobflow state and reviewed materials. It does not operate
an employer page, authorize new facts, or infer that an application was sent.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RESUME_BYTES = 5 * 1024 * 1024
MAX_CONNECTION_BYTES = 16 * 1024
RUNNER_OPS = frozenset(('codex_context', 'codex_claim', 'codex_report',
                       'codex_authorize', 'codex_answers', 'codex_check',
                       'codex_bind_destination'))
_IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')
_SHA256 = re.compile(r'^[a-f0-9]{64}$')
_TOKEN = re.compile(r'^[A-Za-z0-9_.-]{32,2048}$')
_SCOPE = ('workspace_id', 'batch_id', 'run_id')


class HostedClientError(ValueError):
    """An action did not yield a verified result; do not infer permission."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A capability must never be forwarded to a redirect target.
        return None


def _identifier(value, label):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise HostedClientError('Invalid %s in the hosted connection or response.' % label)
    return value


def _read_json(data, label):
    def invalid_constant(value):
        raise ValueError('Non-finite JSON number')

    try:
        value = json.loads(data.decode('utf-8'), parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise HostedClientError('%s must contain valid JSON.' % label) from None
    if not isinstance(value, dict):
        raise HostedClientError('%s must contain a JSON object.' % label)
    return value


def _private_file(path, limit):
    """Read only an owned regular file, without following its final symlink."""
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or
                    hasattr(os, 'getuid') and info.st_uid != os.getuid()):
                raise HostedClientError('The connection file must be an owned regular file with permissions 0600.')
            content = stream.read(limit + 1)
    except OSError:
        raise HostedClientError('The private connection file is unavailable or uses a symbolic link.') from None
    if len(content) > limit:
        raise HostedClientError('The connection file is too large.')
    return content


class HostedCodexClient:
    """Use a single run capability; never retry an uncertain state mutation."""

    def __init__(self, connection_path, *, allow_loopback=False, opener=None):
        self.connection_path = Path(connection_path).absolute()
        self._connection_bytes = _private_file(self.connection_path, MAX_CONNECTION_BYTES)
        self.connection = _read_json(self._connection_bytes, 'The connection file')
        if type(self.connection.get('version')) is not int or self.connection['version'] != 1:
            raise HostedClientError('Unsupported hosted connection version.')
        for field in _SCOPE:
            _identifier(self.connection.get(field), field)
        self.token = self.connection.get('token')
        if not isinstance(self.token, str) or not _TOKEN.fullmatch(self.token):
            raise HostedClientError('The hosted capability is missing or malformed.')
        self.origin = self._validate_origin(self.connection.get('origin'), allow_loopback)
        if self.connection.get('worker_id') is not None:
            _identifier(self.connection['worker_id'], 'worker_id')
        self.opener = opener if opener is not None else build_opener(_NoRedirect())

    @staticmethod
    def _validate_origin(value, allow_loopback):
        if not isinstance(value, str) or any(ord(char) < 33 for char in value):
            raise HostedClientError('Choose the exact HTTPS Jobflow origin.')
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise HostedClientError('Choose the exact HTTPS Jobflow origin.') from None
        local = (allow_loopback and parsed.scheme == 'http' and
                 parsed.hostname in ('127.0.0.1', 'localhost', '::1'))
        if (not parsed.hostname or parsed.username or parsed.password or
                parsed.path not in ('', '/') or parsed.query or parsed.fragment or
                parsed.scheme != 'https' and not local or
                port is not None and not 1 <= port <= 65535):
            raise HostedClientError('Choose the exact HTTPS Jobflow origin; HTTP requires explicit loopback development mode.')
        return value.rstrip('/')

    def _bind_worker(self, worker_id):
        worker_id = _identifier(worker_id, 'worker_id')
        existing = self.connection.get('worker_id')
        if existing and existing != worker_id:
            raise HostedClientError('This connection belongs to another worker. Resume the original Codex chat.')
        if existing:
            return
        # Save ownership before the first network call, including a lost reply.
        connection = {**self.connection, 'worker_id': worker_id}
        content = json.dumps(connection, ensure_ascii=False, indent=2).encode('utf-8') + b'\n'
        try:
            fd = os.open(str(self.connection_path), os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'r+b') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or
                        hasattr(os, 'getuid') and info.st_uid != os.getuid() or
                        stream.read(MAX_CONNECTION_BYTES + 1) != self._connection_bytes):
                    raise HostedClientError('The private connection changed. Read it again before connecting.')
                stream.seek(0)
                stream.write(content)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            raise HostedClientError('Could not retain the worker in the private connection file.') from None
        self.connection = connection
        self._connection_bytes = content

    def _fetch(self, path, *, data=None, maximum=MAX_RESPONSE_BYTES):
        request = Request(self.origin + path, data=data,
                          headers={'Authorization': 'Bearer ' + self.token,
                                   'Accept': 'application/json' if data is not None else 'application/octet-stream',
                                   **({'Content-Type': 'application/json'} if data is not None else {})},
                          method='POST' if data is not None else 'GET')
        try:
            with self.opener.open(request, timeout=60) as response:
                if response.geturl() != request.full_url or response.status != 200:
                    raise HostedClientError('The hosted endpoint returned an unexpected location or status.')
                content = response.read(maximum + 1)
        except HTTPError as error:
            detail = ''
            if error.code == 400:
                try:
                    body = error.read(4097)
                    value = json.loads(body.decode('utf-8')) if len(body) <= 4096 else None
                    message = value.get('error') if isinstance(value, dict) else None
                    if isinstance(message, str) and 0 < len(message) <= 1000 and self.token not in message:
                        detail = ' '.join(''.join(char if char.isprintable() else ' ' for char in message).split())
                except (OSError, ValueError, UnicodeError, RecursionError):
                    pass
            error.close()
            if 300 <= error.code < 400:
                raise HostedClientError('The hosted endpoint redirected; the capability was not forwarded.') from None
            if error.code in (401, 403):
                raise HostedClientError('Hosted access was denied or expired. Return to the batch for its connection status.') from None
            if error.code == 409:
                raise HostedClientError('The hosted run changed or belongs to another worker. Refresh its status in the original chat.') from None
            if error.code == 400 and detail:
                raise HostedClientError('The hosted request was rejected: ' + detail) from None
            raise HostedClientError('The hosted request failed (HTTP %s). Its outcome may be unknown; do not repeat a submission or click grant.' % error.code) from None
        except (URLError, TimeoutError, OSError):
            raise HostedClientError('The hosted response was lost or unavailable. Its outcome may be unknown; do not repeat a submission or click grant. Reconnect with the same worker to inspect progress.') from None
        if len(content) > maximum:
            raise HostedClientError('The hosted response exceeded its size limit. No result or permission was accepted.')
        return content

    def connect(self, worker_id):
        self._bind_worker(worker_id)
        return self.request({'op': 'codex_context'})

    def request(self, payload):
        if not isinstance(payload, dict) or payload.get('op') not in RUNNER_OPS:
            raise HostedClientError('Choose a supported hosted Codex runner operation.')
        worker = self.connection.get('worker_id')
        if not worker:
            raise HostedClientError('Connect once with --connect WORKER_ID before changing a run.')
        scope = {field: self.connection[field] for field in _SCOPE}
        scope['worker_id'] = worker
        for field, value in scope.items():
            if field in payload and payload[field] != value:
                raise HostedClientError('The request belongs to a different %s.' % field)
        try:
            data = json.dumps({**payload, **scope}, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (ValueError, TypeError, RecursionError):
            raise HostedClientError('The request must contain valid JSON values.') from None
        if len(data) > MAX_REQUEST_BYTES:
            raise HostedClientError('The hosted request is too large.')
        result = _read_json(self._fetch('/api/codex/runner', data=data), 'The hosted response')
        if result.get('ok') is not True or any(result.get(field) != value for field, value in scope.items() if field != 'worker_id'):
            raise HostedClientError('The hosted response did not confirm this exact workspace, batch, and run.')
        if any(field in result and type(result[field]) is not bool for field in ('can_click', 'can_fill', 'can_submit')):
            raise HostedClientError('The hosted response contained an invalid permission value.')
        if payload['op'] == 'codex_check' and type(result.get('can_click')) is not bool:
            raise HostedClientError('The hosted check did not return an explicit click decision.')
        if self.token in json.dumps(result, ensure_ascii=False):
            raise HostedClientError('The hosted response unexpectedly contained a credential; it was withheld.')
        if 'run' in result:
            run = result['run']
            if not isinstance(run, dict) or run.get('id') != self.connection['run_id']:
                raise HostedClientError('The response identifies a different Codex run.')
            if run.get('worker_id', worker) != worker:
                raise HostedClientError('The response identifies a different Codex worker.')
        if 'batch' in result and (not isinstance(result['batch'], dict) or result['batch'].get('id') != self.connection['batch_id']):
            raise HostedClientError('The response identifies a different application batch.')
        tasks = result.get('tasks', [])
        if not isinstance(tasks, list) or len(tasks) > 100:
            raise HostedClientError('The response contains an invalid task list.')
        if result.get('task') is not None:
            tasks = [*tasks, result['task']]
        for task in tasks:
            self._materialize_task(task)
        return result

    def _materialize_task(self, task):
        if (not isinstance(task, dict) or task.get('run_id') != self.connection['run_id'] or
                task.get('batch_id') != self.connection['batch_id']):
            raise HostedClientError('The response contains a task outside the connected run.')
        _identifier(task.get('job_id'), 'job_id')
        if task.get('resume_path') is not None:
            raise HostedClientError('The server supplied an untrusted local resume path.')
        download = task.get('resume_download')
        if download is None:
            unavailable = task.get('resume_unavailable_reason')
            if unavailable not in (None, 'batch_paused', 'batch_finished'):
                raise HostedClientError('The hosted task has an unknown resume access restriction.')
            if unavailable is None and task.get('status') not in ('submitted', 'skipped', 'submitting', 'uncertain'):
                raise HostedClientError('The hosted task has no verified resume download.')
            task['resume_path'] = None
            return
        if task.get('resume_unavailable_reason') is not None:
            raise HostedClientError('The hosted task returned conflicting resume access instructions.')
        if not isinstance(download, dict):
            raise HostedClientError('The hosted resume download is invalid.')
        resume_id = _identifier(download.get('id'), 'resume ID')
        filename, size, digest = download.get('filename'), download.get('size'), download.get('sha256')
        if (not isinstance(filename, str) or not filename or len(filename) > 255 or
                filename in ('.', '..') or '/' in filename or '\\' in filename or
                any(ord(char) < 32 or ord(char) == 127 for char in filename) or
                type(size) is not int or not 0 < size <= MAX_RESUME_BYTES or
                not isinstance(digest, str) or not _SHA256.fullmatch(digest) or
                download.get('url') != '/api/codex/resumes/' + resume_id):
            raise HostedClientError('The hosted resume metadata or download address is invalid.')
        resume = task.get('resume')
        if not isinstance(resume, dict) or any(resume.get(key) != download[key] for key in ('id', 'filename', 'size', 'sha256')):
            raise HostedClientError('The download does not match the reviewed resume.')
        content = self._fetch(download['url'], maximum=MAX_RESUME_BYTES)
        if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
            raise HostedClientError('The downloaded resume failed its size or SHA-256 check. Do not upload it.')
        task['resume_path'] = self._cache_resume(resume_id, filename, content)

    def _cache_resume(self, resume_id, filename, content):
        # Never accept any server directory. Descend only fixed/private local
        # directories and validated identifiers, using no-follow descriptors.
        parts = ('materials', self.connection['run_id'], resume_id)
        descriptor = None
        try:
            descriptor = os.open(str(self.connection_path.parent), os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
            for part in parts:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                info = os.fstat(descriptor)
                if info.st_mode & 0o077 or hasattr(os, 'getuid') and info.st_uid != os.getuid():
                    raise HostedClientError('The resume cache must contain private, owned directories.')
            try:
                target = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600, dir_fd=descriptor)
            except FileExistsError:
                target = os.open(filename, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), dir_fd=descriptor)
                with os.fdopen(target, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or
                            hasattr(os, 'getuid') and info.st_uid != os.getuid() or
                            stream.read(MAX_RESUME_BYTES + 1) != content):
                        raise HostedClientError('The cached resume changed or is not private. Do not upload it.')
            else:
                with os.fdopen(target, 'wb') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
        except OSError:
            raise HostedClientError('The private resume cache is unavailable or uses a symbolic link.') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return str(self.connection_path.parent.joinpath(*parts, filename))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Connect Codex to one prepared hosted Jobflow batch.')
    parser.add_argument('--connection', required=True, type=Path, help='Private 0600 connection JSON file')
    parser.add_argument('--allow-loopback', action='store_true', help='Allow HTTP only on loopback for explicit local development')
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument('--connect', metavar='WORKER_ID', help='Read context and retain this chat\'s unique worker ID')
    operation.add_argument('--request', metavar='FILE', help='Runner-operation JSON file, or - for standard input')
    args = parser.parse_args(argv)
    try:
        client = HostedCodexClient(args.connection, allow_loopback=args.allow_loopback)
        if args.connect:
            result = client.connect(args.connect)
        else:
            if args.request == '-':
                content = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
            else:
                with open(args.request, 'rb') as stream:
                    content = stream.read(MAX_REQUEST_BYTES + 1)
            if len(content) > MAX_REQUEST_BYTES:
                raise HostedClientError('The hosted request is too large.')
            result = client.request(_read_json(content, 'The request file'))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (HostedClientError, OSError) as error:
        # OS errors can expose local paths; connection and response parsing must
        # never dump a capability, response body, or request body on failure.
        message = str(error) if isinstance(error, HostedClientError) else 'A required local file could not be accessed.'
        print('Jobflow hosted runner: ' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

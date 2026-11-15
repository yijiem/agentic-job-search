"""Hosted runner transport, scope, private files, and ambiguous outcomes."""
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError, URLError

from jobflow.hosted_codex_client import HostedCodexClient, HostedClientError, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, MAX_RESUME_BYTES


class Response(io.BytesIO):
    status = 200

    def __init__(self, content, url):
        super().__init__(content)
        self.url = url

    def geturl(self):
        return self.url


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, dict):
            response = json.dumps(response).encode()
        return Response(response, request.full_url)


class HostedClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = self.root / 'connection.json'
        self.connection = {'version': 1, 'origin': 'https://jobs.example.com',
                           'token': 'batch-account.' + 'a' * 43,
                           'workspace_id': 'workspace-1', 'batch_id': 'batch-1', 'run_id': 'run-1'}
        self.write_connection()
        self.content = b'%PDF-1.4\nPrivate candidate resume\n'
        self.resume = {'id': 'resume-1', 'filename': 'Candidate.pdf', 'size': len(self.content),
                       'sha256': hashlib.sha256(self.content).hexdigest()}
        self.task = {'batch_id': 'batch-1', 'run_id': 'run-1', 'job_id': 'job-1', 'status': 'pending',
                     'resume': self.resume, 'resume_download': {**self.resume, 'url': '/api/codex/resumes/resume-1'}}

    def tearDown(self):
        self.directory.cleanup()

    def write_connection(self):
        self.path.write_text(json.dumps(self.connection))
        self.path.chmod(0o600)

    def result(self, **kwargs):
        return {'ok': True, 'workspace_id': 'workspace-1', 'batch_id': 'batch-1', 'run_id': 'run-1', **kwargs}

    def test_connect_downloads_only_verified_material_and_retains_private_worker(self):
        opener = Opener([self.result(tasks=[self.task]), self.content, self.result(can_click=False)])
        client = HostedCodexClient(self.path, opener=opener)
        result = client.connect('worker-a')
        stored = Path(result['tasks'][0]['resume_path'])
        self.assertEqual(stored.read_bytes(), self.content)
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        self.assertEqual(stored.parent.stat().st_mode & 0o777, 0o700)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['worker_id'], 'worker-a')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        first = json.loads(opener.requests[0].data)
        self.assertEqual(first, {'op': 'codex_context', 'worker_id': 'worker-a', 'workspace_id': 'workspace-1',
                                 'batch_id': 'batch-1', 'run_id': 'run-1'})
        for request in opener.requests:
            self.assertEqual(request.get_header('Authorization'), 'Bearer ' + saved['token'])
            self.assertIsNone(request.get_header('Cookie'))
            self.assertNotIn(saved['token'], request.full_url)
        client.request({'op': 'codex_check', 'job_id': 'job-1', 'phase': 'before_click'})
        self.assertEqual(json.loads(opener.requests[-1].data)['worker_id'], 'worker-a')
        self.assertNotIn(saved['token'], json.dumps(result))

    def test_request_scope_cannot_be_switched_or_missing(self):
        opener = Opener([self.result()])
        client = HostedCodexClient(self.path, opener=opener)
        with self.assertRaisesRegex(HostedClientError, 'Connect once'):
            client.request({'op': 'codex_claim'})
        client.connect('worker-a')
        for field in ('workspace_id', 'batch_id', 'run_id', 'worker_id'):
            with self.subTest(field=field), self.assertRaisesRegex(HostedClientError, 'different'):
                client.request({'op': 'codex_claim', field: 'different'})
        for op in ('upload_resume', 'codex_prepare', 'codex_discard', 'delete', None):
            with self.subTest(op=op), self.assertRaises(HostedClientError):
                client.request({'op': op})
        self.assertEqual(len(opener.requests), 1)

    def test_response_scope_is_required_before_resumes_are_requested(self):
        for field in ('workspace_id', 'batch_id', 'run_id'):
            result = self.result(tasks=[self.task])
            result[field] = 'another'
            opener = Opener([result])
            with self.subTest(field=field), self.assertRaisesRegex(HostedClientError, 'exact workspace'):
                HostedCodexClient(self.path, opener=opener).connect('worker-a')
            self.assertEqual(len(opener.requests), 1)
        for changed in ({'run': {'id': 'another'}}, {'batch': {'id': 'another'}},
                        {'task': {**self.task, 'run_id': 'another'}}):
            with self.subTest(changed=changed), self.assertRaises(HostedClientError):
                HostedCodexClient(self.path, opener=Opener([self.result(**changed)])).connect('worker-a')

    def test_lost_response_does_not_retry_and_worker_survives(self):
        opener = Opener([URLError('remote unavailable ' + self.connection['token'])])
        with self.assertRaisesRegex(HostedClientError, 'outcome may be unknown') as caught:
            HostedCodexClient(self.path, opener=opener).connect('worker-a')
        self.assertNotIn(self.connection['token'], str(caught.exception))
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(json.loads(self.path.read_text())['worker_id'], 'worker-a')
        with self.assertRaisesRegex(HostedClientError, 'another worker'):
            HostedCodexClient(self.path, opener=Opener([])).connect('worker-b')
        resumed = HostedCodexClient(self.path, opener=Opener([self.result()]))
        self.assertTrue(resumed.connect('worker-a')['ok'])

    def test_resume_integrity_failure_never_writes_a_file(self):
        for content in (b'changed', self.content[:-1], self.content + b'extra'):
            with self.subTest(content=content), self.assertRaisesRegex(HostedClientError, 'SHA-256'):
                HostedCodexClient(self.path, opener=Opener([self.result(task=self.task), content])).connect('worker-a')
        self.assertFalse((self.root / 'materials').exists())

    def test_resume_address_and_name_are_exact_and_run_scoped(self):
        for key, value in (('url', 'https://evil.example/resume'), ('url', '//evil.example/resume'),
                           ('url', '/api/codex/resumes/resume-1?token=x'), ('filename', '../resume.pdf'),
                           ('filename', 'resume\\evil.pdf'), ('filename', '.'), ('filename', 'x\x00.pdf'),
                           ('id', '../resume'), ('sha256', 'abc'), ('size', True)):
            task = copy.deepcopy(self.task)
            task['resume_download'][key] = value
            opener = Opener([self.result(task=task)])
            with self.subTest(key=key, value=value), self.assertRaises(HostedClientError):
                HostedCodexClient(self.path, opener=opener).connect('worker-a')
            self.assertEqual(len(opener.requests), 1)
        task = copy.deepcopy(self.task)
        task['resume']['id'] = 'different-reviewed-resume'
        with self.assertRaisesRegex(HostedClientError, 'reviewed resume'):
            HostedCodexClient(self.path, opener=Opener([self.result(task=task)])).connect('worker-a')

    def test_server_paths_are_not_used_and_terminal_tasks_have_no_upload_path(self):
        task = {**self.task, 'resume_path': '/server/private.pdf'}
        with self.assertRaisesRegex(HostedClientError, 'untrusted local'):
            HostedCodexClient(self.path, opener=Opener([self.result(task=task)])).connect('worker-a')
        task = {key: value for key, value in self.task.items() if key != 'resume_download'}
        with self.assertRaisesRegex(HostedClientError, 'no verified resume'):
            HostedCodexClient(self.path, opener=Opener([self.result(task=task)])).connect('worker-a')
        task['status'] = 'submitted'
        result = HostedCodexClient(self.path, opener=Opener([self.result(task=task)])).connect('worker-a')
        self.assertIsNone(result['task']['resume_path'])
        task.update(status='running', resume_unavailable_reason='batch_paused')
        result = HostedCodexClient(self.path, opener=Opener([self.result(task=task)])).connect('worker-a')
        self.assertIsNone(result['task']['resume_path'])
        task['resume_unavailable_reason'] = 'anything_else'
        with self.assertRaisesRegex(HostedClientError, 'unknown resume access'):
            HostedCodexClient(self.path, opener=Opener([self.result(task=task)])).connect('worker-a')

    def test_cached_tampering_and_symlinks_are_not_overwritten(self):
        def fetch():
            return HostedCodexClient(self.path, opener=Opener([self.result(task=self.task), self.content])).connect('worker-a')
        path = Path(fetch()['task']['resume_path'])
        self.assertEqual(Path(fetch()['task']['resume_path']), path)
        path.write_bytes(b'user-edited material')
        with self.assertRaisesRegex(HostedClientError, 'cached resume changed'):
            fetch()
        self.assertEqual(path.read_bytes(), b'user-edited material')
        path.unlink()
        target = self.root / 'outside.pdf'
        target.write_bytes(b'outside')
        path.symlink_to(target)
        with self.assertRaisesRegex(HostedClientError, 'symbolic link'):
            fetch()
        self.assertEqual(target.read_bytes(), b'outside')

    def test_cache_directory_symlink_is_rejected(self):
        target = self.root / 'outside'
        target.mkdir()
        (self.root / 'materials').symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(HostedClientError, 'symbolic link'):
            HostedCodexClient(self.path, opener=Opener([self.result(task=self.task), self.content])).connect('worker-a')
        self.assertEqual(list(target.iterdir()), [])

    def test_private_connection_is_not_world_readable_or_a_symlink(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(HostedClientError, '0600'):
            HostedCodexClient(self.path)
        self.path.chmod(0o600)
        symlink = self.root / 'linked.json'
        symlink.symlink_to(self.path)
        with self.assertRaisesRegex(HostedClientError, 'symbolic link'):
            HostedCodexClient(symlink)

    def test_origin_requires_https_and_loopback_requires_explicit_flag(self):
        for origin in ('http://jobs.example.com', 'https://user:pass@jobs.example.com',
                       'https://jobs.example.com/api', 'https://jobs.example.com?secret=x',
                       'https://jobs.example.com#part', 'https://jobs.example.com\n'):
            self.connection['origin'] = origin
            self.write_connection()
            with self.subTest(origin=origin), self.assertRaises(HostedClientError):
                HostedCodexClient(self.path, allow_loopback=True)
        self.connection['origin'] = 'http://127.0.0.1:1234'
        self.write_connection()
        with self.assertRaises(HostedClientError):
            HostedCodexClient(self.path)
        self.assertEqual(HostedCodexClient(self.path, allow_loopback=True).origin, self.connection['origin'])

    def test_redirect_never_receives_the_capability(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.path)
                self.send_response(307)
                self.send_header('Location', '/redirect-target')
                self.end_headers()

            def do_GET(self):
                received.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.connection['origin'] = 'http://127.0.0.1:%d' % server.server_port
            self.write_connection()
            with self.assertRaisesRegex(HostedClientError, 'not forwarded'):
                HostedCodexClient(self.path, allow_loopback=True).connect('worker-a')
            self.assertEqual(received, ['/api/codex/runner'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_http_errors_and_response_bodies_do_not_disclose_credentials(self):
        for code in (401, 403, 409, 500):
            error = HTTPError(self.connection['origin'], code, self.connection['token'], {},
                              io.BytesIO(self.connection['token'].encode()))
            with self.subTest(code=code), self.assertRaises(HostedClientError) as caught:
                HostedCodexClient(self.path, opener=Opener([error])).connect('worker-a')
            self.assertNotIn(self.connection['token'], str(caught.exception))
        result = self.result(message=self.connection['token'])
        with self.assertRaisesRegex(HostedClientError, 'credential') as caught:
            HostedCodexClient(self.path, opener=Opener([result])).connect('worker-a')
        self.assertNotIn(self.connection['token'], str(caught.exception))
        with self.assertRaisesRegex(HostedClientError, 'valid JSON'):
            HostedCodexClient(self.path, opener=Opener([self.connection['token'].encode()])).connect('worker-a')
        error = HTTPError(self.connection['origin'], 400, 'Bad Request', {},
                          io.BytesIO(json.dumps({'error': 'Inspect this form before authorizing it.'}).encode()))
        with self.assertRaisesRegex(HostedClientError, 'Inspect this form'):
            HostedCodexClient(self.path, opener=Opener([error])).connect('worker-a')
        error = HTTPError(self.connection['origin'], 400, 'Bad Request', {},
                          io.BytesIO(json.dumps({'error': self.connection['token']}).encode()))
        with self.assertRaises(HostedClientError) as caught:
            HostedCodexClient(self.path, opener=Opener([error])).connect('worker-a')
        self.assertNotIn(self.connection['token'], str(caught.exception))

    def test_request_limit_and_nonfinite_values_fail_before_transport(self):
        opener = Opener([self.result()])
        client = HostedCodexClient(self.path, opener=opener)
        client.connect('worker-a')
        with self.assertRaisesRegex(HostedClientError, 'too large'):
            client.request({'op': 'codex_answers', 'answers': {'long': 'x' * MAX_REQUEST_BYTES}})
        with self.assertRaisesRegex(HostedClientError, 'valid JSON'):
            client.request({'op': 'codex_answers', 'answers': {'number': float('nan')}})
        self.assertEqual(len(opener.requests), 1)

    def test_response_limits_and_non_boolean_permissions_fail_closed(self):
        with self.assertRaisesRegex(HostedClientError, 'size limit'):
            HostedCodexClient(self.path, opener=Opener([b' ' * (MAX_RESPONSE_BYTES + 1)])).connect('worker-a')
        with self.assertRaisesRegex(HostedClientError, 'size limit'):
            HostedCodexClient(self.path, opener=Opener([self.result(task=self.task), b'x' * (MAX_RESUME_BYTES + 1)])).connect('worker-a')
        for value in ('true', 1, {}, None):
            with self.subTest(value=value), self.assertRaisesRegex(HostedClientError, 'invalid permission'):
                HostedCodexClient(self.path, opener=Opener([self.result(can_click=value)])).connect('worker-a')
        client = HostedCodexClient(self.path, opener=Opener([self.result()]))
        with self.assertRaisesRegex(HostedClientError, 'explicit click decision'):
            client.request({'op': 'codex_check'})
        with self.assertRaisesRegex(HostedClientError, 'valid JSON'):
            HostedCodexClient(self.path, opener=Opener([b'{"ok":true,"unknown":NaN}'])).connect('worker-a')

    def test_fifo_connection_is_rejected_without_blocking(self):
        fifo = self.root / 'connection-pipe'
        os.mkfifo(fifo, 0o600)
        with self.assertRaisesRegex(HostedClientError, 'regular file'):
            HostedCodexClient(fifo)

    def test_downloaded_script_runs_without_the_repository(self):
        source = Path(__file__).resolve().parents[1] / 'jobflow/hosted_codex_client.py'
        standalone = self.root / 'runner.py'
        standalone.write_bytes(source.read_bytes())
        result = subprocess.run([sys.executable, str(standalone), '--help'], cwd=self.root,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--connection', result.stdout)
        self.assertIn('--request', result.stdout)

    def test_real_http_client_connects_downloads_and_updates_the_same_hosted_batch(self):
        # Exercise the actual transport and server, using only an isolated local
        # account fixture. No deployed account or employer page is contacted.
        from test_hosted_codex_http import HostedCodexHTTPTests
        fixture = HostedCodexHTTPTests()
        fixture.setUp()
        try:
            prepared = fixture.prepare()
            local_origin = 'http://127.0.0.1:%d' % fixture.server.server_address[1]
            self.connection = {**prepared['connection'], 'origin': local_origin}
            self.write_connection()
            fixture.server.origin = local_origin
            client = HostedCodexClient(self.path, allow_loopback=True)
            context = client.connect('client-contract-worker')
            self.assertEqual(context['run']['id'], prepared['run']['id'])
            self.assertEqual(Path(context['tasks'][0]['resume_path']).read_bytes(), fixture.content)
            task = client.request({'op': 'codex_claim'})['task']
            self.assertEqual(task['mode'], 'inspect')
            client.request({'op': 'codex_report', 'job_id': task['job_id'], 'attempt_id': task['attempt_id'],
                'status': 'inspected', 'observed_url': task['url'],
                'fields': [{'key': 'name', 'label': 'Name', 'type': 'text', 'required': True, 'supported': True},
                           {'key': 'resume', 'label': 'Resume', 'type': 'file', 'required': True, 'supported': True}],
                'planned_answers': {'name': 'Ada Example', 'resume': fixture.resume['filename']}})
            fixture.server.origin = fixture.origin
            current = fixture.current()
            self.assertEqual(current['items'][0]['status'], 'ready')
            self.assertEqual(current['items'][1]['status'], 'pending')
            fixture.owner('pause', batch_id=current['id'], revision=current['revision'])
            fixture.server.origin = local_origin
            paused = client.connect('client-contract-worker')
            self.assertEqual(paused['batch']['status'], 'paused')
            self.assertIsNone(paused['tasks'][0]['resume_path'])
            self.assertEqual(paused['tasks'][0]['resume_unavailable_reason'], 'batch_paused')
        finally:
            fixture.tearDown()
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()

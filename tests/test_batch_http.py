"""The batch transport keeps CLI locking, CSRF and account boundaries intact."""
import base64
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from jobflow.storage import Store
from jobflow.web import JobflowServer, MAX_REQUEST_BYTES, browser_worker_archive


class BatchHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        project = Path(__file__).resolve().parents[1]
        for name in ('settings', 'profile'):
            shutil.copyfile(project / 'config' / (name + '.example.json'),
                            self.root / 'config' / (name + '.json'))
        self.store = Store(self.root / 'data/jobflow.sqlite3')
        self.store.put('jobs', 'test-role', {'id': 'test-role', 'title': 'Senior Engineer',
            'company': 'Example', 'url': 'https://jobs.example.com/test-role',
            'availability': 'open', 'eligible': True, 'score': 90})
        self.server = JobflowServer(('127.0.0.1', 0), self.root)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.temp.cleanup()

    def request(self, method, path, payload=None, token=None, headers=None):
        actual_headers = {'Content-Type': 'application/json'}
        if token:
            actual_headers['X-CSRF-Token'] = token
        actual_headers.update(headers or {})
        conn = HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=15)
        conn.request(method, path, json.dumps(payload) if payload is not None else None, actual_headers)
        response = conn.getresponse()
        data = response.read()
        status, mime = response.status, response.getheader('Content-Type', '')
        conn.close()
        return status, json.loads(data) if mime.startswith('application/json') else data

    def test_resume_upload_and_batch_use_cli_and_read_projection(self):
        status, initial = self.request('GET', '/api/batches')
        self.assertEqual(status, 200)
        self.assertEqual(initial['batches'], [])
        token = initial['csrf_token']
        # A real upload larger than the ordinary JSON bound must survive CLI
        # transport without leaking file bytes into the list API.
        content = b'Fixture resume evidence.\n' * 14000
        payload = {'action': 'batch', 'op': 'upload_resume', 'filename': 'fixture.txt',
                   'content_base64': base64.b64encode(content).decode('ascii')}
        self.assertGreater(len(json.dumps(payload)), MAX_REQUEST_BYTES)
        self.assertEqual(self.request('POST', '/api/action', payload)[0], 403)
        status, uploaded = self.request('POST', '/api/action', payload, token)
        self.assertEqual(status, 200, uploaded)
        resume_id = uploaded['resume']['id']
        status, created = self.request('POST', '/api/action', {
            'action': 'batch', 'op': 'create', 'name': 'Fixture batch',
            'job_ids': ['test-role'], 'resume_id': resume_id,
            'applicant': {'first_name': 'Test', 'last_name': 'Candidate', 'email': 'test@example.com'},
        }, token)
        self.assertEqual(status, 200, created)
        batch_id = created['batch']['id']
        status, state = self.request('GET', '/api/batches')
        self.assertEqual(status, 200)
        self.assertEqual(len(state['batches']), 1)
        self.assertNotIn('content_base64', json.dumps(state))
        self.assertNotIn(str(self.root), json.dumps(state))
        self.assertEqual(self.request('GET', '/api/batches/' + batch_id)[0], 200)
        self.assertEqual(self.store.all('applications'), [], 'Preparing is not applying')
        self.assertEqual(list((self.root / 'inputs/web').iterdir()), [], 'CLI payloads must be removed')

    def test_oversized_non_upload_and_workspace_change_are_rejected(self):
        unused, state = self.request('GET', '/api/batches')
        token = state['csrf_token']
        status, unused = self.request('POST', '/api/action', {'action': 'prep',
            'notes': 'x' * MAX_REQUEST_BYTES}, token)
        self.assertEqual(status, 413)
        with patch('jobflow.web._run_cli') as cli:
            status, result = self.request('POST', '/api/action', {'action': 'batch', 'op': 'claim',
                'worker_id': 'test-worker', 'workspace_id': 'different-workspace'}, token)
        self.assertEqual(status, 409, result)
        cli.assert_not_called()

    def test_resume_library_review_and_application_defaults(self):
        status, library = self.request('GET', '/api/resumes')
        self.assertEqual(status, 200, library)
        token = library['csrf_token']
        original = b'Test Candidate\nSkills\nPython\nExperience\n- Built a Python service.\n'
        status, uploaded = self.request('POST', '/api/action', {
            'action': 'batch', 'op': 'upload_resume', 'filename': 'original.txt',
            'content_base64': base64.b64encode(original).decode('ascii')}, token)
        self.assertEqual(status, 200, uploaded)
        resume_id = uploaded['resume']['id']
        status, saved = self.request('POST', '/api/action', {
            'action': 'batch', 'op': 'resume_profile',
            'applicant': {'first_name': 'Test', 'last_name': 'Candidate', 'email': 'test@example.com'},
            'default_resume_id': resume_id}, token)
        self.assertEqual(status, 200, saved)
        status, draft = self.request('POST', '/api/action', {
            'action': 'batch', 'op': 'resume_draft', 'resume_id': resume_id, 'job_id': 'test-role'}, token)
        self.assertEqual(status, 200, draft)
        request = {'action': 'batch', 'op': 'resume_save_version', 'draft_id': draft['draft']['id'],
                   'text': draft['draft']['text'], 'label': 'Reviewed role version'}
        self.assertEqual(self.request('POST', '/api/action', request, token)[0], 400)
        status, version = self.request('POST', '/api/action', {**request, 'confirmed': True}, token)
        self.assertEqual(status, 200, version)
        self.assertNotEqual(version['resume']['id'], resume_id)
        self.assertEqual(self.request('GET', '/api/application-resumes/' + resume_id)[1], original)
        self.assertEqual(self.request('GET', '/api/resumes/' + resume_id)[0], 200)
        status, batch = self.request('POST', '/api/action', {
            'action': 'batch', 'op': 'create', 'name': 'Use saved materials',
            'job_ids': ['test-role']}, token)
        self.assertEqual(status, 200, batch)
        self.assertIn(resume_id, json.dumps(batch))
        self.assertIn('test@example.com', json.dumps(batch))
        self.assertEqual(self.store.all('applications'), [])
        self.assertEqual(list((self.root / 'inputs/web').iterdir()), [])

    def test_worker_read_and_write_keep_existing_origin_boundary(self):
        self.assertEqual(self.request('GET', '/api/batches', headers={'Origin': 'https://evil.example'})[0], 403)
        unused, state = self.request('GET', '/api/batches')
        status, data = self.request('POST', '/api/action', {'action': 'batch', 'op': 'claim',
            'worker_id': 'test-worker', 'workspace_id': state['workspace_id']}, state['csrf_token'])
        self.assertEqual(status, 200, data)
        self.assertIsNone(data['task'])

    def test_worker_reload_protocol_requires_csrf_and_the_connected_workspace(self):
        status, state = self.request('GET', '/api/batches')
        self.assertEqual(status, 200)
        archive = zipfile.ZipFile(io.BytesIO(browser_worker_archive()))
        version = json.loads(archive.read('jobflow-browser-worker/manifest.json'))['version']
        self.assertEqual(state['worker_release']['version'], version)
        payload = {'action': 'batch', 'op': 'worker_heartbeat', 'worker_id': 'http-worker',
                   'workspace_id': state['workspace_id'], 'version': version,
                   'reload_supported': True, 'enabled': False, 'active': False}
        self.assertEqual(self.request('POST', '/api/action', payload)[0], 403)
        self.assertEqual(self.request('POST', '/api/action', payload, state['csrf_token'],
                                      {'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('POST', '/api/action', {**payload, 'workspace_id': 'wrong'}, state['csrf_token'])[0], 409)
        self.assertEqual(self.request('POST', '/api/action', {k: v for k, v in payload.items() if k != 'workspace_id'}, state['csrf_token'])[0], 400)
        status, heartbeat = self.request('POST', '/api/action', payload, state['csrf_token'])
        self.assertEqual(status, 200, heartbeat)
        request = {'action': 'batch', 'op': 'worker_reload', 'worker_id': 'http-worker', 'workspace_id': state['workspace_id']}
        self.assertEqual(self.request('POST', '/api/action', request)[0], 403)
        status, queued = self.request('POST', '/api/action', request, state['csrf_token'])
        self.assertEqual(status, 200, queued)
        control = queued['reload_request']
        ack = {**request, 'op': 'worker_reload_ack', 'request_id': control['id']}
        self.assertEqual(self.request('POST', '/api/action', ack)[0], 403)
        status, acked = self.request('POST', '/api/action', ack, state['csrf_token'])
        self.assertEqual(status, 200, acked)
        self.assertEqual(acked['reload_request']['status'], 'reloading')
        status, completed = self.request('POST', '/api/action', {**payload, 'reload_completed_id': control['id']}, state['csrf_token'])
        self.assertEqual(status, 200, completed)
        self.assertEqual(completed['reload_request']['status'], 'complete')
        self.assertEqual(self.request('GET', '/api/batches')[1]['worker']['reload_request']['status'], 'complete')
        self.assertEqual(self.store.all('applications'), [])
        self.assertEqual(list((self.root / 'inputs/web').iterdir()), [])

    def test_pdf_review_transport_persists_annotations_and_separate_revision(self):
        unused, initial = self.request('GET', '/api/resumes')
        token = initial['csrf_token']
        original = b'%PDF-1.4\nfixture original\n%%EOF\n'
        status, uploaded = self.request('POST', '/api/action', {
            'action': 'batch', 'op': 'upload_resume', 'filename': 'review.pdf',
            'content_base64': base64.b64encode(original).decode('ascii')}, token)
        self.assertEqual(status, 200, uploaded)
        resume_id = uploaded['resume']['id']
        text = 'Candidate\nBuilt reliable systems.'
        document = {'text': text, 'pages': [{'number': 1, 'width': 612, 'height': 792}],
                    'lines': [{'page': 1, 'start': 0, 'end': 9, 'text': 'Candidate', 'rect': [.1, .1, .2, .03]},
                              {'page': 1, 'start': 10, 'end': len(text), 'text': text[10:], 'rect': [.1, .2, .6, .03]}]}
        status, opened = self.request('POST', '/api/action', {
            'action': 'batch', 'op': 'pdf_review_create', 'resume_id': resume_id,
            'job_id': 'test-role', 'document': document}, token)
        self.assertEqual(status, 200, opened)
        review = opened['review']
        self.assertEqual(self.request('GET', '/api/resume-reviews/' + review['id'])[0], 200)
        annotation = {'kind': 'replace', 'start': 10, 'end': len(text), 'before': text[10:],
                      'after': 'Built and maintained reliable systems.', 'page': 1,
                      'rects': [[.1, .2, .6, .03]], 'status': 'accepted', 'source': 'manual'}
        status, edited = self.request('POST', '/api/action', {
            'action': 'batch', 'op': 'pdf_review_update', 'review_id': review['id'],
            'revision': review['revision'], 'annotations': [annotation]}, token)
        self.assertEqual(status, 200, edited)
        review = edited['review']
        revision_pdf = b'%PDF-1.4\nfixture revision\n%%EOF\n'
        status, saved = self.request('POST', '/api/action', {
            'action': 'batch', 'op': 'pdf_review_save', 'review_id': review['id'],
            'revision': review['revision'], 'text': review['resolved_text'], 'confirmed': True,
            'content_base64': base64.b64encode(revision_pdf).decode('ascii'), 'label': 'Reviewed PDF'}, token)
        self.assertEqual(status, 200, saved)
        self.assertNotEqual(saved['resume']['id'], resume_id)
        self.assertEqual(saved['resume']['parent_resume_id'], resume_id)
        self.assertEqual(self.request('GET', '/api/application-resumes/' + resume_id)[1], original)
        self.assertEqual(self.request('GET', '/api/application-resumes/' + saved['resume']['id'])[1], revision_pdf)
        self.assertEqual(self.store.all('applications'), [])

    def test_archive_is_extension_source_only(self):
        archive = zipfile.ZipFile(io.BytesIO(browser_worker_archive()))
        names = archive.namelist()
        self.assertIn('jobflow-browser-worker/manifest.json', names)
        self.assertTrue(all(name.startswith('jobflow-browser-worker/') for name in names))
        self.assertFalse(any(part in name for name in names for part in ('inputs/', 'data/', '.env', 'config/')))
        manifest = json.loads(archive.read('jobflow-browser-worker/manifest.json'))
        self.assertEqual(manifest['manifest_version'], 3)
        self.assertFalse(manifest.get('host_permissions'), 'Access is granted per origin when starting')
        status, downloaded = self.request('GET', '/browser-worker.zip')
        self.assertEqual(status, 410)
        self.assertIn('retired', downloaded['error'])

    def test_codex_handoff_is_explicit_local_and_keeps_agent_actions_private(self):
        from urllib.parse import parse_qs, urlsplit
        status, state = self.request('GET', '/api/batches')
        self.assertEqual(status, 200)
        self.assertTrue(state['codex']['available'])
        token = state['csrf_token']
        status, uploaded = self.request('POST', '/api/action', {
            'action': 'batch', 'op': 'upload_resume', 'filename': 'candidate.txt',
            'content_base64': base64.b64encode(b'Candidate professional experience.').decode('ascii')}, token)
        self.assertEqual(status, 200, uploaded)
        status, created = self.request('POST', '/api/action', {
            'action': 'batch', 'op': 'create', 'name': 'Codex batch',
            'job_ids': ['test-role'], 'resume_id': uploaded['resume']['id'],
            'applicant': {'first_name': 'Private', 'email': 'private@example.com'}}, token)
        self.assertEqual(status, 200, created)
        payload = {'action': 'batch', 'op': 'codex_prepare', 'batch_id': created['batch']['id'],
                   'revision': created['batch']['revision'], 'workspace_id': state['workspace_id']}
        self.assertEqual(self.request('POST', '/api/action', payload)[0], 403)
        status, prepared = self.request('POST', '/api/action', payload, token)
        self.assertEqual(status, 200, prepared)
        self.assertEqual(prepared['run']['status'], 'awaiting_start')
        link = urlsplit(prepared['launch_url'])
        self.assertEqual((link.scheme, link.netloc), ('codex', 'new'))
        query = parse_qs(link.query)
        self.assertEqual(query['path'], [str(self.root.resolve())])
        self.assertEqual(query['prompt'], [prepared['prompt']])
        self.assertIn(prepared['run']['id'], prepared['prompt'])
        self.assertNotIn('private@example.com', prepared['launch_url'])
        self.assertEqual(self.store.all('applications'), [], 'A launch handoff is not an employer submission')
        public = self.request('GET', '/api/batches')[1]
        self.assertNotIn(str(self.root), json.dumps(public), 'Only explicit handoff exposes the local project path')
        self.assertNotIn('resume_path', json.dumps(public))
        for op in ('codex_context', 'codex_claim', 'codex_report', 'codex_authorize', 'codex_answers', 'codex_check'):
            with self.subTest(op=op), patch('jobflow.web._run_cli') as cli:
                denied, body = self.request('POST', '/api/action', {**payload, 'op': op}, token)
                self.assertEqual(denied, 403, body)
                cli.assert_not_called()
        from jobflow.web import _run_cli
        context = json.loads(_run_cli(self.root, ['batch', '--codex-run', prepared['run']['id'],
                                                 '--worker-id', 'cli-fixture'], full_output=True))
        self.assertEqual(context['workspace_id'], state['workspace_id'])
        self.assertEqual(Path(context['tasks'][0]['resume_path']).name, 'candidate.txt')
        self.assertEqual(Path(context['tasks'][0]['resume_path']).read_bytes(), b'Candidate professional experience.')
        self.assertNotIn('content_base64', json.dumps(context))
        claimed = subprocess.run([sys.executable, '-m', 'jobflow', '--root', str(self.root),
                                  'batch', '--request', '-'],
                                 input=json.dumps({'op': 'codex_claim', 'run_id': prepared['run']['id'],
                                       'worker_id': 'cli-fixture', 'workspace_id': state['workspace_id']}),
                                 capture_output=True, text=True, timeout=15,
                                 cwd=str(Path(__file__).resolve().parents[1]))
        self.assertEqual(claimed.returncode, 0, claimed.stderr)
        self.assertEqual(json.loads(claimed.stdout)['task']['mode'], 'inspect')
        (self.root / 'config/hosted.json').write_text('{"version":1}')
        self.assertFalse(self.request('GET', '/api/batches')[1]['codex']['available'])
        with patch('jobflow.web._run_cli') as cli:
            denied, body = self.request('POST', '/api/action', payload, token)
            self.assertEqual(denied, 409, body)
            cli.assert_not_called()


if __name__ == '__main__':
    unittest.main()

"""Guard the hosted critical path against redundant private-service calls.

This uses warm authoritative file bytes and an empty record page, so call counts
measure fixed request overhead rather than workspace size or network latency.
The assertions preserve the counts used by the performance investigation.
"""
import hashlib
import json
import os
import tempfile
import threading
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jobflow.cloud import CloudHandler


class HostedInitialLoadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'config').mkdir()
        self.workspace = '00000000-0000-0000-0000-000000000001'
        (self.root / 'config/remote.json').write_text(json.dumps({'workspace_id': self.workspace}))
        repository = Path(__file__).resolve().parents[1]
        self.files = {}
        self.calls = []
        for name, content in (
                ('config/settings.json', (repository / 'config/settings.example.json').read_bytes()),
                ('config/profile.json', json.dumps({'name': 'Fixture', 'evidence': [
                    {'id': 'fixture', 'text': 'Built a recorded system.'}]}).encode())):
            (self.root / name).write_bytes(content)
            self.files[name] = {'path': name, 'sha256': hashlib.sha256(content).hexdigest(),
                                'size': len(content)}
        account = {'id': 'fixture', 'email': 'fixture@example.test'}
        session = {'account': account, 'csrf_token': 'fixture'}

        def get_session(unused):
            self.calls.append('jobflow_session_get')
            return session

        def get_workspace(unused):
            self.calls.append('jobflow_account_get')
            return self.root

        def queue_status(unused):
            self.calls.append('jobflow_queue_status')
            return None

        self.server = SimpleNamespace(
            origin='http://127.0.0.1', cookie_name='session',
            accounts=SimpleNamespace(session=get_session, workspace=get_workspace),
            queue=SimpleNamespace(status=queue_status),
            asset_directory=repository / 'jobflow/web_assets',
            account_lock=lambda unused: threading.RLock())
        for context in (patch.dict(os.environ, {'JOBFLOW_BACKEND': 'supabase'}),
                        patch('jobflow.remote.client_from_env', return_value=self),
                        patch('jobflow.remote_store.client_from_env', return_value=self)):
            context.start()
            self.addCleanup(context.stop)

    def rpc(self, name, args):
        self.calls.append(name)
        self.assertEqual(args['p_workspace'], self.workspace)
        if name == 'jobflow_records_page':
            return {'records': [], 'revision': 1, 'count': 0, 'runs': []}
        if name == 'jobflow_file_get':
            return self.files.get(args['p_path'])
        if name == 'jobflow_file_list':
            return [row for path, row in sorted(self.files.items())
                    if path.startswith(args['p_prefix']) and path > args['p_after']]
        self.fail('Unexpected private service operation: ' + name)

    def request(self, path):
        self.calls.clear()
        handler = object.__new__(CloudHandler)
        handler.server = self.server
        handler.path = path
        handler.command = 'GET'
        handler.headers = {'Host': '127.0.0.1', 'Cookie': 'session=fixture'}
        handler._held_account_locks = {}
        responses = []
        handler._respond = lambda status, body, *args, **kwargs: responses.append((status, body))
        try:
            handler.do_GET()
        finally:
            for lock in handler._held_account_locks.values():
                lock.release()
        self.assertEqual(responses[0][0], 200, responses)
        return Counter(self.calls), responses[0][1]

    def test_initial_state_uses_one_record_read_and_one_manifest_per_prefix(self):
        calls, body = self.request('/api/state')
        self.assertEqual(calls, {'jobflow_session_get': 1, 'jobflow_account_get': 1,
                                'jobflow_file_list': 2, 'jobflow_records_page': 1,
                                'jobflow_queue_status': 1})
        self.assertEqual(body['account']['email'], 'fixture@example.test')
        self.assertEqual(body['jobs'], [])

    def test_home_does_not_load_resume_records_just_to_check_onboarding(self):
        calls, unused = self.request('/')
        self.assertEqual(calls, {'jobflow_session_get': 1, 'jobflow_account_get': 1,
                                'jobflow_file_list': 2})

    def test_static_asset_and_version_do_not_contact_private_service(self):
        for path in ('/app.js', '/maps.css', '/api/version'):
            with self.subTest(path=path):
                calls, unused = self.request(path)
                self.assertEqual(calls, {})

    def test_account_details_remain_available_without_duplicate_file_reads(self):
        calls, body = self.request('/api/account')
        self.assertEqual(calls, {'jobflow_session_get': 1, 'jobflow_account_get': 1,
                                'jobflow_file_list': 2, 'jobflow_records_page': 3})
        self.assertTrue(body['onboarding']['complete'])
        self.assertIn('resumes', body['onboarding'])


if __name__ == '__main__':
    unittest.main()

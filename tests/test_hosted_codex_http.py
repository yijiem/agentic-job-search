"""Hosted Codex capabilities cross HTTP without widening normal account access."""
import base64
import hashlib
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import test_cloud as cloud_fixture
from jobflow.application_batches import BATCH_KIND
from jobflow.storage import Store


class HostedCodexHTTPTests(unittest.TestCase):
    # Reuse the server/login fixture without inheriting the unrelated test cases.
    origin = cloud_fixture.CloudHTTPTests.origin
    request = cloud_fixture.CloudHTTPTests.request
    login = cloud_fixture.CloudHTTPTests.login
    seed = cloud_fixture.CloudHTTPTests.seed

    def setUp(self):
        self.environment = patch.dict(os.environ, {'JOBFLOW_BACKEND': 'local'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        cloud_fixture.CloudHTTPTests.setUp(self)
        self.account, self.workspace = self.seed('one@example.com', 'One')
        self.cookie, self.csrf, unused = self.login('one@example.com')
        self.workspace_id = self.state()['workspace_id']
        self.content = b'%PDF-1.4\nPrivate reviewed resume for account One.'
        self.resume = self.owner('upload_resume', filename='Reviewed resume.pdf',
            content_base64=base64.b64encode(self.content).decode('ascii'))['resume']
        self.other_resume = self.owner('upload_resume', filename='Unselected resume.pdf',
            content_base64=base64.b64encode(b'%PDF-1.4\nUnselected private material').decode('ascii'))['resume']
        store = Store(self.workspace / 'data/jobflow.sqlite3')
        job = dict(store.get('jobs', 'shared-id'))
        job.update(id='unselected-role', title='Private unselected role', url='https://example.com/jobs/unselected')
        store.put('jobs', job['id'], job)
        store.close()
        self.batch = self.owner('create', name='Hosted reviewed applications',
            job_ids=['shared-id', 'unselected-role'], resume_id=self.resume['id'],
            items=[{'job_id': 'unselected-role', 'resume_id': self.other_resume['id']}],
            applicant={'first_name': 'Ada', 'last_name': 'Example', 'email': 'ada@example.com'})['batch']
        self.connection = None

    def tearDown(self):
        cloud_fixture.CloudHTTPTests.tearDown(self)

    def state(self, cookie=None):
        status, unused, data = self.request('GET', '/api/batches', cookie=cookie or self.cookie)
        self.assertEqual(status, 200, data)
        return data

    def current(self):
        return next(batch for batch in self.state()['batches'] if batch['id'] == self.batch['id'])

    def owner(self, op, **payload):
        status, unused, data = self.request('POST', '/api/action',
            {'action': 'batch', 'op': op, **payload}, cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 200, data)
        return data

    def prepare(self):
        result = self.owner('codex_prepare', workspace_id=self.workspace_id,
            batch_id=self.batch['id'], revision=self.current()['revision'], job_ids=['shared-id'])
        self.connection = result['connection']
        return result

    def scope(self, **payload):
        return {'workspace_id': self.connection['workspace_id'],
                'batch_id': self.connection['batch_id'], 'run_id': self.connection['run_id'],
                'worker_id': 'hosted-http-worker', **payload}

    def runner_response(self, op, token=None, headers=None, **payload):
        request_headers = {'Origin': None,
            'Authorization': 'Bearer ' + (self.connection['token'] if token is None else token)}
        request_headers.update(headers or {})
        return self.request('POST', '/api/codex/runner', self.scope(op=op, **payload),
                            headers=request_headers)

    def runner(self, op, **payload):
        status, unused, data = self.runner_response(op, **payload)
        self.assertEqual(status, 200, data)
        return data

    def denied(self, response):
        status, unused, data = response
        self.assertIn(status, (400, 401, 403, 404, 409, 410), data)
        return data

    def download(self, resume_id=None, token=None, **headers):
        return self.request('GET', '/api/codex/resumes/' + (resume_id or self.resume['id']),
            headers={'Authorization': 'Bearer ' + (self.connection['token'] if token is None else token), **headers})

    def mutate_run(self, **values):
        store = Store(self.workspace / 'data/jobflow.sqlite3')
        try:
            batch = store.get(BATCH_KIND, self.batch['id'])
            batch['codex_run'].update(values)
            if 'handoff_expires_at' in values:
                batch['codex_run']['hosted_access']['expires_at'] = values['handoff_expires_at']
            store.put(BATCH_KIND, batch['id'], batch)
        finally:
            store.close()

    def inspect(self):
        self.runner('codex_context')
        task = self.runner('codex_claim')['task']
        self.assertEqual(task['mode'], 'inspect')
        result = self.runner('codex_report', job_id=task['job_id'], attempt_id=task['attempt_id'],
            status='inspected', observed_url=task['url'],
            fields=[{'key': 'name', 'label': 'Name', 'type': 'text', 'required': True, 'supported': True},
                    {'key': 'resume', 'label': 'Resume', 'type': 'file', 'required': True, 'supported': True}],
            planned_answers={'name': 'Ada Example', 'resume': self.resume['filename']})
        return result

    def authorize(self):
        result = self.inspect()
        self.runner('codex_authorize', revision=result['batch']['revision'],
                    job_ids=['shared-id'], confirmed=True)
        task = self.runner('codex_claim')['task']
        self.assertEqual(task['mode'], 'submit')
        return task

    def test_handoff_is_scoped_path_free_and_does_not_start_or_submit(self):
        self.assertTrue(self.state()['codex']['available'])
        prepared = self.prepare()
        self.assertEqual(prepared['run']['status'], 'awaiting_start')
        self.assertFalse(prepared['run']['started'])
        self.assertEqual(self.connection['origin'], self.origin)
        self.assertEqual(self.connection['batch_id'], self.batch['id'])
        self.assertEqual(self.connection['workspace_id'], self.workspace_id)
        self.assertEqual(self.connection['run_id'], prepared['run']['id'])
        launch = urlsplit(prepared['launch_url'])
        self.assertEqual((launch.scheme, launch.netloc), ('codex', 'new'))
        query = parse_qs(launch.query)
        self.assertNotIn('path', query)
        self.assertEqual(query['prompt'], [prepared['prompt']])
        self.assertNotIn(str(self.workspace), prepared['prompt'])
        public = json.dumps(self.state())
        self.assertNotIn(self.connection['token'], public)
        self.assertNotIn('token_hash', public)
        self.assertNotIn(str(self.workspace), public)
        store = Store(self.workspace / 'data/jobflow.sqlite3')
        try:
            application = store.get('applications', 'shared-id')
            self.assertEqual(application['stage'], 'saved')
            self.assertFalse(application.get('browser_submissions'))
        finally:
            store.close()

    def test_intermediate_runner_receipt_does_not_start_refresh_before_remaining_role(self):
        prepared = self.owner('codex_prepare', workspace_id=self.workspace_id,
            batch_id=self.batch['id'], revision=self.current()['revision'],
            job_ids=['shared-id', 'unselected-role'])
        self.connection = prepared['connection']
        task = self.authorize()
        self.runner('codex_report', job_id=task['job_id'], attempt_id=task['attempt_id'],
                    status='submitting', form_signature=task['form_signature'])
        self.runner('codex_check', job_id=task['job_id'], attempt_id=task['attempt_id'], phase='before_click')
        # A pre-existing search must remain queued; browser bookkeeping only
        # avoids adding a competing derived-report refresh.
        self.server.queue.enqueue(self.account['id'], 'run')
        existing = self.server.queue.status(self.account['id'])
        with patch('jobflow.cloud.remote_enabled', return_value=True), \
                patch.object(self.server.queue, 'enqueue') as enqueue:
            result = self.runner('codex_report', job_id=task['job_id'], attempt_id=task['attempt_id'],
                status='submitted', evidence={'url': task['url'], 'text': 'Application received.'})
            self.assertEqual(result['batch']['codex_run']['status'], 'running')
            enqueue.assert_not_called()
        self.assertEqual(self.server.queue.status(self.account['id']), existing)
        # Manual confirmation of the remaining role must enqueue exactly once
        # after the run becomes complete, preserving both application receipts.
        remaining = self.runner('codex_claim', job_id='unselected-role')['task']
        self.runner('codex_report', job_id=remaining['job_id'], attempt_id=remaining['attempt_id'],
                    status='needs_input', message='User completed the employer form manually.')
        revision = self.current()['revision']
        with patch('jobflow.cloud.remote_enabled', return_value=True), \
                patch.object(self.server.queue, 'enqueue') as enqueue:
            result = self.owner('reconcile', batch_id=self.batch['id'], revision=revision,
                job_id='unselected-role', outcome='submitted', confirmed=True,
                evidence={'url': remaining['url'], 'text': 'User verified application received.'})
            self.assertEqual(result['batch']['codex_run']['status'], 'complete')
            enqueue.assert_called_once_with(self.account['id'], 'refresh')
        store = Store(self.workspace / 'data/jobflow.sqlite3')
        try:
            for identifier in ('shared-id', 'unselected-role'):
                self.assertEqual(len(store.get('applications', identifier)['browser_submissions']), 1)
        finally:
            store.close()

    def test_runner_hydrates_configuration_once_and_loads_selected_resume_lazily(self):
        from jobflow.cloud import hydrate_workspace
        self.prepare()
        with patch('jobflow.cloud.hydrate_workspace', wraps=hydrate_workspace) as hydrate:
            context = self.runner('codex_context')
            hydrate.assert_called_once_with(self.workspace, uploads=False)
        self.assertEqual(context['tasks'][0]['resume_download']['id'], self.resume['id'])
        status, unused, data = self.download()
        self.assertEqual(status, 200)
        self.assertEqual(data, self.content)

    def test_public_runner_assets_contain_no_account_materials(self):
        for path in ('/codex/runner.py', '/codex/workflow.md'):
            with self.subTest(path=path):
                status, headers, body = self.request('GET', path)
                self.assertEqual(status, 200)
                self.assertGreater(len(body), 100)
                self.assertNotIn(self.content, body)
                self.assertNotIn(b'one@example.com', body)
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        for path in ('/codex/../config/profile.json', '/codex/private.py'):
            self.denied(self.request('GET', path))

    def test_bearer_auth_does_not_replace_browser_csrf_or_accept_browser_origins(self):
        self.prepare()
        self.denied(self.runner_response('codex_context', token='invalid'))
        self.denied(self.runner_response('codex_context', headers={'Authorization': None}))
        self.denied(self.runner_response('codex_context', headers={'Origin': 'https://evil.example'}))
        self.denied(self.runner_response('codex_context', headers={'Sec-Fetch-Site': 'cross-site'}))
        self.denied(self.runner_response('codex_context', headers={'Host': 'evil.example'}))
        self.denied(self.runner_response('codex_context', headers={'Cookie': self.cookie}))
        payload = {'action': 'batch', 'op': 'codex_prepare', 'workspace_id': self.workspace_id,
                   'batch_id': self.batch['id'], 'revision': self.current()['revision']}
        self.denied(self.request('POST', '/api/action', payload, cookie=self.cookie))
        self.denied(self.request('POST', '/api/action', payload,
            headers={'Origin': None, 'Authorization': 'Bearer ' + self.connection['token']}))
        for op in ('codex_context', 'codex_hosted_prepare'):
            self.denied(self.request('POST', '/api/action', {**payload, 'op': op},
                cookie=self.cookie, csrf=self.csrf))
        self.assertFalse(self.current()['codex_run']['started'])

    def test_run_scope_rejects_spoofed_identifiers_and_unrelated_operations(self):
        self.prepare()
        for override in ({'workspace_id': 'wrong-workspace'}, {'batch_id': 'wrong-batch'},
                         {'run_id': 'wrong-run'}):
            with self.subTest(scope=override):
                self.denied(self.runner_response('codex_context', **override))
        for op in ('create', 'upload_resume', 'update', 'reconcile', 'codex_prepare',
                   'codex_hosted_prepare', 'codex_renew', 'codex_revoke', 'codex_discard'):
            with self.subTest(op=op):
                self.denied(self.runner_response(op))
        context = self.runner('codex_context')
        self.assertEqual([task['job_id'] for task in context['tasks']], ['shared-id'])
        self.assertEqual([item['job_id'] for item in context['batch']['items']], ['shared-id'])
        self.assertNotIn('Private unselected role', json.dumps(context))
        self.assertNotIn(str(self.workspace), json.dumps(context))
        self.denied(self.runner_response('codex_claim', job_id='unselected-role'))
        self.denied(self.runner_response('codex_context', worker_id='different-chat'))

    def test_private_resume_requires_selected_run_capability_and_intact_bytes(self):
        self.prepare()
        context = self.runner('codex_context')
        task = context['tasks'][0]
        self.assertFalse(task.get('resume_path'))
        self.assertEqual(task['resume']['sha256'], hashlib.sha256(self.content).hexdigest())
        status, headers, body = self.download()
        self.assertEqual(status, 200, body)
        self.assertEqual(body, self.content)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.denied(self.download(token='wrong'))
        self.denied(self.download(self.other_resume['id']))
        self.denied(self.request('GET', '/api/codex/resumes/' + self.resume['id'], cookie=self.cookie))
        self.denied(self.request('GET', '/api/codex/resumes/' + self.resume['id'] + '?token=' + self.connection['token']))
        path = self.workspace / 'inputs/application-resumes' / self.resume['id']
        path.write_bytes(b'Corrupted resume')
        self.denied(self.download())

    def test_capability_cannot_access_another_account_or_its_resumes(self):
        self.prepare()
        first = dict(self.connection)
        first_cookie, first_csrf = self.cookie, self.csrf
        account_two, workspace_two = self.seed('two@example.com', 'Two')
        self.cookie, self.csrf, unused = self.login('two@example.com')
        second_state = self.state()
        second_resume = self.owner('upload_resume', filename='Second account.pdf',
            content_base64=base64.b64encode(b'%PDF-1.4\nSecond account private resume').decode('ascii'))['resume']
        second_batch = self.owner('create', name='Second account private batch', job_ids=['shared-id'],
            resume_id=second_resume['id'], applicant={'first_name': 'Second', 'email': 'second@example.com'})['batch']
        second = self.owner('codex_prepare', batch_id=second_batch['id'], revision=second_batch['revision'],
                            workspace_id=second_state['workspace_id'], job_ids=['shared-id'])['connection']
        self.denied(self.runner_response('codex_context', workspace_id=second['workspace_id'],
            batch_id=second['batch_id'], run_id=second['run_id']))
        self.denied(self.download(second_resume['id']))
        self.connection = second
        self.denied(self.runner_response('codex_context', workspace_id=first['workspace_id'],
            batch_id=first['batch_id'], run_id=first['run_id']))
        self.denied(self.download(self.resume['id']))
        second_context = self.runner('codex_context')
        self.assertNotIn('One company', json.dumps(second_context))
        self.connection = first
        first_context = self.runner('codex_context')
        self.assertNotIn('Second account', json.dumps(first_context))
        self.cookie, self.csrf = first_cookie, first_csrf

    def test_inspection_submission_and_one_click_checkpoint_sync_to_hosted_account(self):
        self.prepare()
        task = self.authorize()
        self.assertEqual(self.current()['items'][0]['status'], 'running')
        self.denied(self.runner_response('codex_check', job_id=task['job_id'],
            attempt_id=task['attempt_id'], phase='before_click'))
        self.runner('codex_report', job_id=task['job_id'], attempt_id=task['attempt_id'],
                    status='submitting', form_signature=task['form_signature'])
        first = self.runner('codex_check', job_id=task['job_id'], attempt_id=task['attempt_id'], phase='before_click')
        second = self.runner('codex_check', job_id=task['job_id'], attempt_id=task['attempt_id'], phase='before_click')
        self.assertTrue(first['can_click'])
        self.assertFalse(second['can_click'])
        self.runner('codex_report', job_id=task['job_id'], attempt_id=task['attempt_id'], status='submitted',
            evidence={'url': task['url'], 'text': 'Application received. Thank you for applying.'})
        current = self.current()
        self.assertEqual(current['items'][0]['status'], 'submitted')
        self.assertEqual(current['items'][1]['status'], 'pending')
        self.assertEqual(current['codex_run']['status'], 'complete')
        store = Store(self.workspace / 'data/jobflow.sqlite3')
        try:
            application = store.get('applications', 'shared-id')
            self.assertEqual(application['stage'], 'applied')
            self.assertEqual(application['notes'], 'One confidential notes')
            self.assertEqual(application['custom'], {'preserve': 'One'})
            self.assertEqual(application['browser_submissions'][-1]['run_id'], self.connection['run_id'])
            self.assertEqual(application['browser_submissions'][-1]['executor'], 'codex')
        finally:
            store.close()

    def test_pause_blocks_filling_and_clicking_until_owner_resumes(self):
        self.prepare()
        task = self.authorize()
        self.owner('pause', batch_id=self.batch['id'], revision=self.current()['revision'])
        for phase in ('before_click', 'before_fill'):
            self.denied(self.runner_response('codex_check', job_id=task['job_id'],
                                            attempt_id=task['attempt_id'], phase=phase))
        self.owner('resume', batch_id=self.batch['id'], revision=self.current()['revision'])
        self.assertTrue(self.runner('codex_check', job_id=task['job_id'], attempt_id=task['attempt_id'])['can_fill'])

    def test_cancellation_retains_inflight_outcome_and_accepts_its_confirmation(self):
        self.prepare()
        task = self.authorize()
        self.runner('codex_report', job_id=task['job_id'], attempt_id=task['attempt_id'],
                    status='submitting', form_signature=task['form_signature'])
        self.assertTrue(self.runner('codex_check', job_id=task['job_id'],
            attempt_id=task['attempt_id'], phase='before_click')['can_click'])
        self.owner('cancel', batch_id=self.batch['id'], revision=self.current()['revision'])
        self.assertEqual(self.current()['items'][0]['status'], 'uncertain')
        self.denied(self.download())
        self.denied(self.runner_response('codex_claim'))
        self.denied(self.runner_response('codex_check', job_id=task['job_id'],
            attempt_id=task['attempt_id'], phase='before_click'))
        self.runner('codex_report', job_id=task['job_id'], attempt_id=task['attempt_id'], status='submitted',
                    evidence={'url': task['url'], 'text': 'The submitted application was received.'})
        self.assertEqual(self.current()['items'][0]['status'], 'submitted')
        self.assertEqual(self.current()['status'], 'cancelled')

    def test_disabling_account_invalidates_capability_even_before_expiry(self):
        self.prepare()
        self.runner('codex_context')
        with self.server.accounts._connect() as accounts:
            accounts.execute("UPDATE accounts SET status='disabled' WHERE id=?", (self.account['id'],))
        self.denied(self.runner_response('codex_context'))
        self.denied(self.download())

    def test_expiry_rotation_and_revocation_preserve_worker_ownership(self):
        self.prepare()
        self.runner('codex_context')
        old = self.connection['token']
        self.mutate_run(handoff_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        self.denied(self.runner_response('codex_context'))
        self.denied(self.download())
        renewed = self.owner('codex_renew', workspace_id=self.workspace_id, batch_id=self.batch['id'],
            run_id=self.connection['run_id'], revision=self.current()['revision'])
        self.connection = renewed['connection']
        self.assertNotEqual(self.connection['token'], old)
        self.denied(self.runner_response('codex_context', token=old))
        self.denied(self.runner_response('codex_context', worker_id='different-chat'))
        self.assertTrue(self.runner('codex_context')['run']['started'])
        self.owner('codex_revoke', workspace_id=self.workspace_id, batch_id=self.batch['id'],
            run_id=self.connection['run_id'], revision=self.current()['revision'])
        self.denied(self.runner_response('codex_context'))
        self.denied(self.download())
        current = self.current()
        self.assertEqual(current['items'][0]['status'], 'pending')
        self.assertTrue(current['codex_run']['started'])


if __name__ == '__main__':
    unittest.main()

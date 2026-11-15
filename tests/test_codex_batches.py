import base64
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jobflow.application_batches import BATCH_KIND, batch_detail, batch_snapshot, perform_batch_action, workspace_id
from jobflow.storage import Store


class CodexBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data/jobflow.sqlite3')
        self.config = {'preparation': {'daily_minutes': 90}, 'schedule': {'timezone': 'America/Los_Angeles'}}
        for number in range(1, 4):
            job_id = 'job-' + str(number)
            self.store.put('jobs', job_id, {'id': job_id, 'company': 'Example', 'title': 'Engineer',
                'url': 'https://jobs.example.com/role/' + str(number), 'availability': 'open'})
        self.content = b'%PDF-1.4\nReviewed candidate resume'
        self.resume = self.action('upload_resume', filename='Reviewed resume.pdf',
            content_base64=base64.b64encode(self.content).decode())['resume']
        self.batch = self.action('create', name='Codex applications', job_ids=['job-1'], resume_id=self.resume['id'],
            applicant={'first_name': 'Ada', 'last_name': 'Example', 'email': 'ada@example.com'},
            answers={'Work authorization': 'Yes'})['batch']
        self.run_id = None

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def action(self, op, **payload):
        return perform_batch_action(self.root, self.store, self.config, {}, {'op': op, **payload})

    def current(self):
        return batch_detail(self.root, self.batch['id'])

    def prepare(self, ids=None):
        result = self.action('codex_prepare', batch_id=self.batch['id'], revision=self.current()['revision'],
            **({'job_ids': ids} if ids is not None else {}))
        self.run_id = result['run']['id']
        return result

    def codex(self, op, **payload):
        return self.action(op, **{'workspace_id': workspace_id(self.root), 'run_id': self.run_id,
            'worker_id': 'codex-owner', 'batch_id': self.batch['id'], **payload})

    def context(self):
        return self.codex('codex_context')

    def claim(self, **payload):
        return self.codex('codex_claim', **payload)['task']

    def report(self, task, status, **payload):
        return self.codex('codex_report', job_id=task['job_id'], attempt_id=task['attempt_id'], status=status, **payload)

    def inspect(self, task=None, planned=None):
        task = task or self.claim()
        fields = [{'key': 'name', 'label': 'Name', 'type': 'text', 'required': True},
            {'key': 'resume', 'label': 'Resume/CV', 'type': 'file', 'required': True},
            {'key': 'locations', 'label': 'Preferred locations', 'type': 'multiselect',
             'options': ['San Francisco', 'Remote'], 'required': True}]
        return self.report(task, 'inspected', observed_url=task['url'], fields=fields,
            planned_answers=planned if planned is not None else {'name': 'Ada Example', 'resume': self.resume['filename'], 'locations': ['Remote']})

    def authorized(self):
        self.prepare()
        self.context()
        reviewed = self.inspect()['batch']
        return self.codex('codex_authorize', revision=reviewed['revision'], job_ids=['job-1'], confirmed=True)['batch']

    def submitting(self):
        self.authorized()
        task = self.claim()
        self.report(task, 'submitting', form_signature=task['form_signature'])
        return task

    def expire(self):
        batch = self.store.get(BATCH_KIND, self.batch['id'])
        batch['items'][0]['lease_until'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.store.put(BATCH_KIND, batch['id'], batch)

    def test_prepare_reserves_without_authorizing_and_is_idempotent(self):
        revision = self.current()['revision']
        result = self.prepare()
        self.assertEqual(result['run']['status'], 'awaiting_start')
        self.assertFalse(result['run']['started'])
        self.assertEqual(result['batch']['executor'], 'codex')
        self.assertNotIn('authorization', self.store.get(BATCH_KIND, self.batch['id'])['items'][0])
        repeated = self.action('codex_prepare', batch_id=self.batch['id'], revision=revision)
        self.assertEqual(repeated['run']['id'], self.run_id)
        self.assertEqual(repeated['batch']['revision'], result['batch']['revision'])
        self.assertIsNone(self.action('claim', worker_id='legacy-worker')['task'])
        public = json.dumps(batch_snapshot(self.root))
        self.assertNotIn(str(self.root), public)
        self.assertNotIn('materials', result['run'])

    def test_context_binds_one_worker_and_stages_verified_named_upload(self):
        self.prepare()
        context = self.context()
        self.assertTrue(context['run']['started'])
        task = context['tasks'][0]
        path = Path(task['resume_path'])
        self.assertEqual(path.name, self.resume['filename'])
        self.assertEqual(path.read_bytes(), self.content)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), self.resume['sha256'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('content_base64', task['resume'])
        with self.assertRaisesRegex(ValueError, 'another Codex worker'):
            self.codex('codex_context', worker_id='different-chat')
        with self.assertRaisesRegex(ValueError, 'workspace'):
            self.codex('codex_context', workspace_id='different-workspace')
        with self.assertRaisesRegex(ValueError, 'reserves'):
            self.action('update', batch_id=self.batch['id'], revision=self.current()['revision'], applicant={'first_name': 'Changed'})

    def test_upload_staging_is_per_resume_and_rejects_symlinks_and_changed_bytes(self):
        alternate = self.action('upload_resume', filename=self.resume['filename'],
            content_base64=base64.b64encode(b'%PDF-1.4\nDifferent reviewed resume').decode())['resume']
        self.batch = self.action('create', name='Second batch', job_ids=['job-2', 'job-3'], resume_id=self.resume['id'],
            items=[{'job_id': 'job-3', 'resume_id': alternate['id']}])['batch']
        self.prepare()
        paths = [Path(task['resume_path']) for task in self.context()['tasks']]
        self.assertEqual(paths[0].name, paths[1].name)
        self.assertNotEqual(paths[0], paths[1])
        self.assertNotEqual(paths[0].read_bytes(), paths[1].read_bytes())
        paths[0].write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'upload copy changed'):
            self.context()
        paths[0].unlink()
        paths[0].symlink_to(self.root / 'inputs/application-resumes' / self.resume['id'])
        with self.assertRaisesRegex(ValueError, 'symbolic'):
            self.context()

    def test_staging_directory_symlink_is_rejected(self):
        self.prepare()
        (self.root / 'inputs/codex_uploads').symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, 'symbolic'):
            self.context()

    def test_claim_scope_and_fresh_codex_inspection_with_multiselect(self):
        self.prepare()
        self.context()
        with self.assertRaisesRegex(ValueError, 'reserved'):
            self.claim(job_id='job-2')
        task = self.claim()
        self.assertEqual(task['mode'], 'inspect')
        self.assertIsNone(self.claim(), 'One runner cannot claim another role while an attempt is live')
        reviewed = self.inspect(task)['batch']
        item = reviewed['items'][0]
        self.assertEqual(item['status'], 'ready')
        self.assertTrue(item['form_signature'].startswith('codex-'))
        self.assertEqual(item['form_executor'], 'codex')
        self.assertEqual(len(item['codex_review_history']), 1)
        with self.assertRaisesRegex(ValueError, 'Codex workflow'):
            self.action('authorize', batch_id=self.batch['id'], revision=reviewed['revision'], job_ids=['job-1'], confirmed=True)

    def test_missing_choices_unknown_controls_and_role_mismatch_are_not_approved(self):
        self.prepare()
        self.context()
        task = self.claim()
        with self.assertRaisesRegex(ValueError, 'different application'):
            self.report(task, 'inspected', observed_url='https://jobs.example.com/role/2',
                fields=[{'key': 'x', 'type': 'text'}], planned_answers={})
        with self.assertRaisesRegex(ValueError, 'observed choice options'):
            self.report(task, 'inspected', observed_url=task['url'], fields=[{'key': 'x', 'type': 'select'}], planned_answers={})
        result = self.inspect(task, planned={'name': 'Ada', 'resume': self.resume['filename'], 'locations': ['Invented option']})
        self.assertEqual(result['batch']['items'][0]['status'], 'needs_input')
        with self.assertRaisesRegex(ValueError, 'ready form'):
            self.codex('codex_authorize', revision=result['batch']['revision'], job_ids=['job-1'], confirmed=True)

    def test_explicit_answers_reset_review_without_releasing_reservation(self):
        self.prepare()
        self.context()
        self.inspect(planned={})
        result = self.codex('codex_answers', job_id='job-1', answers={'locations': ['Remote'], 'sponsorship': 'No'})
        item = result['batch']['items'][0]
        self.assertEqual(item['status'], 'pending')
        self.assertEqual(item['codex_run_id'], self.run_id)
        self.assertEqual(len(item['codex_review_history']), 1)
        self.assertEqual(self.context()['tasks'][0]['answers']['sponsorship'], 'No')
        self.assertIsNone(self.action('claim', worker_id='legacy-worker')['task'])

    def test_exact_authorization_checkpoint_and_one_time_click_grant(self):
        self.authorized()
        task = self.claim()
        self.assertEqual(task['mode'], 'submit')
        self.assertIsNotNone(task['resume_path'])
        with self.assertRaisesRegex(ValueError, 'submitting checkpoint'):
            self.codex('codex_check', job_id='job-1', attempt_id=task['attempt_id'], phase='before_click')
        with self.assertRaisesRegex(ValueError, 'form changed'):
            self.report(task, 'submitting', form_signature='stale')
        self.report(task, 'submitting', form_signature=task['form_signature'])
        first = self.codex('codex_check', job_id='job-1', attempt_id=task['attempt_id'], phase='before_click')
        second = self.codex('codex_check', job_id='job-1', attempt_id=task['attempt_id'], phase='before_click')
        self.assertTrue(first['can_click'])
        self.assertFalse(second['can_click'])
        self.assertFalse(self.codex('codex_check', job_id='job-1', attempt_id=task['attempt_id'])['can_submit'])

    def test_confirmation_survives_missing_resume_and_preserves_history(self):
        task = self.submitting()
        self.codex('codex_check', job_id='job-1', attempt_id=task['attempt_id'], phase='before_click')
        (self.root / 'inputs/application-resumes' / self.resume['id']).unlink()
        self.store.put('applications', 'job-1', {'id': 'job-1', 'job_id': 'job-1', 'stage': 'technical',
            'notes': 'User history', 'applied_at': '2025-01-01T12:00:00+00:00'})
        result = self.report(task, 'submitted', evidence={'url': task['url'] + '/confirmation', 'text': 'Your application has been received.'})
        self.assertTrue(result['application_updated'])
        application = self.store.get('applications', 'job-1')
        self.assertEqual(application['stage'], 'technical')
        self.assertEqual(application['notes'], 'User history')
        self.assertEqual(application['applied_at'], '2025-01-01T12:00:00+00:00')
        self.assertEqual(application['browser_submissions'][-1]['executor'], 'codex')
        self.assertEqual(application['browser_submissions'][-1]['run_id'], self.run_id)
        self.assertEqual(result['batch']['codex_run']['status'], 'complete')
        self.assertIsNone(self.context()['tasks'][0]['resume_path'])

    def test_expired_submission_is_uncertain_and_late_receipt_is_accepted(self):
        task = self.submitting()
        self.expire()
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.report(task, 'heartbeat')
        self.assertEqual(self.current()['items'][0]['status'], 'uncertain')
        self.assertEqual(self.context()['tasks'][0]['attempt_id'], task['attempt_id'])
        self.assertIsNone(self.claim())
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.codex('codex_check', job_id='job-1', attempt_id=task['attempt_id'], phase='before_click')
        self.report(task, 'submitted', evidence={'url': task['url'], 'text': 'Application received.'})
        self.assertEqual(self.current()['items'][0]['status'], 'submitted')

    def test_context_recovers_expired_fill_without_reviving_authorization(self):
        self.authorized()
        task = self.claim()
        self.expire()
        context = self.context()
        self.assertEqual(context['tasks'][0]['status'], 'needs_input')
        self.assertIsNone(context['tasks'][0]['attempt_id'])
        with self.assertRaises(ValueError):
            self.report(task, 'heartbeat')
        self.assertIsNone(self.claim())

    def test_pause_cancel_and_uncertain_outcomes_are_not_retryable(self):
        task = self.submitting()
        paused = self.action('pause', batch_id=self.batch['id'], revision=self.current()['revision'])['batch']
        with self.assertRaisesRegex(ValueError, 'paused'):
            self.codex('codex_check', job_id='job-1', attempt_id=task['attempt_id'], phase='before_click')
        self.action('cancel', batch_id=self.batch['id'], revision=paused['revision'])
        self.assertEqual(self.current()['items'][0]['status'], 'uncertain')
        self.assertIsNone(self.claim())
        self.report(task, 'submitted', evidence={'url': task['url'], 'text': 'Confirmed actual submission.'})
        self.assertEqual(self.current()['items'][0]['status'], 'submitted')

    def test_discard_only_unstarted_keeps_answers_and_run_history(self):
        self.prepare()
        old_id = self.run_id
        discarded = self.action('codex_discard', batch_id=self.batch['id'], revision=self.current()['revision'])['batch']
        self.assertNotIn('codex_run', discarded)
        self.assertEqual(discarded['codex_history'][-1]['id'], old_id)
        self.assertEqual(discarded['codex_history'][-1]['status'], 'discarded')
        self.assertEqual(discarded['answers']['Work authorization'], 'Yes')
        self.assertNotIn('codex_run_id', discarded['items'][0])
        self.action('update', batch_id=self.batch['id'], revision=discarded['revision'], applicant={'phone': '555-1234'})
        self.prepare()
        self.assertNotEqual(old_id, self.run_id)
        self.context()
        with self.assertRaisesRegex(ValueError, 'unstarted'):
            self.action('codex_discard', batch_id=self.batch['id'], revision=self.current()['revision'])

    def test_legacy_reports_cannot_mutate_reserved_attempts(self):
        self.prepare()
        self.context()
        task = self.claim()
        with self.assertRaisesRegex(ValueError, 'Codex workflow'):
            self.action('report', batch_id=self.batch['id'], job_id='job-1', attempt_id=task['attempt_id'], status='failed')
        with self.assertRaisesRegex(ValueError, 'another Codex worker'):
            self.codex('codex_report', worker_id='foreign', job_id='job-1', attempt_id=task['attempt_id'], status='failed')

    def test_unrecognized_query_role_identifier_is_still_bound(self):
        raw = self.store.get(BATCH_KIND, self.batch['id'])
        url = 'https://jobs.example.com/apply?job=selected-role&locale=en'
        raw['items'][0]['url'] = url
        self.store.put(BATCH_KIND, raw['id'], raw)
        job = self.store.get('jobs', 'job-1')
        job['url'] = url
        self.store.put('jobs', 'job-1', job)
        self.prepare()
        self.context()
        task = self.claim()
        with self.assertRaisesRegex(ValueError, 'different application'):
            self.report(task, 'inspected', observed_url='https://jobs.example.com/apply?job=other-role&locale=en',
                fields=[{'key': 'name', 'type': 'text'}], planned_answers={'name': 'Ada'})
        self.report(task, 'inspected', observed_url='https://jobs.example.com/apply?locale=en&job=selected-role',
            fields=[{'key': 'name', 'type': 'text'}], planned_answers={'name': 'Ada'})

    def test_click_grant_rechecks_original_and_named_upload_integrity(self):
        task = self.submitting()
        original = self.root / 'inputs/application-resumes' / self.resume['id']
        staged = Path(task['resume_path'])
        for path in (original, staged):
            with self.subTest(path=path.name):
                content = path.read_bytes()
                path.write_bytes(b'Changed after form filling')
                with self.assertRaisesRegex(ValueError, 'changed'):
                    self.codex('codex_check', job_id='job-1', attempt_id=task['attempt_id'], phase='before_click')
                item = self.store.get(BATCH_KIND, self.batch['id'])['items'][0]
                self.assertNotIn('codex_click_granted_at', item)
                path.write_bytes(content)
        self.assertTrue(self.codex('codex_check', job_id='job-1', attempt_id=task['attempt_id'], phase='before_click')['can_click'])

    def test_finished_role_resume_cannot_block_remaining_healthy_role(self):
        other = self.action('upload_resume', filename='Other.pdf', content_base64=base64.b64encode(b'%PDF-1.4\nOther').decode())['resume']
        self.batch = self.action('create', name='Multi role', job_ids=['job-2', 'job-3'], resume_id=self.resume['id'],
            items=[{'job_id': 'job-3', 'resume_id': other['id']}])['batch']
        self.prepare()
        self.context()
        task = self.claim(job_id='job-2')
        ready = self.inspect(task)['batch']
        self.codex('codex_authorize', revision=ready['revision'], job_ids=['job-2'], confirmed=True)
        task = self.claim(job_id='job-2')
        self.report(task, 'submitting', form_signature=task['form_signature'])
        self.report(task, 'submitted', evidence={'url': task['url'], 'text': 'Application received.'})
        (self.root / 'inputs/application-resumes' / self.resume['id']).unlink()
        self.assertEqual(self.claim(job_id='job-3')['job_id'], 'job-3')

    def test_completed_scoped_run_can_prepare_remaining_roles_by_default(self):
        self.batch = self.action('create', name='Scoped roles', job_ids=['job-2', 'job-3'], resume_id=self.resume['id'])['batch']
        self.prepare(['job-2'])
        first_run = self.run_id
        self.context()
        task = self.claim()
        ready = self.inspect(task)['batch']
        self.codex('codex_authorize', revision=ready['revision'], job_ids=['job-2'], confirmed=True)
        task = self.claim()
        self.report(task, 'submitting', form_signature=task['form_signature'])
        self.report(task, 'submitted', evidence={'url': task['url'], 'text': 'Application received.'})
        second = self.prepare()
        self.assertNotEqual(second['run']['id'], first_run)
        self.assertEqual(second['run']['job_ids'], ['job-3'])
        self.assertEqual(second['batch']['codex_history'][-1]['id'], first_run)


if __name__ == '__main__':
    unittest.main()

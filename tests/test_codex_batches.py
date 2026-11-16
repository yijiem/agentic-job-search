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

    def coinbase(self):
        self.listing_url = 'https://www.coinbase.com/careers/positions/8304034?gh_jid=8304034'
        self.destination_url = 'https://job-boards.greenhouse.io/embed/job_app?token=8304034&for=coinbase&gh_jid=8304034'
        batch = self.store.get(BATCH_KIND, self.batch['id'])
        job = self.store.get('jobs', 'job-1')
        job.update(company='Coinbase', title='Senior Software Engineer', url=self.listing_url)
        self.store.put('jobs', 'job-1', job)
        batch['items'][0].update({key: job[key] for key in ('company', 'title', 'url')})
        self.store.put(BATCH_KIND, batch['id'], batch)
        self.prepare()
        self.context()

    def bind_destination(self, **changes):
        return self.codex('codex_bind_destination', **{
            'job_id': 'job-1', 'revision': self.current()['revision'], 'listing_url': self.listing_url,
            'observed_href': self.destination_url, 'destination_url': self.destination_url,
            'observed_company': 'Coinbase', 'observed_title': 'Senior Software Engineer',
            'observed_role_id': '8304034', **changes})

    def inspect_destination(self, task=None):
        task = task or self.claim()
        return self.report(task, 'inspected', observed_url=self.destination_url,
            fields=[{'key': 'name', 'label': 'Name', 'type': 'text', 'required': True}],
            planned_answers={'name': 'Ada Example'})['batch']

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

    def conditional_fields(self):
        return [
            {'key': 'current_role', 'label': 'Current role', 'type': 'checkbox', 'required': False},
            {'key': 'end_month', 'label': 'End month', 'type': 'select', 'required': True,
             'options': ['January', 'February'], 'required_unless': {'key': 'current_role', 'equals': True}},
            {'key': 'end_year', 'label': 'End year', 'type': 'text', 'required': True,
             'required_unless': {'key': 'current_role', 'equals': True}}]

    def test_conditional_plan_requires_exact_observed_verification_before_click_checkpoint(self):
        self.prepare()
        self.context()
        task = self.claim()
        ready = self.report(task, 'inspected', observed_url=task['url'], fields=self.conditional_fields(),
            planned_answers={'current_role': True})['batch']
        item = ready['items'][0]
        self.assertEqual(item['status'], 'ready')
        self.assertTrue(item['fields'][1]['required'])
        self.assertEqual(item['fields'][1]['required_unless'], {'key': 'current_role', 'equals': True})
        self.assertEqual(item['codex_review_history'][-1]['fields'], item['fields'])
        self.codex('codex_authorize', revision=ready['revision'], job_ids=['job-1'], confirmed=True)
        task = self.claim()
        before = self.current()
        invalid = [None, {}, {'end_month': True}, {'end_month': True, 'end_year': False},
                   {'end_month': True, 'end_year': 1}, {'end_month': True, 'end_year': True, 'extra': True}]
        for verified in invalid:
            with self.subTest(verified=verified), self.assertRaisesRegex(ValueError, 'conditional requirement'):
                self.report(task, 'submitting', form_signature=task['form_signature'], verified_conditionals=verified)
            self.assertEqual(self.current(), before)
        with self.assertRaisesRegex(ValueError, 'checkpoint'):
            self.codex('codex_check', job_id='job-1', attempt_id=task['attempt_id'], phase='before_click')
        verified = {'end_month': True, 'end_year': True}
        self.report(task, 'submitting', form_signature=task['form_signature'], verified_conditionals=verified)
        raw = self.store.get(BATCH_KIND, self.batch['id'])['items'][0]
        self.assertEqual(raw['submission_materials']['verified_conditionals'], verified)
        self.assertIn('conditionals_verified_at', raw['submission_materials'])
        self.assertTrue(raw['submission_materials']['fields'][1]['required'])
        self.assertTrue(self.codex('codex_check', job_id='job-1', attempt_id=task['attempt_id'], phase='before_click')['can_click'])
        result = self.report(task, 'submitted', evidence={'url': task['url'], 'text': 'Application received.'})
        self.assertEqual(result['batch']['items'][0]['submitted_materials']['verified_conditionals'], verified)

    def test_false_or_unanswered_conditional_controller_does_not_waive_dates(self):
        self.prepare()
        self.context()
        for answer in (False, None, 'true'):
            with self.subTest(answer=answer):
                task = self.claim()
                planned = {} if answer is None else {'current_role': answer}
                result = self.report(task, 'inspected', observed_url=task['url'], fields=self.conditional_fields(),
                    planned_answers=planned)['batch']
                item = result['items'][0]
                self.assertEqual(item['status'], 'needs_input')
                self.assertIn('End month', item['missing_fields'])
                self.assertIn('End year', item['missing_fields'])
                with self.assertRaisesRegex(ValueError, 'ready form'):
                    self.codex('codex_authorize', revision=result['revision'], job_ids=['job-1'], confirmed=True)
                self.codex('codex_answers', job_id='job-1', answers={})
        task = self.claim()
        ready = self.report(task, 'inspected', observed_url=task['url'], fields=self.conditional_fields(),
            planned_answers={'current_role': False, 'end_month': 'January', 'end_year': '2020'})['batch']
        self.codex('codex_authorize', revision=ready['revision'], job_ids=['job-1'], confirmed=True)
        task = self.claim()
        self.report(task, 'submitting', form_signature=task['form_signature'])

    def test_conditional_schema_rejects_unreliable_controllers_and_malformed_rules(self):
        self.prepare()
        self.context()
        task = self.claim()
        invalid = [None, {}, {'key': 'current_role'}, {'key': 'current_role', 'equals': 'true'},
                   {'key': 'current_role', 'equals': 1}, {'key': 'current_role', 'equals': True, 'extra': True},
                   {'key': 'missing', 'equals': True}, {'key': 'end_month', 'equals': True}]
        for condition in invalid:
            fields = self.conditional_fields()
            fields[1]['required_unless'] = condition
            with self.subTest(condition=condition), self.assertRaisesRegex(ValueError, 'Conditional requirement'):
                self.report(task, 'inspected', observed_url=task['url'], fields=fields,
                    planned_answers={'current_role': True})
        for change in ({'type': 'text'}, {'supported': False}):
            fields = self.conditional_fields()
            fields[0].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'known supported checkbox'):
                self.report(task, 'inspected', observed_url=task['url'], fields=fields,
                    planned_answers={'current_role': True})
        fields = self.conditional_fields()
        fields[1]['required'] = False
        with self.assertRaisesRegex(ValueError, 'required field'):
            self.report(task, 'inspected', observed_url=task['url'], fields=fields,
                planned_answers={'current_role': True})

    def test_waiver_does_not_hide_unsupported_controls_or_invalid_supplied_values(self):
        self.prepare()
        self.context()
        task = self.claim()
        fields = self.conditional_fields()
        fields[2]['supported'] = False
        result = self.report(task, 'inspected', observed_url=task['url'], fields=fields,
            planned_answers={'current_role': True, 'end_month': 'Invented month'})['batch']['items'][0]
        self.assertEqual(result['status'], 'needs_input')
        self.assertIn('End year (complete in browser)', result['missing_fields'])
        self.assertIn('End month (choose a listed option)', result['missing_fields'])

    def test_bound_destination_preserves_listing_and_requires_exact_form_and_receipt(self):
        self.coinbase()
        bound = self.bind_destination()['batch']['items'][0]
        self.assertEqual(bound['url'], self.listing_url)
        self.assertEqual(bound['application_url'], self.destination_url)
        self.assertEqual(bound['status'], 'pending')
        self.assertEqual(bound['destination_binding']['observed_href'], self.destination_url)
        task = self.claim()
        self.assertEqual(task['url'], self.listing_url)
        self.assertEqual(task['application_url'], self.destination_url)
        for wrong in (self.listing_url, self.destination_url.replace('8304034', '999'),
                      self.destination_url.replace('coinbase&', 'different&'),
                      self.destination_url.replace('/embed/job_app', '/unrelated'),
                      'https://job-boards.greenhouse.io/embed/job_app/confirmation?for=coinbase&token=8304034'):
            with self.subTest(wrong=wrong), self.assertRaisesRegex(ValueError, 'different application'):
                self.report(task, 'inspected', observed_url=wrong,
                    fields=[{'key': 'name', 'type': 'text'}], planned_answers={'name': 'Ada'})
        ready = self.inspect_destination(task)
        self.codex('codex_authorize', revision=ready['revision'], job_ids=['job-1'], confirmed=True)
        task = self.claim()
        self.assertTrue(self.codex('codex_check', job_id='job-1', attempt_id=task['attempt_id'])['can_fill'])
        self.report(task, 'submitting', form_signature=task['form_signature'])
        self.assertTrue(self.codex('codex_check', job_id='job-1', attempt_id=task['attempt_id'], phase='before_click')['can_click'])
        for wrong in (self.listing_url, self.destination_url.replace('8304034', '999'),
                      'https://job-boards.greenhouse.io/other-role'):
            with self.subTest(wrong=wrong), self.assertRaisesRegex(ValueError, 'different application'):
                self.report(task, 'submitted', evidence={'url': wrong, 'text': 'Received.'})
        result = self.report(task, 'submitted', evidence={'url': self.destination_url, 'text': 'Application received.'})
        self.assertEqual(result['batch']['codex_run']['status'], 'complete')
        receipt = self.store.get('applications', 'job-1')['browser_submissions'][-1]
        self.assertFalse(receipt['manual'])
        self.assertEqual(receipt['evidence']['url'], self.destination_url)
        self.assertEqual(result['batch']['items'][0]['submitted_materials']['application_url'], self.destination_url)

    def test_bound_coinbase_receipt_accepts_only_exact_matching_confirmation_route(self):
        self.coinbase()
        self.bind_destination()
        ready = self.inspect_destination()
        self.codex('codex_authorize', revision=ready['revision'], job_ids=['job-1'], confirmed=True)
        task = self.claim()
        self.report(task, 'submitting', form_signature=task['form_signature'])
        self.codex('codex_check', job_id='job-1', attempt_id=task['attempt_id'], phase='before_click')
        confirmation = 'https://job-boards.greenhouse.io/embed/job_app/confirmation?for=coinbase&token=8304034'
        invalid = [confirmation.replace('8304034', '999'), confirmation.replace('coinbase&', 'different&'),
                   confirmation + '&gh_jid=8304034', confirmation + '&token=8304034',
                   confirmation.replace('for=coinbase&', ''), confirmation.replace('&token=8304034', ''),
                   confirmation.replace('https:', 'http:'),
                   confirmation.replace('job-boards.greenhouse.io', 'evil.example.com'),
                   confirmation.replace('/confirmation?', '/confirmation/other?'),
                   confirmation.replace('/confirmation?', '/confirmation/?'), confirmation + '#other']
        before = self.current()
        for wrong in invalid:
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                self.report(task, 'submitted', evidence={'url': wrong, 'text': 'Application received.'})
            self.assertEqual(self.current(), before)
        self.report(task, 'uncertain', message='Awaiting verified employer receipt; no repeat submission.')
        result = self.report(task, 'submitted', evidence={'url': confirmation,
            'text': 'Thank you for applying. Your application has been received.'})
        item = result['batch']['items'][0]
        self.assertEqual(item['status'], 'submitted')
        self.assertEqual(item['evidence']['url'], confirmation)
        self.assertEqual(item['application_url'], self.destination_url)
        self.assertEqual(item['submitted_materials']['application_url'], self.destination_url)
        self.assertEqual(len(self.store.get('applications', 'job-1')['browser_submissions']), 1)

    def test_binding_rejects_changed_role_company_and_unsafe_destinations(self):
        self.coinbase()
        invalid = [
            {'observed_company': 'Different'}, {'observed_title': 'Different'}, {'observed_role_id': '999'},
            {'listing_url': self.listing_url.replace('8304034', '999')},
            {'observed_href': self.destination_url.replace('8304034', '999')},
            {'destination_url': self.destination_url.replace('token=8304034', 'token=999')},
            {'destination_url': self.destination_url.replace('coinbase&', 'different&')},
            {'destination_url': self.destination_url + '&redirect=https://other.example.com'},
            {'destination_url': self.destination_url + '&token=8304034'},
            {'destination_url': self.destination_url.replace('https:', 'http:')},
            {'destination_url': 'https://127.0.0.1/embed/job_app'},
            {'destination_url': self.destination_url.replace('job-boards.greenhouse.io', 'evil.example.com')},
            {'destination_url': self.destination_url + '#other'},
            {'observed_href': self.destination_url + '#other'},
        ]
        before = self.current()
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.bind_destination(**payload)
            self.assertEqual(self.current(), before)

    def test_binding_requires_current_owner_revision_and_active_unsubmitted_state(self):
        self.coinbase()
        for payload in ({'worker_id': 'foreign'}, {'workspace_id': 'wrong'}, {'revision': -1}, {'job_id': 'job-2'}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.bind_destination(**payload)
        task = self.claim()
        with self.assertRaisesRegex(ValueError, 'Resolve the current'):
            self.bind_destination()
        self.expire()
        with self.assertRaisesRegex(ValueError, 'Resolve the current'):
            self.bind_destination()
        self.context()  # Explicit recovery, never revival of the expired attempt.
        paused = self.action('pause', batch_id=self.batch['id'], revision=self.current()['revision'])['batch']
        with self.assertRaisesRegex(ValueError, 'Resolve the current'):
            self.bind_destination()
        self.action('resume', batch_id=self.batch['id'], revision=paused['revision'])
        self.store.put('applications', 'job-1', {'id': 'job-1', 'job_id': 'job-1', 'stage': 'applied'})
        with self.assertRaisesRegex(ValueError, 'listing or application state changed'):
            self.bind_destination()

    def test_binding_invalidates_review_authorization_and_binds_snapshot(self):
        self.coinbase()
        reviewed = self.inspect()['batch']
        self.codex('codex_authorize', revision=reviewed['revision'], job_ids=['job-1'], confirmed=True)
        rebound = self.bind_destination()['batch']['items'][0]
        self.assertEqual(rebound['status'], 'pending')
        self.assertNotIn('authorization', rebound)
        self.assertNotIn('form_signature', rebound)
        self.assertEqual(len(rebound['codex_review_history']), 1)
        ready = self.inspect_destination()
        self.codex('codex_authorize', revision=ready['revision'], job_ids=['job-1'], confirmed=True)
        task = self.claim()
        raw = self.store.get(BATCH_KIND, self.batch['id'])
        raw['items'][0]['authorization']['application_url'] = self.listing_url
        self.store.put(BATCH_KIND, raw['id'], raw)
        with self.assertRaisesRegex(ValueError, 'authorized application destination changed'):
            self.codex('codex_check', job_id='job-1', attempt_id=task['attempt_id'])
        raw['items'][0]['authorization']['application_url'] = self.destination_url
        raw['items'][0]['application_url'] = self.listing_url
        self.store.put(BATCH_KIND, raw['id'], raw)
        with self.assertRaisesRegex(ValueError, 'reserved application materials changed'):
            self.context()

    def test_binding_cannot_change_after_click_or_uncertain_or_submitted(self):
        self.coinbase()
        self.bind_destination()
        ready = self.inspect_destination()
        self.codex('codex_authorize', revision=ready['revision'], job_ids=['job-1'], confirmed=True)
        task = self.claim()
        self.report(task, 'submitting', form_signature=task['form_signature'])
        self.codex('codex_check', job_id='job-1', attempt_id=task['attempt_id'], phase='before_click')
        with self.assertRaisesRegex(ValueError, 'Resolve the current'):
            self.bind_destination()
        self.report(task, 'uncertain', message='Outcome unclear; no retry.')
        with self.assertRaisesRegex(ValueError, 'Resolve the current'):
            self.bind_destination()
        self.report(task, 'submitted', evidence={'url': self.destination_url, 'text': 'Application received.'})
        with self.assertRaisesRegex(ValueError, 'Resolve the current'):
            self.bind_destination()

    def test_binding_rejects_listing_identity_change_and_unlisted_mapping(self):
        self.coinbase()
        job = self.store.get('jobs', 'job-1')
        job['title'] = 'Different role'
        self.store.put('jobs', 'job-1', job)
        with self.assertRaisesRegex(ValueError, 'listing or application state changed'):
            self.bind_destination()
        job['title'] = 'Senior Software Engineer'
        job['company'] = 'Different employer'
        self.store.put('jobs', 'job-1', job)
        raw = self.store.get(BATCH_KIND, self.batch['id'])
        raw['items'][0]['company'] = job['company']
        self.store.put(BATCH_KIND, raw['id'], raw)
        with self.assertRaisesRegex(ValueError, 'not a supported exact role mapping'):
            self.bind_destination(observed_company='Different employer')


if __name__ == '__main__':
    unittest.main()

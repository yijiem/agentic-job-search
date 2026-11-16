import base64
import contextlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jobflow.application_batches import (
    BATCH_KIND, LEASE_SECONDS, MAX_RESUME_BYTES, WORKER_KIND, batch_detail, batch_snapshot,
    perform_batch_action, resume_content, worker_release, workspace_id,
    _claim,
)
from jobflow.cli import main
from jobflow.storage import Store


class ApplicationBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data/jobflow.sqlite3')
        self.config = {'preparation': {'daily_minutes': 90}, 'schedule': {'timezone': 'America/Los_Angeles'}}
        for i in range(1, 4):
            self.store.put('jobs', 'job-' + str(i), {
                'id': 'job-' + str(i), 'company': 'Example', 'title': 'Engineer ' + str(i),
                'url': 'https://jobs.example.com/role/' + str(i), 'availability': 'open',
            })
        self.resume = self.action('upload_resume', filename='resume.pdf', content_base64=base64.b64encode(b'%PDF-1.4\nCandidate resume').decode())['resume']

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def action(self, op, **payload):
        return perform_batch_action(self.root, self.store, self.config, {}, {'op': op, **payload})

    def create(self, ids=None, **overrides):
        return self.action('create', **{
            'name': 'Top roles', 'job_ids': ids or ['job-1'], 'resume_id': self.resume['id'],
            'applicant': {'first_name': 'Ada', 'last_name': 'Example', 'email': 'ada@example.com'}, **overrides,
        })['batch']

    def get(self, batch):
        return batch_detail(self.root, batch['id'])

    def claim(self, batch, worker='worker-1'):
        # Exercise retained legacy recovery machinery directly. The public
        # extension claim route is retired and must never start new work.
        return _claim(self.root, self.store, {'worker_id': worker, 'batch_id': batch['id']})['task']

    def report(self, task, status, **payload):
        return self.action('report', batch_id=task['batch_id'], job_id=task['job_id'],
                           attempt_id=task['attempt_id'], status=status, **payload)

    def inspected(self, batch, fields=None, planned=None):
        task = self.claim(batch)
        self.assertEqual(task['mode'], 'inspect')
        self.assertNotIn('content_base64', task['resume'])
        fields = fields or [
            {'key': 'name', 'label': 'Name', 'type': 'text', 'required': True, 'supported': True},
            {'key': 'resume', 'label': 'Resume', 'type': 'file', 'required': True, 'supported': True},
        ]
        return self.report(task, 'inspected', fields=fields, form_signature='test-signature',
                           planned_answers=planned if planned is not None else {'name': 'Ada Example', 'resume': 'resume.pdf'})['batch']

    def authorized(self, batch):
        ready = self.inspected(batch)
        return self.action('authorize', batch_id=ready['id'], revision=ready['revision'],
                           confirmed=True, job_ids=['job-1'])['batch']

    def submission(self):
        batch = self.authorized(self.create())
        task = self.claim(batch)
        self.assertEqual(task['mode'], 'submit')
        self.report(task, 'submitting', form_signature=task['form_signature'])
        return batch, task

    def test_read_only_snapshot_does_not_create_an_empty_workspace(self):
        root = self.root / 'uninitialized'
        snapshot = batch_snapshot(root)
        self.assertEqual(snapshot['batches'], [])
        self.assertFalse(root.exists())
        self.assertEqual(len(snapshot['workspace_id']), 32)

    def test_resume_preview_matches_immutable_private_file(self):
        metadata, content = resume_content(self.root, self.resume['id'])
        self.assertEqual(metadata, self.resume)
        self.assertEqual(content, b'%PDF-1.4\nCandidate resume')
        path = self.root / 'inputs/application-resumes' / self.resume['id']
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('path', metadata)
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            resume_content(self.root, self.resume['id'])

    def test_resume_validation_rejects_invalid_files_paths_and_oversize(self):
        for filename, content in [('resume.exe', b'x'), ('../resume.pdf', b'%PDF-1.4'),
                                  ('resume\\other.pdf', b'%PDF-1.4'), ('resume.pdf', b'not pdf'),
                                  ('resume.txt', b'\x00binary'), ('resume.docx', b'not zip')]:
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                self.action('upload_resume', filename=filename, content_base64=base64.b64encode(content).decode())
        with self.assertRaises(ValueError):
            self.action('upload_resume', filename='resume.pdf', content_base64='!invalid!')
        with self.assertRaises(ValueError):
            self.action('upload_resume', filename='resume.pdf', content_base64=base64.b64encode(b'%PDF-' + b'a' * MAX_RESUME_BYTES).decode())
        self.assertEqual(len(batch_snapshot(self.root)['resumes']), 1)

    def test_docx_upload_accepts_container_and_rejects_macros(self):
        for macro in (False, True):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w') as archive:
                archive.writestr('[Content_Types].xml', '<Types/>')
                archive.writestr('word/document.xml', '<document/>')
                if macro:
                    archive.writestr('word/vbaProject.bin', b'macro')
            payload = base64.b64encode(stream.getvalue()).decode()
            if macro:
                with self.assertRaises(ValueError):
                    self.action('upload_resume', filename='resume.docx', content_base64=payload)
            else:
                self.assertEqual(self.action('upload_resume', filename='resume.docx', content_base64=payload)['resume']['filename'], 'resume.docx')

    def test_resume_symlink_and_foreign_workspace_are_rejected(self):
        path = self.root / 'inputs/application-resumes' / self.resume['id']
        original = path.read_bytes()
        outside = self.root / 'outside.pdf'
        outside.write_bytes(original)
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(ValueError):
            resume_content(self.root, self.resume['id'])
        with self.assertRaises(ValueError):
            resume_content(self.root / 'another-account', self.resume['id'])

    def test_symlinked_upload_directory_never_writes_outside_workspace(self):
        root = self.root / 'symlink-workspace'
        root.mkdir()
        (root / 'inputs').symlink_to(self.root / 'inputs', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symbolic'):
            perform_batch_action(root, self.store, self.config, {}, {'op': 'upload_resume', 'filename': 'resume.txt', 'content_base64': 'YQ=='})

    def test_create_skips_existing_applications_and_duplicate_batches(self):
        self.store.put('applications', 'job-1', {'id': 'job-1', 'job_id': 'job-1', 'stage': 'technical', 'notes': 'Keep me'})
        first = self.create(['job-1', 'job-2'])
        self.assertEqual([i['status'] for i in first['items']], ['skipped', 'pending'])
        second = self.create(['job-2', 'job-3'])
        self.assertEqual([i['status'] for i in second['items']], ['skipped', 'pending'])
        self.assertEqual(self.store.get('applications', 'job-1')['notes'], 'Keep me')

    def test_create_uses_tailored_resumes_for_the_selected_roles(self):
        alternate = self.action('upload_resume', filename='tailored.txt', content_base64='VGFpbG9yZWQ=')['resume']
        batch = self.create(['job-1', 'job-2', 'job-3'], items=[
            {'job_id': 'job-1', 'resume_id': alternate['id']},
            {'job_id': 'job-2', 'resume_id': ''},
        ])
        self.assertEqual(batch['resume_id'], self.resume['id'])
        self.assertEqual([item['resume_id'] for item in batch['items']],
                         [alternate['id'], self.resume['id'], self.resume['id']])
        self.assertEqual([item['resume_override'] for item in batch['items']], [True, False, False])
        self.assertEqual([item['status'] for item in batch['items']], ['pending'] * 3)
        self.assertEqual(self.store.all('applications'), [])
        changed = self.action('upload_resume', filename='default.txt', content_base64='RGVmYXVsdA==')['resume']
        batch = self.action('update', batch_id=batch['id'], revision=batch['revision'], resume_id=changed['id'])['batch']
        self.assertEqual([item['resume_id'] for item in batch['items']],
                         [alternate['id'], changed['id'], changed['id']])
        task = self.claim(batch)
        self.assertEqual(task['job_id'], 'job-1')
        self.assertEqual(task['resume']['id'], alternate['id'])
        self.assertEqual(task['mode'], 'inspect')
        self.assertNotIn('content_base64', task['resume'])

    def test_create_validates_role_resume_overrides_before_saving(self):
        other_root = self.root / 'other-account'
        other_store = Store(other_root / 'data/jobflow.sqlite3')
        try:
            foreign = perform_batch_action(other_root, other_store, self.config, {}, {
                'op': 'upload_resume', 'filename': 'foreign.txt', 'content_base64': 'Rm9yZWlnbg==',
            })['resume']
        finally:
            other_store.close()
        valid = {'job_id': 'job-1', 'resume_id': self.resume['id']}
        invalid_overrides = [
            None, {}, 'invalid', [valid] * 101, [None], [{}],
            [{'job_id': 'job-1'}],
            [{'job_id': 'job-1', 'resume_id': self.resume['id'], 'answers': {}}],
            [valid, valid],
            [valid, {'job_id': 'job-2', 'resume_id': self.resume['id']}],
            [valid, {'job_id': 'unknown', 'resume_id': self.resume['id']}],
            [{'job_id': 'job-1', 'resume_id': 'unknown-resume'}],
            [{'job_id': 'job-1', 'resume_id': foreign['id']}],
        ]
        invalid_overrides.extend([{'job_id': 'job-1', 'resume_id': value}] for value in (None, False, 0, '../resume'))
        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.create(items=overrides)
            self.assertEqual(self.store.all(BATCH_KIND), [])
            self.assertEqual(self.store.all('applications'), [])

    def test_invalid_selection_is_atomic_and_rejects_private_urls(self):
        with self.assertRaises(ValueError):
            self.create(['job-1', 'unknown'])
        self.assertEqual(self.store.all(BATCH_KIND), [])
        for url in ('http://jobs.example.com/a', 'https://localhost/a', 'https://127.0.0.1/a',
                    'https://10.0.0.1/a', 'https://example.local/a', 'https://user:password@jobs.example.com/a'):
            job = self.store.get('jobs', 'job-1')
            job['url'] = url
            self.store.put('jobs', 'job-1', job)
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.create()

    def test_closed_role_skips_but_missing_role_still_inspects(self):
        for job_id, status in [('job-1', 'closed'), ('job-2', 'missing')]:
            job = self.store.get('jobs', job_id)
            self.store.put('jobs', job_id, {**job, 'availability': status})
        batch = self.create(['job-1', 'job-2'])
        self.assertEqual([i['status'] for i in batch['items']], ['skipped', 'pending'])

    def test_claim_only_exposes_resume_bytes_after_explicit_authorization(self):
        batch = self.create()
        with self.assertRaises(ValueError):
            self.action('authorize', batch_id=batch['id'], revision=batch['revision'], confirmed=True, job_ids=['job-1'])
        ready = self.inspected(batch)
        with self.assertRaises(ValueError):
            self.action('authorize', batch_id=ready['id'], revision=ready['revision'], confirmed=False, job_ids=['job-1'])
        authorized = self.action('authorize', batch_id=ready['id'], revision=ready['revision'], confirmed=True, job_ids=['job-1'])['batch']
        task = self.claim(authorized)
        self.assertEqual(base64.b64decode(task['resume']['content_base64']), b'%PDF-1.4\nCandidate resume')
        snapshot = json.dumps(batch_snapshot(self.root))
        self.assertNotIn(task['attempt_id'], snapshot)
        self.assertNotIn('content_base64', snapshot)
        self.assertNotIn('authorization', snapshot)

    def test_unknown_required_question_requires_answer_and_fresh_review(self):
        batch = self.create()
        fields = [{'key': 'sponsor', 'label': 'Need sponsorship?', 'type': 'select', 'required': True, 'options': ['Yes', 'No']}]
        batch = self.inspected(batch, fields=fields, planned={})
        self.assertEqual(batch['items'][0]['status'], 'needs_input')
        self.assertEqual(batch['items'][0]['missing_fields'], ['Need sponsorship?'])
        updated = self.action('update', batch_id=batch['id'], revision=batch['revision'],
                              items=[{'job_id': 'job-1', 'answers': {'sponsor': 'No'}}])['batch']
        self.assertEqual(updated['items'][0]['status'], 'pending')
        task = self.claim(updated)
        self.assertEqual(task['answers']['sponsor'], 'No')

    def test_required_checkbox_never_inferred_as_consent(self):
        batch = self.inspected(self.create(), fields=[{'key': 'agree', 'label': 'I agree', 'type': 'checkbox', 'required': True}], planned={'agree': False})
        self.assertEqual(batch['items'][0]['status'], 'needs_input')

    def test_unknown_optional_control_requires_browser_attention(self):
        batch = self.inspected(self.create(), fields=[{'key': 'unknown', 'label': 'Custom control', 'type': 'custom', 'required': False, 'supported': False}], planned={})
        self.assertEqual(batch['items'][0]['status'], 'needs_input')

    def test_ready_validation_matches_worker_field_types(self):
        cases = [('email', 'invalid-email'), ('checkbox', 'false'), ('text', ['multiple']), ('file', 'different.pdf')]
        for field_type, answer in cases:
            batch = self.create(['job-3'])
            inspected = self.inspected(batch, fields=[{'key': 'field', 'label': 'Field', 'type': field_type, 'required': True}], planned={'field': answer})
            self.assertEqual(inspected['items'][0]['status'], 'needs_input')
            self.action('cancel', batch_id=inspected['id'], revision=inspected['revision'])

    def test_individual_review_edit_does_not_interrupt_other_roles_inspecting(self):
        batch = self.create(['job-1', 'job-2'])
        first = self.inspected(batch)
        other_task = self.claim(first)
        self.assertEqual(other_task['job_id'], 'job-2')
        current = self.get(first)
        with self.assertRaises(ValueError):
            self.action('update', batch_id=current['id'], revision=current['revision'], applicant={'first_name': 'Different'})
        edited = self.action('update', batch_id=current['id'], revision=current['revision'],
                             items=[{'job_id': 'job-1', 'answers': {'name': 'Ada Reviewed'}}])['batch']
        self.assertEqual([i['status'] for i in edited['items']], ['pending', 'inspecting'])
        self.report(other_task, 'inspected', fields=[{'key': 'name', 'label': 'Name', 'type': 'text', 'required': True}],
                    form_signature='other-form', planned_answers={'name': 'Ada Example'})
        self.assertEqual(self.get(edited)['items'][1]['status'], 'ready')

    def test_role_resume_can_inherit_or_override_batch_resume(self):
        alternate = self.action('upload_resume', filename='other.txt', content_base64='T3RoZXI=')['resume']
        batch = self.create(['job-1', 'job-2'])
        batch = self.action('update', batch_id=batch['id'], revision=batch['revision'], items=[{'job_id': 'job-1', 'resume_id': alternate['id']}])['batch']
        batch = self.action('update', batch_id=batch['id'], revision=batch['revision'], resume_id=alternate['id'])['batch']
        self.assertEqual([i['resume_id'] for i in batch['items']], [alternate['id'], alternate['id']])
        batch = self.action('update', batch_id=batch['id'], revision=batch['revision'], resume_id=self.resume['id'],
                            items=[{'job_id': 'job-1', 'resume_id': ''}])['batch']
        self.assertEqual([i['resume_id'] for i in batch['items']], [self.resume['id'], self.resume['id']])

    def test_worker_workspace_binding_is_checked_in_core(self):
        with self.assertRaisesRegex(ValueError, 'different.*workspace'):
            self.action('claim', worker_id='worker-1', workspace_id='other-account')

    def test_listing_changes_after_authorization_prevent_submission(self):
        batch = self.authorized(self.create())
        job = self.store.get('jobs', 'job-1')
        self.store.put('jobs', 'job-1', {**job, 'availability': 'closed'})
        self.assertIsNone(self.claim(batch))
        self.assertEqual(self.get(batch)['items'][0]['status'], 'needs_input')

    def test_listing_changes_after_claim_prevent_submission_gate(self):
        batch = self.authorized(self.create())
        task = self.claim(batch)
        job = self.store.get('jobs', 'job-1')
        self.store.put('jobs', 'job-1', {**job, 'url': 'https://jobs.example.com/another-role'})
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.report(task, 'submitting', form_signature=task['form_signature'])

    def test_stale_review_revision_cannot_authorize_changed_answers(self):
        ready = self.inspected(self.create())
        changed = self.action('update', batch_id=ready['id'], revision=ready['revision'], applicant={'first_name': 'Updated'})['batch']
        self.assertEqual(changed['items'][0]['status'], 'pending')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.action('authorize', batch_id=ready['id'], revision=ready['revision'], confirmed=True, job_ids=['job-1'])

    def test_worker_cannot_claim_twice_or_replace_reviewed_form_during_submission(self):
        batch = self.authorized(self.create())
        task = self.claim(batch)
        self.assertIsNone(self.claim(batch))
        with self.assertRaises(ValueError):
            self.report(task, 'inspected', fields=[{'key': 'changed', 'label': 'Changed'}], form_signature='changed', planned_answers={})
        with self.assertRaises(ValueError):
            self.report(task, 'submitting', form_signature='changed')
        self.assertEqual(self.get(batch)['items'][0]['status'], 'running')

    def test_pause_prevents_queued_claim_and_inflight_submit_gate(self):
        batch = self.authorized(self.create())
        paused = self.action('pause', batch_id=batch['id'], revision=batch['revision'])['batch']
        self.assertIsNone(self.claim(paused))
        resumed = self.action('resume', batch_id=paused['id'], revision=paused['revision'])['batch']
        task = self.claim(resumed)
        current = self.get(resumed)
        self.action('pause', batch_id=current['id'], revision=current['revision'])
        with self.assertRaisesRegex(ValueError, 'authorized'):
            self.report(task, 'submitting', form_signature=task['form_signature'])

    def test_submit_confirmation_updates_tracker_atomically_and_preserves_user_work(self):
        self.store.put('applications', 'job-1', {'id': 'job-1', 'job_id': 'job-1', 'stage': 'saved', 'notes': 'Important notes', 'custom_history': ['keep']})
        batch, task = self.submission()
        result = self.report(task, 'submitted', evidence={'url': task['url'], 'text': 'Your application has been received.'})
        self.assertTrue(result['application_updated'])
        self.assertEqual(result['batch']['status'], 'complete')
        self.assertEqual(result['batch']['items'][0]['submitted_materials']['resume_id'], self.resume['id'])
        self.assertEqual(result['batch']['items'][0]['submitted_materials']['planned_answers']['name'], 'Ada Example')
        application = self.store.get('applications', 'job-1')
        self.assertEqual(application['stage'], 'applied')
        self.assertEqual(application['notes'], 'Important notes')
        self.assertEqual(application['custom_history'], ['keep'])
        self.assertEqual(len(application['browser_submissions']), 1)
        self.assertTrue(application['applied_at'])
        with self.assertRaises(ValueError):
            self.report(task, 'submitted', evidence={'url': task['url'], 'text': 'Your application has been received.'})
        self.assertEqual(len(self.store.get('applications', 'job-1')['browser_submissions']), 1)

    def test_delayed_receipt_preserves_later_stage_and_original_timestamp(self):
        batch, task = self.submission()
        original = {'id': 'job-1', 'job_id': 'job-1', 'stage': 'technical', 'applied_at': '2026-01-01T10:00:00-08:00', 'notes': 'User updated while request was in flight'}
        self.store.put('applications', 'job-1', original)
        self.report(task, 'submitted', evidence={'url': task['url'], 'text': 'Application submitted.'})
        current = self.store.get('applications', 'job-1')
        for key, value in original.items():
            self.assertEqual(current[key], value)

    def test_undated_historical_record_stays_undated_when_delayed_receipt_arrives(self):
        batch, task = self.submission()
        self.store.put('applications', 'job-1', {'id': 'job-1', 'job_id': 'job-1', 'stage': 'technical'})
        self.report(task, 'submitted', evidence={'url': task['url'], 'text': 'Application submitted.'})
        self.assertNotIn('applied_at', self.store.get('applications', 'job-1'))

    def test_no_confirmation_before_submit_or_with_foreign_evidence(self):
        batch = self.authorized(self.create())
        task = self.claim(batch)
        with self.assertRaises(ValueError):
            self.report(task, 'submitted', evidence={'url': task['url'], 'text': 'Application received.'})
        self.report(task, 'submitting', form_signature=task['form_signature'])
        with self.assertRaises(ValueError):
            self.report(task, 'submitted', evidence={'url': 'https://evil.example.com/', 'text': 'Application received.'})
        self.assertEqual(self.store.all('applications'), [])

    def test_failure_after_click_becomes_uncertain_and_never_retries(self):
        batch, task = self.submission()
        uncertain = self.report(task, 'failed', message='Connection lost after click')['batch']
        self.assertEqual(uncertain['items'][0]['status'], 'uncertain')
        self.assertIsNone(self.claim(uncertain))
        self.assertEqual(self.store.all('applications'), [])
        duplicate = self.create()
        self.assertEqual(duplicate['items'][0]['status'], 'skipped')

    def test_expired_submission_claim_never_becomes_pending(self):
        batch, task = self.submission()
        raw = self.store.get(BATCH_KIND, batch['id'])
        raw['items'][0]['lease_until'] = (datetime.now(timezone.utc) - timedelta(seconds=LEASE_SECONDS)).isoformat()
        self.store.put(BATCH_KIND, batch['id'], raw)
        self.assertIsNone(self.claim(batch))
        self.assertEqual(self.get(batch)['items'][0]['status'], 'uncertain')
        self.report(task, 'submitted', evidence={'url': task['url'], 'text': 'Application received.'})
        self.assertEqual(self.store.get('applications', 'job-1')['stage'], 'applied')
        self.assertEqual(self.get(batch)['items'][0]['submitted_materials']['resume_id'], self.resume['id'])

    def test_expired_preclick_attempt_cannot_pass_submit_gate(self):
        batch = self.authorized(self.create())
        task = self.claim(batch)
        raw = self.store.get(BATCH_KIND, batch['id'])
        raw['items'][0]['lease_until'] = '2020-01-01T00:00:00+00:00'
        self.store.put(BATCH_KIND, batch['id'], raw)
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.report(task, 'submitting', form_signature=task['form_signature'])

    def test_uncertain_retry_requires_explicit_reconciliation_evidence_and_reinspection(self):
        batch, task = self.submission()
        batch = self.report(task, 'uncertain', message='No receipt')['batch']
        with self.assertRaises(ValueError):
            self.action('update', batch_id=batch['id'], revision=batch['revision'], items=[{'job_id': 'job-1'}])
        with self.assertRaises(ValueError):
            self.action('reconcile', batch_id=batch['id'], revision=batch['revision'], job_id='job-1', outcome='not_submitted', confirmed=False,
                        evidence={'url': task['url'], 'text': 'Checked employer portal; no application.'})
        checked = self.action('reconcile', batch_id=batch['id'], revision=batch['revision'], job_id='job-1', outcome='not_submitted', confirmed=True,
                              evidence={'url': task['url'], 'text': 'Checked employer portal; no application.'})['batch']
        self.assertEqual(checked['items'][0]['status'], 'pending')
        self.assertEqual(self.claim(checked)['mode'], 'inspect')

    def test_cancel_during_submission_retains_uncertain_outcome_and_can_settle(self):
        batch, task = self.submission()
        batch = self.get(batch)
        cancelled = self.action('cancel', batch_id=batch['id'], revision=batch['revision'])['batch']
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertEqual(cancelled['items'][0]['status'], 'uncertain')
        self.report(task, 'submitted', evidence={'url': task['url'], 'text': 'Application received.'})
        self.assertEqual(self.store.get('applications', 'job-1')['stage'], 'applied')

    def test_manual_submission_of_unsupported_form_requires_explicit_receipt(self):
        batch = self.create()
        task = self.claim(batch)
        batch = self.report(task, 'needs_input', message='Complete CAPTCHA and application manually')['batch']
        evidence = {'url': task['url'], 'text': 'I completed the CAPTCHA, submitted, and saw Application received.'}
        with self.assertRaises(ValueError):
            self.action('reconcile', batch_id=batch['id'], revision=batch['revision'], job_id='job-1', outcome='submitted', confirmed=False, evidence=evidence)
        with self.assertRaises(ValueError):
            self.action('reconcile', batch_id=batch['id'], revision=batch['revision'], job_id='job-1', outcome='not_submitted', confirmed=True, evidence=evidence)
        result = self.action('reconcile', batch_id=batch['id'], revision=batch['revision'], job_id='job-1', outcome='submitted', confirmed=True, evidence=evidence)
        self.assertEqual(result['batch']['items'][0]['status'], 'submitted')
        self.assertNotIn('submitted_materials', result['batch']['items'][0], 'Manual form edits are not claimed to match a frozen automated payload')
        application = self.store.get('applications', 'job-1')
        self.assertTrue(application['browser_submissions'][0]['manual'])

    def test_manual_reconciliation_does_not_override_running_worker(self):
        batch = self.create()
        task = self.claim(batch)
        current = self.get(batch)
        with self.assertRaises(ValueError):
            self.action('reconcile', batch_id=batch['id'], revision=current['revision'], job_id='job-1', outcome='submitted', confirmed=True,
                        evidence={'url': task['url'], 'text': 'Manual receipt'})

    def test_explicit_manual_receipt_can_record_an_employer_to_ats_redirect(self):
        batch = self.create()
        task = self.claim(batch)
        batch = self.report(task, 'needs_input', message='Employer redirected to another application host')['batch']
        evidence = {'url': 'https://ats.example.org/jobs/role-1/confirmation',
                    'text': 'I followed the employer application link and received this ATS confirmation.'}
        result = self.action('reconcile', batch_id=batch['id'], revision=batch['revision'], job_id='job-1', outcome='submitted', confirmed=True, evidence=evidence)
        self.assertEqual(result['batch']['items'][0]['evidence']['url'], evidence['url'])
        receipt = self.store.get('applications', 'job-1')['browser_submissions'][0]
        self.assertTrue(receipt['manual'])

    def test_cli_dispatch_produces_clean_json_and_uses_workspace(self):
        directory = self.root / 'config'
        directory.mkdir()
        repository = Path(__file__).resolve().parents[1]
        for name in ('settings', 'profile'):
            (directory / (name + '.json')).write_bytes((repository / 'config' / (name + '.example.json')).read_bytes())
        request = self.root / 'request.json'
        request.write_text(json.dumps({'op': 'create', 'name': 'CLI batch', 'job_ids': ['job-1'], 'resume_id': self.resume['id']}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(['--root', str(self.root), 'batch', '--request', str(request)])
        result = json.loads(output.getvalue())
        self.assertEqual(result['batch']['name'], 'CLI batch')
        self.assertTrue((self.root / 'data/workflow.lock').exists())
        self.assertFalse((self.root / 'reports').exists())

    def worker_action(self, op, worker='worker-1', **payload):
        return self.action(op, workspace_id=workspace_id(self.root), worker_id=worker, **payload)

    def heartbeat(self, worker='worker-1', **payload):
        return self.worker_action('worker_heartbeat', worker, **{
            'version': worker_release()['version'], 'reload_supported': True,
            'enabled': False, 'active': False, **payload})

    def test_worker_reload_keeps_materials_and_authorization_until_idle_reload_finishes(self):
        batch = self.authorized(self.create())
        before = self.store.get(BATCH_KIND, batch['id'])
        self.heartbeat(enabled=True)
        requested = self.worker_action('worker_reload')['reload_request']
        self.assertEqual(requested['status'], 'pending')
        self.assertEqual(self.worker_action('worker_reload')['reload_request']['id'], requested['id'])
        self.assertIsNone(self.claim(batch), 'Pending reload cannot start an authorized application')
        self.assertEqual(self.store.get(BATCH_KIND, batch['id']), before)
        state = batch_snapshot(self.root)
        self.assertEqual(state['worker_release'], worker_release())
        self.assertEqual(state['worker']['version'], worker_release()['version'])
        self.assertTrue(state['worker']['reload_supported'])
        self.assertEqual(state['worker']['reload_request']['id'], requested['id'])
        acknowledged = self.worker_action('worker_reload_ack', request_id=requested['id'])
        self.assertEqual(acknowledged['reload_request']['status'], 'reloading')
        self.assertIsNone(self.claim(batch), 'Reload acknowledgement also blocks task claims')
        self.assertEqual(self.worker_action('worker_reload')['reload_request']['id'], requested['id'])
        completed = self.heartbeat(reload_completed_id=requested['id'])
        self.assertEqual(completed['reload_request']['status'], 'complete')
        self.assertEqual(self.heartbeat(reload_completed_id=requested['id'])['reload_request'], completed['reload_request'])
        self.assertEqual(self.store.get(BATCH_KIND, batch['id']), before)
        self.assertEqual(self.claim(batch)['mode'], 'submit', 'The original reviewed authorization remains available')
        self.assertEqual(self.store.all('applications'), [])

    def test_worker_reload_waits_for_both_browser_and_server_active_steps(self):
        batch = self.create()
        task = self.claim(batch)
        self.heartbeat(active=False)
        request = self.worker_action('worker_reload')['reload_request']
        with self.assertRaisesRegex(ValueError, 'active application'):
            self.worker_action('worker_reload_ack', request_id=request['id'])
        self.assertEqual(self.get(batch)['items'][0]['status'], 'inspecting')
        self.report(task, 'needs_input', message='Finish the employer step manually')
        self.heartbeat(active=True)
        with self.assertRaisesRegex(ValueError, 'active application'):
            self.worker_action('worker_reload_ack', request_id=request['id'])
        self.heartbeat(active=False)
        self.assertEqual(self.worker_action('worker_reload_ack', request_id=request['id'])['reload_request']['status'], 'reloading')

    def test_worker_reload_requests_and_completions_are_scoped_to_worker(self):
        self.heartbeat()
        first = self.worker_action('worker_reload')['reload_request']
        other = self.heartbeat('worker-2')
        self.assertIsNone(other['reload_request'])
        with self.assertRaisesRegex(ValueError, 'belong'):
            self.worker_action('worker_reload_ack', 'worker-2', request_id=first['id'])
        with self.assertRaisesRegex(ValueError, 'match'):
            self.heartbeat('worker-2', reload_completed_id=first['id'])
        with self.assertRaisesRegex(ValueError, 'belong'):
            self.worker_action('worker_reload_ack', request_id='reload-wrong')
        with self.assertRaisesRegex(ValueError, 'acknowledged'):
            self.heartbeat(reload_completed_id=first['id'])
        second = self.worker_action('worker_reload', 'worker-2')['reload_request']
        self.assertNotEqual(second['id'], first['id'])
        self.assertEqual(self.heartbeat()['reload_request']['id'], first['id'])
        self.assertEqual(self.heartbeat('worker-2')['reload_request']['id'], second['id'])

    def test_worker_reload_reports_old_files_instead_of_claiming_an_update(self):
        self.heartbeat()
        request = self.worker_action('worker_reload')['reload_request']
        self.worker_action('worker_reload_ack', request_id=request['id'])
        result = self.heartbeat(version='0.0.1', reload_completed_id=request['id'])
        self.assertEqual(result['reload_request']['status'], 'outdated')
        self.assertIn('installed files', result['reload_request']['message'])
        self.assertEqual(batch_snapshot(self.root)['worker']['version'], '0.0.1')
        self.assertEqual(self.heartbeat(version='0.0.1', reload_completed_id=request['id'])['reload_request'], result['reload_request'])
        self.assertNotEqual(self.worker_action('worker_reload')['reload_request']['id'], request['id'])

    def test_worker_reload_failure_allows_explicit_retry_without_application_changes(self):
        batch = self.authorized(self.create())
        before = self.store.get(BATCH_KIND, batch['id'])
        self.heartbeat()
        for acknowledged in (False, True):
            with self.subTest(acknowledged=acknowledged):
                request = self.worker_action('worker_reload')['reload_request']
                if acknowledged:
                    self.worker_action('worker_reload_ack', request_id=request['id'])
                failed = self.heartbeat(reload_failed_id=request['id'])['reload_request']
                self.assertEqual(failed['status'], 'failed')
                self.assertIn('request another reload', failed['message'])
                self.assertEqual(self.heartbeat(reload_failed_id=request['id'])['reload_request'], failed)
                retry = self.worker_action('worker_reload')['reload_request']
                self.assertEqual(retry['status'], 'pending')
                self.assertNotEqual(retry['id'], request['id'])
                self.assertEqual(self.store.get(BATCH_KIND, batch['id']), before)
                self.assertEqual(self.store.all('applications'), [])

    def test_worker_reload_failure_is_scoped_and_cannot_overwrite_terminal_results(self):
        self.heartbeat()
        request = self.worker_action('worker_reload')['reload_request']
        before = self.store.get(WORKER_KIND, 'worker-1')
        with self.assertRaisesRegex(ValueError, 'not both'):
            self.heartbeat(reload_completed_id=request['id'], reload_failed_id=request['id'])
        with self.assertRaisesRegex(ValueError, 'match'):
            self.heartbeat('worker-2', reload_failed_id=request['id'])
        with self.assertRaisesRegex(ValueError, 'match'):
            self.heartbeat(reload_failed_id='reload-wrong')
        self.assertEqual(self.store.get(WORKER_KIND, 'worker-1'), before)
        self.assertIsNone(self.store.get(WORKER_KIND, 'worker-2'))
        for status in ('complete', 'outdated', 'expired'):
            with self.subTest(status=status):
                record = self.store.get(WORKER_KIND, 'worker-1')
                record['reload_request']['status'] = status
                self.store.put(WORKER_KIND, 'worker-1', record)
                result = self.heartbeat(reload_failed_id=request['id'])['reload_request']
                self.assertEqual(result, record['reload_request'])

    def test_worker_reload_expiration_is_read_only_and_allows_an_explicit_retry(self):
        batch = self.create()
        self.heartbeat()
        for status in ('pending', 'reloading'):
            with self.subTest(status=status):
                request = self.worker_action('worker_reload')['reload_request']
                record = self.store.get(WORKER_KIND, 'worker-1')
                record['reload_request'].update(status=status, expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
                self.store.put(WORKER_KIND, record['id'], record)
                self.store.put('application_worker', 'current', {**record, 'id': 'current'})
                self.assertEqual(batch_snapshot(self.root)['worker']['reload_request']['status'], 'expired')
                self.assertEqual(self.store.get(WORKER_KIND, 'worker-1')['reload_request']['status'], status, 'Snapshot does not write history')
                with self.assertRaisesRegex(ValueError, 'no longer pending'):
                    self.worker_action('worker_reload_ack', request_id=request['id'])
                self.assertEqual(self.heartbeat(reload_completed_id=request['id'])['reload_request']['status'], 'expired')
                fresh = self.worker_action('worker_reload')['reload_request']
                self.assertNotEqual(fresh['id'], request['id'])
        record = self.store.get(WORKER_KIND, 'worker-1')
        record['reload_request']['expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.store.put(WORKER_KIND, record['id'], record)
        self.assertIsNotNone(self.claim(batch), 'Expired control requests do not permanently block work')

    def test_worker_reload_requires_a_fresh_capable_worker_and_exact_workspace(self):
        with self.assertRaisesRegex(ValueError, 'manually once'):
            self.worker_action('worker_reload')
        self.heartbeat(reload_supported=False)
        with self.assertRaisesRegex(ValueError, 'manually once'):
            self.worker_action('worker_reload')
        self.heartbeat()
        record = self.store.get(WORKER_KIND, 'worker-1')
        record['last_seen_at'] = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        self.store.put(WORKER_KIND, record['id'], record)
        with self.assertRaisesRegex(ValueError, 'not connected'):
            self.worker_action('worker_reload')
        for op in ('worker_heartbeat', 'worker_reload', 'worker_reload_ack'):
            with self.subTest(op=op), self.assertRaisesRegex(ValueError, 'workspace'):
                self.action(op, worker_id='worker-1')
            with self.subTest(op=op), self.assertRaisesRegex(ValueError, 'workspace'):
                self.action(op, workspace_id='another-workspace', worker_id='worker-1')
        for payload in ({'version': 'invalid'}, {'active': 'false'}, {'enabled': 1}, {'reload_supported': None}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.heartbeat(**payload)

    def test_legacy_worker_claims_still_work_and_preserve_new_metadata(self):
        batch = self.create()
        self.assertIsNotNone(self.claim(batch))
        self.assertEqual(batch_snapshot(self.root)['worker']['worker_id'], 'worker-1')
        self.assertNotIn('reload_supported', batch_snapshot(self.root)['worker'])
        self.heartbeat()
        self.assertIsNone(self.claim(batch))
        state = batch_snapshot(self.root)['worker']
        self.assertEqual(state['version'], worker_release()['version'])
        self.assertTrue(state['reload_supported'])


if __name__ == '__main__':
    unittest.main()

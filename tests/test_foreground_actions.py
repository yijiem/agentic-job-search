"""User saves remain durable while an unrelated search owns workflow.lock."""
import base64
import fcntl
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobflow.storage import Store
from jobflow.web import WebError, perform_action


class ForegroundActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        repository = Path(__file__).resolve().parents[1]
        for name in ('profile', 'settings'):
            shutil.copyfile(repository / 'config' / (name + '.example.json'), self.root / 'config' / (name + '.json'))
        self.store = Store(self.root / 'data/jobflow.sqlite3')
        self.addCleanup(self.store.close)
        self.store.put('jobs', 'job-1', {'id': 'job-1', 'company': 'Example', 'title': 'Engineer',
            'url': 'https://example.test/jobs/1', 'description': 'Recorded role', 'availability': 'open'})
        self.lock = (self.root / 'data/workflow.lock').open('a')
        self.addCleanup(self.lock.close)
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def action(self, op, **values):
        return perform_action(self.root, {'action': 'batch', 'op': op, **values})

    def test_save_role_commits_and_requests_projection_without_broad_read(self):
        with patch('jobflow.web._read_records', side_effect=AssertionError('full snapshot')):
            result = perform_action(self.root, {'action': 'application', 'job_id': 'job-1', 'stage': 'saved', 'notes': 'Keep this'})
        self.assertEqual(result['applications'][0]['notes'], 'Keep this')
        self.assertEqual(self.store.get('applications', 'job-1')['stage'], 'saved')
        self.assertIsNotNone(self.store.get('projection_requests', 'current'))
        self.assertFalse((self.root / 'reports').exists())

    def test_resume_upload_batch_create_and_annotations_ignore_search_lock(self):
        content = base64.b64encode(b'%PDF-1.7\nRecorded resume\n%%EOF\n').decode()
        resume = self.action('upload_resume', filename='resume.pdf', content_base64=content)['resume']
        batch = self.action('create', job_ids=['job-1'], resume_id=resume['id'], name='My batch')['batch']
        self.assertEqual(batch['items'][0]['job_id'], 'job-1')
        text = 'Built a recorded system.'
        document = {'text': text, 'pages': [{'number': 1, 'width': 612, 'height': 792}],
                    'lines': [{'page': 1, 'start': 0, 'end': len(text), 'text': text, 'rect': [.1, .1, .8, .05]}]}
        review = self.action('pdf_review_create', resume_id=resume['id'], document=document)['review']
        note = {'kind': 'note', 'start': 0, 'end': len(text), 'before': text, 'note': 'Keep this evidence.',
                'page': 1, 'rects': [[.1, .1, .8, .05]], 'status': 'accepted', 'source': 'manual'}
        saved = self.action('pdf_review_update', review_id=review['id'], revision=review['revision'], annotations=[note])['review']
        self.assertEqual(saved['annotations'][0]['note'], note['note'])
        self.assertEqual(self.store.get('resume_reviews', review['id'])['revision'], saved['revision'])

    def test_profile_response_reads_its_committed_overlay(self):
        result = self.action('resume_profile', applicant={'first_name': 'Reviewed'})
        self.assertEqual(result['applicant']['first_name'], 'Reviewed')
        self.assertEqual(self.store.get('resume_profile', 'default')['applicant']['first_name'], 'Reviewed')

    def test_invalid_bulk_save_rolls_back_all_records(self):
        with self.assertRaises(WebError):
            perform_action(self.root, {'action': 'application', 'job_ids': ['job-1', 'missing'], 'stage': 'saved'})
        self.assertIsNone(self.store.get('applications', 'job-1'))
        self.assertIsNone(self.store.get('projection_requests', 'current'))

    def test_submission_operations_are_not_foreground_commands(self):
        from jobflow.foreground import supported
        for operation in ('codex_prepare', 'codex_report', 'authorize', 'report', 'reconcile'):
            self.assertFalse(supported({'action': 'batch', 'op': operation}))
        self.assertFalse(supported({'action': 'batch', 'op': 'pdf_review_suggest', 'mode': 'ai'}))

    def test_malformed_action_names_cannot_enter_foreground_path(self):
        from jobflow.foreground import supported
        for payload in (None, [], {'action': []}, {'action': {}},
                        {'action': 'batch', 'op': []}, {'action': 'batch', 'op': {}}):
            self.assertFalse(supported(payload))


if __name__ == '__main__':
    unittest.main()

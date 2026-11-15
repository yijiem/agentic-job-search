import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobflow.record_concurrency import optimistic_store
from jobflow.storage import Store, RecordConflictError


class LocalRecordTransactions(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.environment = patch.dict(os.environ, {'JOBFLOW_BACKEND': 'local'})
        self.environment.start()
        self.worker = Store(self.root / 'data/jobflow.sqlite3')
        self.user = Store(self.root / 'data/jobflow.sqlite3')

    def tearDown(self):
        self.worker.close()
        self.user.close()
        self.environment.stop()
        self.directory.cleanup()

    def test_foreground_transaction_does_not_take_workflow_file_lock(self):
        import fcntl
        with (self.root / 'data/workflow.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.user.optimistic():
                self.assertIs(self.user, optimistic_store(self.root))
                self.user.put('applications', 'a', {'id': 'a', 'stage': 'saved'})
                self.user.put('resume_reviews', 'r', {'id': 'r', 'revision': 1})
                self.user.request_refresh()
                self.assertIsNone(self.worker.get('applications', 'a'))
            self.assertIsNone(optimistic_store(self.root))
        self.assertEqual('saved', self.worker.get('applications', 'a')['stage'])
        self.assertIsNotNone(self.worker.get('projection_requests', 'current'))

    def test_failed_validation_discards_entire_command(self):
        with self.assertRaisesRegex(ValueError, 'invalid'):
            with self.user.optimistic():
                self.user.put('applications', 'a', {'id': 'a'})
                self.user.request_refresh()
                raise ValueError('invalid')
        self.assertIsNone(self.worker.get('applications', 'a'))
        self.assertIsNone(self.worker.get('projection_requests', 'current'))

    def test_worker_preserves_foreground_completion_and_does_not_publish_stale_plan(self):
        self.worker.put('tasks', 't', {'id': 't', 'status': 'todo', 'notes': ''})
        tasks = self.worker.all('tasks')
        with self.user.optimistic():
            task = self.user.get('tasks', 't')
            task.update(status='done', completed_at='2026-11-15T12:00:00-08:00', notes='Keep this')
            self.user.put('tasks', 't', task)
        tasks[0]['due_date'] = '2026-11-16'
        with self.assertRaises(RecordConflictError):
            self.worker.put_groups({'tasks': tasks, 'plans': [{'id': 'current'}]})
        self.assertEqual('done', self.user.get('tasks', 't')['status'])
        self.assertEqual('Keep this', self.user.get('tasks', 't')['notes'])
        self.assertIsNone(self.user.get('plans', 'current'))

    def test_missing_record_expectation_prevents_duplicate_creation(self):
        with self.assertRaises(RecordConflictError):
            with self.user.optimistic():
                self.assertIsNone(self.user.get('resume_reviews', 'r'))
                self.worker.put('resume_reviews', 'r', {'id': 'r', 'revision': 1, 'notes': 'Other editor'})
                self.user.put('resume_reviews', 'r', {'id': 'r', 'revision': 1, 'notes': 'Mine'})
        self.assertEqual('Other editor', self.worker.get('resume_reviews', 'r')['notes'])

    def test_batch_collection_detects_phantom_insert(self):
        with self.assertRaises(RecordConflictError):
            with self.user.optimistic():
                self.assertEqual([], self.user.all('application_batches'))
                self.worker.put('application_batches', 'b', {'id': 'b', 'job_ids': ['j']})
                self.user.put('application_batches', 'a', {'id': 'a', 'job_ids': ['j']})
        self.assertIsNone(self.worker.get('application_batches', 'a'))

    def test_background_job_refresh_does_not_invalidate_saved_role(self):
        self.worker.put('jobs', 'j', {'id': 'j', 'last_seen': 'before'})
        with self.user.optimistic():
            self.assertEqual('before', self.user.get('jobs', 'j')['last_seen'])
            self.worker.put('jobs', 'j', {'id': 'j', 'last_seen': 'after'})
            self.user.put('applications', 'j', {'id': 'j', 'job_id': 'j', 'stage': 'saved'})
        self.assertEqual('saved', self.worker.get('applications', 'j')['stage'])

    def test_matching_generator_runs_outside_sqlite_writer_transaction(self):
        self.user.db.execute('PRAGMA busy_timeout=10')
        def matching():
            yield {'id': 'first'}
            with self.user.optimistic():
                self.user.put('applications', 'first', {'id': 'first', 'stage': 'saved'})
            yield {'id': 'second'}
        self.worker.put_many('jobs', matching())
        self.assertEqual('saved', self.worker.get('applications', 'first')['stage'])
        self.assertEqual(2, len(self.worker.all('jobs')))

    def test_reads_see_buffered_changes_and_caller_mutation_is_isolated(self):
        with self.user.optimistic():
            value = {'id': 'r', 'revision': 1, 'annotations': []}
            self.user.put('resume_reviews', 'r', value)
            value['revision'] = 99
            self.assertEqual(1, self.user.get('resume_reviews', 'r')['revision'])
            self.assertEqual(1, self.user.all('resume_reviews')[0]['revision'])
            self.assertEqual([], self.worker.all('resume_reviews'))
        self.assertEqual(1, self.worker.get('resume_reviews', 'r')['revision'])

    def test_same_entity_conflict_does_not_commit_an_unrelated_pending_record(self):
        self.worker.put('resume_reviews', 'r', {'id': 'r', 'revision': 1})
        with self.assertRaises(RecordConflictError):
            with self.user.optimistic():
                current = self.user.get('resume_reviews', 'r')
                self.worker.put('resume_reviews', 'r', {'id': 'r', 'revision': 2})
                current['revision'] = 3
                self.user.put('resume_reviews', 'r', current)
                self.user.put('application_batches', 'b', {'id': 'b'})
        self.assertIsNone(self.worker.get('application_batches', 'b'))


if __name__ == '__main__':
    unittest.main()

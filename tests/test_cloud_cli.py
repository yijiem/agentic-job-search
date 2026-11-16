import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobflow.cli import main
from jobflow.onboarding import initialize_workspace, save_onboarding
from jobflow.storage import Store


class HostedCommandTests(unittest.TestCase):
    def test_remote_cloud_serve_uses_separate_worker(self):
        with patch('jobflow.cloud.serve') as serve:
            with patch.dict(os.environ, {'JOBFLOW_BACKEND': 'supabase'}):
                main(['cloud', 'serve', '--origin', 'https://jobs.example.test'])
            self.assertFalse(serve.call_args.kwargs['worker'])
            with patch.dict(os.environ, {'JOBFLOW_BACKEND': 'local'}):
                main(['cloud', 'serve', '--origin', 'https://jobs.example.test'])
            self.assertTrue(serve.call_args.kwargs['worker'])
            with patch.dict(os.environ, {'JOBFLOW_BACKEND': 'local'}):
                main(['cloud', 'serve', '--no-worker', '--origin', 'https://jobs.example.test'])
            self.assertFalse(serve.call_args.kwargs['worker'])

    def test_rematch_preserves_job_lifecycle_and_completed_history_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize_workspace(root)
            save_onboarding(root, {'profile': {'name': 'Test', 'headline': 'Engineer',
                'skills': ['Python'], 'evidence': [{'id': 'fact', 'text': 'Built Python tools.', 'tags': ['Python']}]},
                'preferences': {'location_mode': 'any', 'allowed_levels': ['senior']}})
            store = Store(root / 'data/jobflow.sqlite3')
            job = {'id': 'one', 'title': 'Senior Software Engineer', 'company': 'Example',
                   'url': 'https://example.com/jobs/one', 'source_id': 'manual',
                   'description': 'Python', 'location': 'Remote, US', 'availability': 'missing',
                   'first_seen': '2025-01-02T12:00:00+00:00', 'last_seen': '2025-02-01T12:00:00+00:00',
                   'missing_scans': 1, 'score': 0, 'eligible': False}
            store.put('jobs', 'one', job)
            task = {'id': 'done', 'title': 'Completed practice', 'status': 'done',
                    'due_date': '2025-01-01', 'minutes': 30, 'actual_minutes': 25,
                    'completed_at': '2025-01-01T12:00:00+00:00', 'notes': 'Keep this note', 'generated': False}
            store.put('tasks', 'done', task)
            app = {'id': 'one', 'job_id': 'one', 'stage': 'applied', 'notes': 'Original application',
                   'applied_at': '2025-01-01T12:00:00+00:00'}
            store.put('applications', 'one', app)
            store.put('packets', 'one', {'id': 'one', 'job_id': 'one', 'markdown': 'Preserve this edited draft.'})
            store.close()
            # Hosted execution must suppress process-wide model credentials even
            # when the imported owner's config previously enabled research.
            settings_path = root / 'config/settings.json'
            settings = json.loads(settings_path.read_text())
            settings['research']['enabled'] = True
            settings_path.write_text(json.dumps(settings))
            with patch('jobflow.sources.urlopen', side_effect=AssertionError('No live feed calls')), \
                    patch('jobflow.cli.Researcher', side_effect=AssertionError('No shared credentials')), \
                    contextlib.redirect_stdout(io.StringIO()):
                main(['--root', str(root), '--hosted', 'rematch'])
            store = Store(root / 'data/jobflow.sqlite3')
            updated = store.get('jobs', 'one')
            for field in ('availability', 'first_seen', 'last_seen', 'missing_scans'):
                self.assertEqual(updated[field], job[field])
            self.assertTrue(updated['eligible'])
            self.assertGreater(updated['score'], 0)
            self.assertEqual(store.get('applications', 'one'), app)
            self.assertTrue(store.get('packets', 'one')['needs_profile_review'])
            self.assertEqual(store.get('packets', 'one')['markdown'], 'Preserve this edited draft.')
            completed = store.get('tasks', 'done')
            for field in ('status', 'actual_minutes', 'completed_at', 'notes'):
                self.assertEqual(completed[field], task[field])
            self.assertEqual(store.recent_runs(), [])
            store.close()
            self.assertTrue(json.loads(settings_path.read_text())['research']['enabled'])

    def test_rematch_updates_changed_matches_without_rewriting_current_jobs(self):
        from jobflow.matching import match_job
        from jobflow.workflow import load_config
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize_workspace(root)
            save_onboarding(root, {'profile': {'name': 'Test', 'headline': 'Engineer',
                'skills': ['Python'], 'evidence': [{'id': 'fact', 'text': 'Built Python tools.', 'tags': ['Python']}]},
                'preferences': {'location_mode': 'any', 'allowed_levels': ['senior']}})
            config, profile = load_config(root)
            current = {'id': 'current', 'title': 'Senior Software Engineer', 'company': 'Example',
                       'url': 'https://example.com/jobs/current', 'description': 'Python',
                       'location': 'Remote, US', 'availability': 'open',
                       'first_seen': '2025-01-02T12:00:00+00:00',
                       'last_seen': '2025-02-01T12:00:00+00:00'}
            current.update(match_job(current, profile, config))
            stale = {**current, 'id': 'stale', 'score': 0, 'eligible': False,
                     'url': 'https://example.com/jobs/stale'}
            store = Store(root / 'data/jobflow.sqlite3')
            with patch('jobflow.storage.utcnow', return_value='2025-02-01T12:00:00+00:00'):
                store.put_many('jobs', [current, stale])
            store.close()
            with patch('jobflow.sources.urlopen', side_effect=AssertionError('No live feed calls')), \
                    contextlib.redirect_stdout(io.StringIO()):
                main(['--root', str(root), '--hosted', 'rematch'])
            store = Store(root / 'data/jobflow.sqlite3')
            try:
                timestamps = {row['id']: row['updated_at'] for row in store.db.execute(
                    "SELECT id,updated_at FROM records WHERE kind='jobs'")}
                self.assertEqual(timestamps['current'], '2025-02-01T12:00:00+00:00')
                self.assertNotEqual(timestamps['stale'], timestamps['current'])
                self.assertEqual(store.get('jobs', 'current'), current)
                updated = store.get('jobs', 'stale')
                self.assertEqual(updated['score'], current['score'])
                self.assertTrue(updated['eligible'])
                self.assertEqual(updated['last_seen'], stale['last_seen'])
                self.assertEqual(store.recent_runs(), [])
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()

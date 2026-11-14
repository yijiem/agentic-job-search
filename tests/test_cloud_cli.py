import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobflow.cli import main
from jobflow.onboarding import initialize_workspace, save_onboarding
from jobflow.storage import Store


class HostedCommandTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()

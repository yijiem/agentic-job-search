import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jobflow.cli import dispatch, parser
from jobflow.storage import Store
from jobflow.tracking import export_tracking, read_records, validate_record


class CoachingCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        self.store = Store(self.root / 'state.sqlite3')
        self.config = {'preparation': {'daily_minutes': 90, 'horizon_days': 3},
                       'schedule': {'timezone': 'America/Los_Angeles'},
                       'search': {'min_score': 40}, 'sources': []}
        self.profile = {'skills': ['Go'], 'evidence': [{'text': 'Built services'}]}
        self.job = {'id': 'job-1', 'company': 'Example', 'title': 'Software Engineer'}
        self.store.put('jobs', 'job-1', self.job)
        self.stamp = '2026-11-12T20:00:00+00:00'

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def command(self, *arguments):
        args = parser().parse_args(['--root', str(self.root), *arguments])
        with contextlib.redirect_stdout(io.StringIO()), patch('jobflow.cli.utcnow', return_value=self.stamp):
            dispatch(args, self.store, self.config, self.profile)

    def test_submission_timestamp_survives_stage_changes_and_repeated_updates(self):
        self.command('application', 'job-1', '--stage', 'applied', '--notes', 'Submitted myself')
        self.command('application', 'job-1', '--stage', 'rejected')
        row = self.store.get('applications', 'job-1')
        self.assertEqual(row['applied_at'], self.stamp)
        self.assertEqual(row['notes'], 'Submitted myself')
        self.command('coach', '--date', '2026-11-12')
        self.assertEqual(self.store.get('coaching', '2026-11-12')['progress']['applications']['logged'], 1)

    def test_legacy_applied_update_does_not_invent_today_submission(self):
        self.store.put('applications', 'job-1', {'id': 'job-1', 'job_id': 'job-1', 'stage': 'applied'})
        self.command('application', 'job-1', '--stage', 'applied', '--notes', 'Follow up')
        self.assertNotIn('applied_at', self.store.get('applications', 'job-1'))

    def test_imported_historical_work_remains_undated(self):
        app = validate_record('applications', {'id': 'job-1', 'job_id': 'job-1', 'stage': 'applied'}, self.store)
        task = validate_record('tasks', {'id': 'historical', 'due_date': '2025-01-01', 'title': 'Old work',
                                        'minutes': 30, 'status': 'done'}, self.store)
        self.assertNotIn('applied_at', app)
        self.assertNotIn('completed_at', task)

    def test_task_completion_preserves_time_and_notes_on_followup_edit(self):
        self.command('task', 'study-1', '--title', 'Tool loop', '--category', 'learning',
                     '--date', '2026-11-12', '--minutes', '30', '--status', 'done',
                     '--actual-minutes', '25', '--notes', 'Fixed tool error')
        self.command('task', 'study-1', '--status', 'done', '--title', 'Updated title')
        row = self.store.get('tasks', 'study-1')
        self.assertEqual(row['completed_at'], self.stamp)
        self.assertEqual(row['actual_minutes'], 25)
        self.assertEqual(row['notes'], 'Fixed tool error')
        self.command('coach', '--date', '2026-11-12')
        self.assertEqual(self.store.get('coaching', '2026-11-12')['progress']['learning']['logged'], 25)

    def test_activity_edit_and_csv_roundtrip_preserve_session_metadata(self):
        self.command('activity', 'study-session', '--category', 'learning', '--date', '2026-11-12',
                     '--minutes', '30', '--notes', 'Read course; tested errors')
        self.command('activity', 'study-session', '--minutes', '35')
        self.assertEqual(len(self.store.all('activities')), 1)
        export_tracking(self.store, self.root / 'exports')
        row = validate_record('activities', read_records(self.root / 'exports/activities.csv')[0], self.store)
        self.assertEqual(row, self.store.get('activities', 'study-session'))
        self.assertEqual(row['notes'], 'Read course; tested errors')
        self.command('coach', '--date', '2026-11-12')
        self.assertEqual(self.store.get('coaching', '2026-11-12')['progress']['learning']['logged'], 35)

    def test_invalid_activity_batch_does_not_write_partial_progress(self):
        path = self.root / 'activities.json'
        path.write_text(json.dumps([
            {'id': 'valid', 'date': '2026-11-12', 'category': 'coding', 'minutes': 30},
            {'id': 'invalid', 'date': '2026-02-30', 'category': 'coding', 'minutes': 30}]))
        with self.assertRaises(ValueError):
            self.command('import', str(path), '--kind', 'activities')
        self.assertEqual(self.store.all('activities'), [])

    def test_invalid_actual_minutes_and_missing_activity_fields_are_actionable(self):
        base = {'id': 't', 'title': 'Coding', 'status': 'done', 'due_date': '2026-11-12', 'minutes': 30}
        for value in (-1, True, 1.5, 'nope'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_record('tasks', {**base, 'actual_minutes': value}, self.store)
        for row in ({'id': 'a'}, {'id': 'a', 'date': None},
                    {'id': 'a', 'date': '2026-11-12', 'category': 'coding', 'minutes': None}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                validate_record('activities', row, self.store)

    def test_goal_change_keeps_capacity_and_invalid_update_keeps_preferences(self):
        self.command('goals', '--applications', '10', '--learning-minutes', '30',
                     '--coding-minutes', '30', '--system-design-minutes', '30')
        saved = (self.root / 'config/settings.json').read_text()
        self.assertEqual(self.config['preparation']['daily_minutes'], 90)
        self.assertEqual(self.config['daily_goals']['applications'], 10)
        with self.assertRaises(ValueError):
            self.command('goals', '--learning-minutes', '-1')
        self.assertEqual((self.root / 'config/settings.json').read_text(), saved)

    def test_report_exports_current_scorecard_and_dated_snapshot(self):
        self.command('coach', '--date', '2026-11-12')
        self.assertTrue((self.root / 'reports/daily_coach.json').is_file())
        self.assertTrue((self.root / 'reports/coaching/2026-11-12.md').is_file())
        self.assertIn('Daily goals and coach', (self.root / 'reports/review.md').read_text())


if __name__ == '__main__':
    unittest.main()

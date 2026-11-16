import copy
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

from jobflow.agentic import perform_agentic_action
from jobflow.preparation import build_plan
from jobflow.storage import Store


class AgenticPlannerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / 'jobflow.sqlite3')
        self.addCleanup(self.store.close)
        self.config = {'preparation': {'daily_minutes': 90, 'horizon_days': 4},
                       'schedule': {'timezone': 'America/Los_Angeles'},
                       'daily_goals': {'enabled': True, 'learning_minutes': 30,
                                       'coding_minutes': 30, 'system_design_minutes': 30}}
        self.profile = {'name': 'Candidate', 'skills': ['Go', 'storage']}
        self.store.put('jobs', 'role-a', {'id': 'role-a', 'company': 'Example',
                       'title': 'Senior Storage Engineer', 'description': 'Build distributed storage systems in Go.',
                       'url': 'https://example.com/jobs/a', 'matched_skills': ['storage']})
        self.now = '2026-11-14T09:00:00-08:00'

    def action(self, operation='preview', **fields):
        return perform_agentic_action(self.store, self.config, self.profile,
                                      {'operation': operation, 'job_id': 'role-a', **fields},
                                      today=self.now)

    def test_preview_does_not_mutate_any_records(self):
        proposal = self.action()['proposal']
        self.assertGreater(proposal['new_count'], 0)
        self.assertGreater(proposal['focused_count'], 0)
        self.assertEqual(self.store.all('tasks'), [])
        self.assertEqual(self.store.all('plans'), [])
        self.assertEqual(proposal['mode'], 'local')
        self.assertIn('No live research', proposal['evidence_note'])
        self.assertTrue(any(row['kind'] == 'resume' for row in proposal['next_steps']))
        self.assertTrue(all(row['minutes'] <= 90 for row in proposal['days']))

    def test_apply_exact_review_preserves_budget_notes_and_completed_history(self):
        tasks = build_plan(self.profile, [], [], [], self.config, today=self.now)['tasks']
        tasks[0].update(status='done', notes='My completed notes', actual_minutes=18,
                        completed_at='2026-11-14T08:00:00-08:00')
        tasks[1]['notes'] = 'My own prompt and practice notes'
        before = copy.deepcopy(tasks)
        self.store.put_many('tasks', tasks)
        proposal = self.action()['proposal']
        self.action('apply', fingerprint=proposal['fingerprint'])
        saved = {row['id']: row for row in self.store.all('tasks')}
        self.assertEqual(saved[tasks[0]['id']], before[0])
        for row in before:
            self.assertEqual(saved[row['id']]['notes'], row['notes'])
            self.assertEqual(saved[row['id']]['minutes'], row['minutes'])
        focused = [row for row in saved.values() if row.get('agent_job_id')]
        self.assertTrue(focused)
        self.assertTrue(all(row['status'] == 'todo' for row in focused))
        totals = defaultdict(int)
        for row in saved.values():
            if row['status'] not in ('skipped', 'unscheduled'):
                totals[row['due_date']] += row['minutes']
        self.assertTrue(all(total <= 90 for total in totals.values()))

    def test_stale_preview_cannot_overwrite_new_task_edits(self):
        proposal = self.action()['proposal']
        task = {'id': 'user-task', 'title': 'Keep my practice', 'status': 'done',
                'due_date': '2026-11-14', 'minutes': 30, 'notes': 'Finished independently'}
        self.store.put('tasks', task['id'], task)
        with self.assertRaisesRegex(ValueError, 'workspace changed'):
            self.action('apply', fingerprint=proposal['fingerprint'])
        self.assertEqual(self.store.all('tasks'), [task])

    def test_new_interview_and_profile_change_invalidate_preview(self):
        for change in ('interview', 'profile'):
            with self.subTest(change=change):
                proposal = self.action()['proposal']
                if change == 'interview':
                    self.store.put('interviews', 'screen', {'id': 'screen', 'job_id': 'role-a',
                                   'company': 'Example', 'round': 'Coding',
                                   'starts_at': '2026-11-15T10:00:00-08:00'})
                else:
                    self.profile['skills'].append('C++')
                with self.assertRaisesRegex(ValueError, 'workspace changed'):
                    self.action('apply', fingerprint=proposal['fingerprint'])

    def test_unknown_role_cannot_read_other_workspace_evidence(self):
        with self.assertRaisesRegex(ValueError, 'role in this workspace'):
            self.action(job_id='private-other-workspace')
        self.assertEqual(self.store.all('tasks'), [])

    def test_fallback_prioritizes_confirmed_interview_and_due_followup(self):
        self.store.put('applications', 'app', {'id': 'app', 'job_id': 'role-a', 'company': 'Example',
                       'stage': 'technical', 'follow_up_date': '2026-11-13',
                       'next_action': 'Review recruiter response'})
        self.store.put('interviews', 'screen', {'id': 'screen', 'job_id': 'role-a',
                       'company': 'Example', 'round': 'Coding', 'starts_at': '2026-11-15T10:00:00-08:00'})
        proposal = self.action(job_id='')['proposal']
        self.assertEqual(proposal['selected_job_id'], 'role-a')
        self.assertEqual(proposal['job_id'], '')
        self.assertEqual(proposal['next_steps'][0]['kind'], 'interview')
        self.assertTrue(any(row['detail'] == 'Review recruiter response' for row in proposal['next_steps']))

    def test_manual_and_other_role_blocks_do_not_receive_focus(self):
        tasks = build_plan(self.profile, [], [], [], self.config, today=self.now)['tasks']
        tasks[0]['generated'] = False
        tasks[1]['job_id'] = 'another-role'
        tasks[2]['status'] = 'doing'
        self.store.put_many('tasks', tasks)
        proposal = self.action()['proposal']
        self.action('apply', fingerprint=proposal['fingerprint'])
        for original in tasks[:3]:
            self.assertNotIn('agent_guidance', self.store.get('tasks', original['id']))


if __name__ == '__main__':
    unittest.main()

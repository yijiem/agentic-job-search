import io
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from jobflow.cloud_accounts import Accounts
from jobflow.cloud_jobs import LEASE_SECONDS, WorkQueue, run_item, worker_loop
from jobflow.onboarding import initialize_workspace, save_onboarding
from jobflow.storage import Store


class WorkQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.queue = WorkQueue(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_queue_survives_restart_and_deduplicates_account_work(self):
        self.assertTrue(self.queue.enqueue('first', 'run'))
        self.assertFalse(self.queue.enqueue('first', 'due'))
        restarted = WorkQueue(self.root)
        self.assertEqual(restarted.status('first')['command'], 'run')
        first = restarted.claim()
        self.assertEqual(first['account_id'], 'first')
        self.assertEqual(first['attempts'], 1)
        self.assertFalse(self.queue.enqueue('first', 'due'))
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.queue.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('claim_token', self.queue.status('first'))
        self.assertNotIn('attempts', self.queue.status('first'))
        with self.assertRaises(ValueError):
            self.queue.enqueue('first', 'shell-command')

    def test_pending_commands_upgrade_without_losing_profile_changes(self):
        self.queue.enqueue('first', 'due')
        self.assertTrue(self.queue.enqueue('first', 'rematch'))
        self.assertEqual(self.queue.status('first')['command'], 'rematch')
        self.assertFalse(self.queue.enqueue('first', 'due'))
        self.assertTrue(self.queue.enqueue('first', 'run'))
        self.assertFalse(self.queue.enqueue('first', 'rematch'))
        self.assertEqual(self.queue.status('first')['command'], 'run')

    def test_running_profile_edit_creates_one_bounded_followup_and_due_does_not(self):
        self.queue.enqueue('first', 'rematch')
        first = self.queue.claim()
        self.assertFalse(self.queue.enqueue('first', 'due'))
        self.assertTrue(self.queue.enqueue('first', 'rematch'))
        self.assertFalse(self.queue.enqueue('first', 'rematch'))
        self.assertTrue(self.queue.enqueue('first', 'run'))
        self.assertFalse(self.queue.enqueue('first', 'rematch'))
        self.assertEqual(self.queue.status('first')['next_command'], 'run')
        self.queue.finish(first, 'done', 'First pass complete')
        followup = self.queue.claim()
        self.assertEqual(followup['command'], 'run')
        self.assertEqual(followup['attempts'], 1)
        self.queue.finish(followup, 'done', 'Complete')
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.queue.status('first')['status'], 'done')

    def test_only_one_concurrent_worker_claims_each_job(self):
        self.queue.enqueue('first')
        barrier = threading.Barrier(2)
        items = []

        def claim():
            barrier.wait()
            items.append(self.queue.claim())

        threads = [threading.Thread(target=claim) for unused in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(item is not None for item in items), 1)

    def test_retry_backoff_is_bounded_and_failed_job_can_be_requested_again(self):
        with patch('jobflow.cloud_jobs.time.time', return_value=100):
            self.queue.enqueue('first')
            first = self.queue.claim()
            self.queue.finish(first, 'retry', 'Transient failure')
        self.assertEqual(self.queue.status('first')['status'], 'pending')
        with patch('jobflow.cloud_jobs.time.time', return_value=159):
            self.assertIsNone(self.queue.claim())
        with patch('jobflow.cloud_jobs.time.time', return_value=160):
            second = self.queue.claim()
            self.assertEqual(second['attempts'], 2)
            self.queue.finish(second, 'retry', 'Transient failure again')
        with patch('jobflow.cloud_jobs.time.time', return_value=220):
            third = self.queue.claim()
            self.assertEqual(third['attempts'], 3)
            self.queue.finish(third, 'retry', 'Persistent failure')
            self.assertEqual(self.queue.status('first')['status'], 'failed')
            self.assertIsNone(self.queue.claim())
            self.assertTrue(self.queue.enqueue('first'))
            self.assertEqual(self.queue.claim()['attempts'], 1)

    def test_expired_lease_recovery_fences_late_worker_completion(self):
        with patch('jobflow.cloud_jobs.time.time', return_value=100):
            self.queue.enqueue('first')
            stale = self.queue.claim()
        with patch('jobflow.cloud_jobs.time.time', return_value=100 + LEASE_SECONDS + 1):
            current = WorkQueue(self.root).claim()
            self.assertNotEqual(stale['claim_token'], current['claim_token'])
            self.assertFalse(self.queue.finish(stale, 'done', 'Stale result'))
            self.assertEqual(self.queue.status('first')['status'], 'running')
            self.assertTrue(self.queue.finish(current, 'done', 'Current result'))
        self.assertEqual(self.queue.status('first')['message'], 'Current result')

    def test_expired_lease_merges_pending_followup_into_latest_claim(self):
        with patch('jobflow.cloud_jobs.time.time', return_value=100):
            self.queue.enqueue('first', 'due')
            stale = self.queue.claim()
            self.queue.enqueue('first', 'rematch')
        with patch('jobflow.cloud_jobs.time.time', return_value=100 + LEASE_SECONDS + 1):
            item = self.queue.claim()
            self.assertEqual(item['command'], 'rematch')
            self.assertIsNone(self.queue.status('first')['next_command'])
            self.queue.finish(item, 'done', 'Updated')
            self.assertIsNone(self.queue.claim())
            self.assertFalse(self.queue.finish(stale, 'done', 'Late'))

    def test_repeated_crash_recovery_eventually_fails_without_infinite_restarts(self):
        with patch('jobflow.cloud_jobs.time.time', return_value=100):
            self.queue.enqueue('first')
            self.queue.claim()
        for attempt in (2, 3):
            with patch('jobflow.cloud_jobs.time.time', return_value=100 + (attempt - 1) * (LEASE_SECONDS + 1)):
                self.assertEqual(self.queue.claim()['attempts'], attempt)
        with patch('jobflow.cloud_jobs.time.time', return_value=100 + 3 * (LEASE_SECONDS + 1)):
            self.assertIsNone(self.queue.claim())
        self.assertEqual(self.queue.status('first')['status'], 'failed')

    def test_existing_queue_schema_is_upgraded_without_losing_pending_item(self):
        other = self.root / 'old'
        (other / 'data').mkdir(parents=True)
        with sqlite3.connect(str(other / 'data/cloud_jobs.sqlite3')) as db:
            db.executescript('''CREATE TABLE work (
                account_id TEXT PRIMARY KEY, status TEXT NOT NULL, command TEXT NOT NULL,
                requested_at REAL NOT NULL, started_at REAL, finished_at REAL,
                attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL,
                lease_until REAL, message TEXT NOT NULL DEFAULT '');
                INSERT INTO work(account_id,status,command,requested_at,available_at)
                VALUES('existing','pending','run',1,1);''')
        queue = WorkQueue(other)
        self.assertEqual(queue.claim()['account_id'], 'existing')
        with queue.connect() as db:
            columns = {row[1] for row in db.execute('PRAGMA table_info(work)')}
        self.assertTrue({'claim_token', 'next_command'}.issubset(columns))


class CloudWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.accounts = Accounts(self.root)
        self.queue = WorkQueue(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def account(self, email='owner@example.com', complete=True, active=True):
        account = self.accounts.invite(email)
        if active:
            account = self.accounts.authenticate_identity('verified:' + email, email)
        root = self.accounts.workspace(account)
        initialize_workspace(root, sources=[])
        if complete:
            save_onboarding(root, {'profile': {'name': email, 'skills': ['Python'],
                'evidence': [{'id': 'evidence', 'text': 'Built an actual system.', 'tags': ['Python']}]}})
        return account, root

    def item(self, account, command='run'):
        self.queue.enqueue(account['id'], command)
        return self.queue.claim()

    def reports(self, root, health=None, status='completed'):
        directory = root / 'reports'
        directory.mkdir(exist_ok=True)
        (directory / 'source_health.json').write_text(json.dumps(health if health is not None else []))
        (directory / 'runs.json').write_text(json.dumps([{'id': 1, 'status': status}]))

    def test_due_worker_invokes_only_account_workspace_with_hosted_guard_and_no_global_key(self):
        account, root = self.account()
        item = self.item(account, 'due')
        result = SimpleNamespace(returncode=0, stdout='Not due; no changes.\n', stderr='')
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'never-use-global-secret'}), \
                patch('jobflow.cloud_jobs.subprocess.run', return_value=result) as run:
            run_item(self.accounts, self.queue, item)
        command = run.call_args.args[0]
        self.assertEqual(command[1:], ['-m', 'jobflow', '--root', str(root), '--hosted', 'run', '--due'])
        self.assertNotIn('OPENAI_API_KEY', run.call_args.kwargs['env'])
        self.assertNotIn('never-use-global-secret', json.dumps(self.queue.status(account['id'])))
        self.assertEqual(self.queue.status(account['id'])['status'], 'done')
        self.assertIn('saved schedule', self.queue.status(account['id'])['message'])
        self.assertFalse((root / 'reports/source_health.json').exists())

    def test_workflow_logs_command_duration_and_outcome_without_private_data(self):
        account, root = self.account('private-owner@example.com')
        self.reports(root)
        item = self.item(account, 'run')
        captured = io.StringIO()
        result = SimpleNamespace(returncode=0, stdout='private resume text', stderr='private credential')
        with redirect_stdout(captured), \
                patch('jobflow.cloud_jobs.time.monotonic', side_effect=[100, 108.25]), \
                patch('jobflow.cloud_jobs.subprocess.run', return_value=result):
            run_item(self.accounts, self.queue, item)
        self.assertEqual(captured.getvalue().splitlines(), [
            'Jobflow background workflow started: command=run',
            'Jobflow background workflow finished: command=run outcome=done elapsed_seconds=8.250',
        ])
        for private in (account['id'], account['email'], item['claim_token'], str(root),
                        'private resume text', 'private credential'):
            self.assertNotIn(private, captured.getvalue())

    def test_timeout_logs_safe_outcome_and_retains_existing_retry_behavior(self):
        account, unused = self.account()
        item = self.item(account, 'refresh')
        captured = io.StringIO()
        with redirect_stdout(captured), \
                patch('jobflow.cloud_jobs.time.monotonic', side_effect=[50, 1850]), \
                patch('jobflow.cloud_jobs.subprocess.run', side_effect=subprocess.TimeoutExpired(
                    'private command path', 1800, output='private output', stderr='secret')):
            run_item(self.accounts, self.queue, item)
        self.assertEqual(captured.getvalue().splitlines(), [
            'Jobflow background workflow started: command=refresh',
            'Jobflow background workflow finished: command=refresh outcome=timeout elapsed_seconds=1800.000',
        ])
        self.assertEqual(self.queue.status(account['id'])['status'], 'pending')

    def test_rematch_has_its_own_cli_without_network_run_or_source_health_claim(self):
        account, root = self.account()
        item = self.item(account, 'rematch')
        self.reports(root, [{'id': 'unavailable', 'status': 'error'}], 'partial')
        with patch('jobflow.cloud_jobs.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')) as run:
            run_item(self.accounts, self.queue, item)
        self.assertEqual(run.call_args.args[0][-1], 'rematch')
        self.assertNotIn('--due', run.call_args.args[0])
        status = self.queue.status(account['id'])
        self.assertEqual(status['status'], 'done')
        self.assertIn('saved profile', status['message'])
        self.assertNotIn('up to date', status['message'])

    def test_source_error_is_partial_and_never_closes_or_changes_saved_jobs(self):
        account, root = self.account()
        store = Store(root / 'data/jobflow.sqlite3')
        job = {'id': 'retained', 'availability': 'open', 'missing_scans': 0, 'notes': 'User history'}
        store.put('jobs', job['id'], job)
        store.close()
        self.reports(root, [{'id': 'source', 'status': 'error', 'message': 'Temporarily unavailable'}], 'partial')
        item = self.item(account)
        with patch('jobflow.cloud_jobs.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='{}', stderr='')):
            run_item(self.accounts, self.queue, item)
        status = self.queue.status(account['id'])
        self.assertEqual(status['status'], 'partial')
        self.assertIn('source errors', status['message'])
        store = Store(root / 'data/jobflow.sqlite3')
        self.assertEqual(store.get('jobs', job['id']), job)
        store.close()

    def test_missing_or_malformed_reports_do_not_establish_success(self):
        account, root = self.account()
        result = SimpleNamespace(returncode=0, stdout='{}', stderr='')
        for variant in ('missing', 'bad-row', 'bad-root', 'empty-runs', 'unknown-status'):
            with self.subTest(variant=variant):
                if variant != 'missing':
                    self.reports(root, [None] if variant == 'bad-row' else [])
                    if variant == 'bad-root':
                        (root / 'reports/source_health.json').write_text('{}')
                    if variant == 'empty-runs':
                        (root / 'reports/runs.json').write_text('[]')
                    if variant == 'unknown-status':
                        (root / 'reports/source_health.json').write_text('[{}]')
                item = self.item(account)
                with patch('jobflow.cloud_jobs.subprocess.run', return_value=result):
                    run_item(self.accounts, self.queue, item)
                self.assertNotEqual(self.queue.status(account['id'])['status'], 'done')

    def test_verified_completed_reports_allow_success(self):
        account, root = self.account()
        self.reports(root, [{'id': 'source', 'status': 'ok'}, {'id': 'manual', 'status': 'manual'}])
        item = self.item(account)
        with patch('jobflow.cloud_jobs.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='{}', stderr='')):
            run_item(self.accounts, self.queue, item)
        self.assertEqual(self.queue.status(account['id'])['status'], 'done')

    def test_workflow_lock_or_process_error_retries_without_exposing_subprocess_output(self):
        for result in (SimpleNamespace(returncode=0, stdout='', stderr='Another workflow process is running; try again later.'),
                       SimpleNamespace(returncode=1, stdout='private resume', stderr='credential or private path')):
            account, unused_root = self.account('user%s@example.com' % result.returncode)
            item = self.item(account)
            with patch('jobflow.cloud_jobs.subprocess.run', return_value=result):
                run_item(self.accounts, self.queue, item)
            status = self.queue.status(account['id'])
            self.assertEqual(status['status'], 'pending')
            self.assertNotIn('credential', status['message'])
            self.assertNotIn('private resume', status['message'])

    def test_unknown_disabled_or_unfinished_account_cannot_start_workflow(self):
        unfinished, unused_root = self.account(complete=False)
        disabled, unused_root = self.account('disabled@example.com')
        with sqlite3.connect(str(self.accounts.path)) as db:
            db.execute("UPDATE accounts SET status='disabled' WHERE id=?", (disabled['id'],))
        for account in (unfinished, disabled, {'id': 'missing-account'}):
            item = self.item(account)
            with patch('jobflow.cloud_jobs.subprocess.run') as run:
                run_item(self.accounts, self.queue, item)
            run.assert_not_called()
            self.assertEqual(self.queue.status(account['id'])['status'], 'failed')

    def test_corrupt_workspace_fails_its_job_without_raising(self):
        account, root = self.account()
        (root / 'config/profile.json').write_text('{invalid json')
        item = self.item(account)
        with patch('jobflow.cloud_jobs.subprocess.run') as run:
            run_item(self.accounts, self.queue, item)
        run.assert_not_called()
        self.assertEqual(self.queue.status(account['id'])['status'], 'failed')

    def test_one_corrupt_workspace_does_not_stop_other_accounts_scheduling(self):
        bad, bad_root = self.account('bad@example.com')
        good, good_root = self.account('good@example.com')
        invited, unused_root = self.account('invited@example.com', active=False)
        incomplete, unused_root = self.account('incomplete@example.com', complete=False)
        (bad_root / 'config/profile.json').write_text('{invalid json')
        with patch('jobflow.cloud_jobs.subprocess.run',
                   return_value=SimpleNamespace(returncode=0, stdout='Not due; no changes.\n', stderr='')) as run:
            worker_loop(self.accounts, self.queue, threading.Event(), once=True)
        self.assertEqual(run.call_count, 1)
        self.assertIn(str(good_root), run.call_args.args[0])
        self.assertEqual(self.queue.status(good['id'])['status'], 'done')
        self.assertEqual(self.queue.status(bad['id'])['status'], 'failed')
        self.assertIsNone(self.queue.status(invited['id']))
        self.assertIsNone(self.queue.status(incomplete['id']))

    def test_worker_honors_stop_without_running_or_enqueuing(self):
        account, unused_root = self.account()
        stop = threading.Event()
        stop.set()
        with patch('jobflow.cloud_jobs.subprocess.run') as run:
            worker_loop(self.accounts, self.queue, stop, once=True)
        run.assert_not_called()
        self.assertIsNone(self.queue.status(account['id']))


if __name__ == '__main__':
    unittest.main()

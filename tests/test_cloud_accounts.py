import fcntl
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jobflow.cloud_accounts import Accounts, SESSION_SECONDS, export_workspace, normalize_email
from jobflow.storage import Store


class CloudAccountsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.accounts = Accounts(self.root / 'cloud')

    def tearDown(self):
        self.temp.cleanup()

    def active(self, email='owner@example.com', identity='verified-owner'):
        self.accounts.invite(email)
        return self.accounts.authenticate_identity(identity, email)

    def source(self, name='source'):
        root = self.root / name
        (root / 'config').mkdir(parents=True)
        config = {'schedule': {'timezone': 'America/Los_Angeles', 'hour': 8, 'minute': 0, 'days': list(range(7))},
                  'preparation': {'daily_minutes': 90, 'horizon_days': 14},
                  'search': {'min_score': 40, 'target_tc': 500000}, 'sources': [],
                  'daily_goals': {'applications': 10, 'learning_minutes': 30},
                  'research': {'enabled': False}}
        profile = {'name': 'Existing person', 'skills': ['Python'],
                   'evidence': [{'id': 'actual-evidence', 'text': 'Built an actual system.', 'tags': ['Python']}]}
        (root / 'config/settings.json').write_text(json.dumps(config, indent=4) + '\n')
        (root / 'config/profile.json').write_text(json.dumps(profile, indent=4) + '\n')
        store = Store(root / 'data/jobflow.sqlite3')
        store.put('applications', 'job-a', {'id': 'job-a', 'stage': 'applied', 'applied_at': '2024-01-01T12:00:00-08:00',
                                           'notes': 'User-edited application history'})
        store.put('tasks', 'task-a', {'id': 'task-a', 'status': 'done', 'completed_at': '2024-01-02T12:00:00-08:00',
                                    'actual_minutes': 37, 'notes': 'Preserve completion history'})
        store.put('contacts', 'contact-a', {'id': 'contact-a', 'relationship': 'mutual', 'via': 'Actual introducer'})
        run_id = store.start_run()
        store.finish_run(run_id, 'partial', {'source_errors': ['Example source unavailable']})
        store.close()
        (root / 'inputs/tracker').mkdir(parents=True)
        (root / 'inputs/tracker/tasks.csv').write_text('id,status\ntask-a,done\n')
        (root / 'reports').mkdir()
        (root / 'reports/review.md').write_text('Existing review')
        return root

    def test_invites_normalize_email_and_do_not_reset_identity(self):
        first = self.accounts.invite('Owner+Jobs@Example.COM ')
        self.assertEqual(first['email'], 'owner+jobs@example.com')
        self.assertEqual(first['status'], 'invited')
        self.assertIsNone(self.accounts.lookup_email('unknown@example.com'))
        self.assertEqual(first, self.accounts.invite('owner+jobs@example.com'))
        active = self.accounts.authenticate_identity('verified-id', first['email'])
        self.assertEqual(active['status'], 'active')
        self.assertEqual(self.accounts.invite(first['email']), active)

    def test_email_validation_and_uninvited_identity(self):
        for value in ('', 'no-email', 'a@b', 'a@example.com\nInjected: value', '../owner@example.com', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_email(value)
        with self.assertRaises(ValueError):
            self.accounts.authenticate_identity('valid-provider-id', 'unknown@example.com')
        self.assertEqual(self.accounts.list_accounts(), [])

    def test_identity_cannot_replace_owner_or_claim_second_account(self):
        owner = self.active()
        self.accounts.invite('other@example.com')
        with self.assertRaises(ValueError):
            self.accounts.authenticate_identity('different-provider-id', owner['email'])
        with self.assertRaises(ValueError):
            self.accounts.authenticate_identity('verified-owner', 'other@example.com')
        self.assertEqual(self.accounts.authenticate_identity('verified-owner', owner['email']), owner)

    def test_concurrent_binding_allows_only_one_owner(self):
        self.accounts.invite('owner@example.com')
        outcomes = []
        barrier = threading.Barrier(2)

        def bind(identity):
            barrier.wait()
            try:
                self.accounts.authenticate_identity(identity, 'owner@example.com')
                outcomes.append('success')
            except ValueError:
                outcomes.append('denied')

        threads = [threading.Thread(target=bind, args=(str(index),)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(outcomes, ['success', 'denied'])

    def test_workspace_is_derived_from_registry_and_never_from_payload(self):
        first, second = self.active(), self.active('second@example.com', 'second-id')
        first_path, second_path = self.accounts.workspace(first), self.accounts.workspace(second)
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(self.accounts.workspace(dict(first, workspace_id='../../outside')), first_path)
        with self.assertRaises(ValueError):
            self.accounts.workspace({'id': '../../outside', 'workspace_id': first['workspace_id']})
        self.assertEqual(first_path.parent, self.accounts.workspaces)
        self.assertFalse(first_path.exists())

    def test_sessions_are_hashed_isolated_and_revocable(self):
        first, second = self.active(), self.active('second@example.com', 'second-id')
        token, csrf = self.accounts.create_session(first)
        second_token, second_csrf = self.accounts.create_session(second)
        self.assertNotEqual(csrf, second_csrf)
        self.assertEqual(self.accounts.session(token), {'account': first, 'csrf_token': csrf})
        self.assertEqual(self.accounts.session(second_token)['account']['id'], second['id'])
        with sqlite3.connect(str(self.accounts.path)) as db:
            values = db.execute('SELECT token_hash FROM sessions').fetchall()
        self.assertNotIn((token,), values)
        self.assertIn((hashlib.sha256(token.encode()).hexdigest(),), values)
        self.accounts.revoke_session(token)
        self.assertIsNone(self.accounts.session(token))
        self.assertIsNotNone(self.accounts.session(second_token))
        for token in ('', None, 'short', 'x' * 43):
            self.assertIsNone(self.accounts.session(token))

    def test_session_hard_expiry_and_invited_account_rejected(self):
        invited = self.accounts.invite('invited@example.com')
        with self.assertRaises(ValueError):
            self.accounts.create_session(invited)
        active = self.active()
        with patch('jobflow.cloud_accounts.time.time', return_value=1000):
            token, unused = self.accounts.create_session(active)
        with patch('jobflow.cloud_accounts.time.time', return_value=1000 + SESSION_SECONDS - 1):
            self.assertIsNotNone(self.accounts.session(token))
        with patch('jobflow.cloud_accounts.time.time', return_value=1000 + SESSION_SECONDS):
            self.assertIsNone(self.accounts.session(token))

    def test_disabled_account_cannot_authenticate_or_reuse_session(self):
        active = self.active()
        token, unused = self.accounts.create_session(active)
        with sqlite3.connect(str(self.accounts.path)) as db:
            db.execute("UPDATE accounts SET status='disabled' WHERE id=?", (active['id'],))
        self.assertIsNone(self.accounts.session(token))
        with self.assertRaises(ValueError):
            self.accounts.authenticate_identity('verified-owner', active['email'])

    def test_private_permissions_and_symbolic_link_boundaries(self):
        self.assertEqual(self.accounts.path.stat().st_mode & 0o777, 0o600)
        for directory in (self.accounts.root, self.accounts.data, self.accounts.workspaces):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        account = self.accounts.invite('owner@example.com')
        path = self.accounts.workspace(account)
        path.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.accounts.workspace(account)
        other = self.root / 'linked-cloud'
        other.symlink_to(self.accounts.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            Accounts(other)

    def test_adoption_preserves_every_record_and_run_timestamp(self):
        source = self.source()
        account = self.accounts.invite('owner@example.com')
        original = sqlite3.connect(str(source / 'data/jobflow.sqlite3'))
        original_records = original.execute('SELECT * FROM records ORDER BY kind,id').fetchall()
        original_runs = original.execute('SELECT * FROM runs ORDER BY id').fetchall()
        original.close()
        summary = self.accounts.adopt(account['email'], source)
        destination = self.accounts.workspace(account)
        self.assertTrue(summary['adopted'])
        self.assertEqual(summary['counts'], {'applications': 1, 'contacts': 1, 'tasks': 1, 'runs': 1})
        copied = sqlite3.connect(str(destination / 'data/jobflow.sqlite3'))
        self.assertEqual(copied.execute('SELECT * FROM records ORDER BY kind,id').fetchall(), original_records)
        self.assertEqual(copied.execute('SELECT * FROM runs ORDER BY id').fetchall(), original_runs)
        copied.close()
        for name in ('profile.json', 'settings.json'):
            self.assertEqual((source / 'config' / name).read_bytes(), (destination / 'config' / name).read_bytes())
        self.assertEqual((destination / 'inputs/tracker/tasks.csv').read_bytes(),
                         (source / 'inputs/tracker/tasks.csv').read_bytes())
        self.assertTrue(json.loads((destination / 'config/onboarding.json').read_text())['completed'])
        self.assertEqual(json.loads((destination / 'config/hosted.json').read_text()), {'version': 1})
        self.assertTrue(self.accounts.lookup_email(account['email'])['onboarding_complete'])
        self.assertFalse((source / 'config/onboarding.json').exists())

    def test_adoption_is_idempotent_and_never_overwrites_later_edits(self):
        source = self.source()
        account = self.accounts.invite('owner@example.com')
        self.accounts.adopt(account['email'], source)
        destination = self.accounts.workspace(account)
        store = Store(destination / 'data/jobflow.sqlite3')
        store.put('tasks', 'later', {'id': 'later', 'status': 'done'})
        store.close()
        summary = self.accounts.adopt(account['email'], source)
        self.assertTrue(summary['already_adopted'])
        store = Store(destination / 'data/jobflow.sqlite3')
        self.assertEqual(store.get('tasks', 'later')['status'], 'done')
        store.close()
        with self.assertRaises(ValueError):
            self.accounts.adopt(account['email'], self.source('different-source'))

    def test_adoption_refuses_populated_and_uninvited_destinations(self):
        source = self.source()
        with self.assertRaises(ValueError):
            self.accounts.adopt('unknown@example.com', source)
        account = self.accounts.invite('owner@example.com')
        destination = self.accounts.workspace(account)
        destination.mkdir()
        (destination / 'keep.txt').write_text('User data')
        with self.assertRaises(ValueError):
            self.accounts.adopt(account['email'], source)
        self.assertEqual((destination / 'keep.txt').read_text(), 'User data')

    def test_adoption_excludes_credentials_unknown_files_and_links(self):
        source = self.source()
        (source / 'inputs/credentials.json').write_text('{"secret": "do not copy"}')
        (source / 'inputs/provider.json').write_text('{"refresh_token": "do not copy"}')
        (source / 'inputs/.env').write_text('API_KEY=do-not-copy')
        (source / 'inputs/run.sh').write_text('do not copy')
        (source / 'inputs/linked.txt').symlink_to(source / 'config/profile.json')
        self.accounts.invite('owner@example.com')
        summary = self.accounts.adopt('owner@example.com', source)
        destination = self.accounts.workspace(summary['account'])
        for name in ('credentials.json', 'provider.json', '.env', 'run.sh', 'linked.txt'):
            self.assertFalse((destination / 'inputs' / name).exists())
        self.assertEqual(len(summary['skipped_files']), 5)
        self.assertFalse((destination / 'data/accounts.sqlite3').exists())

    def test_export_preserves_resume_sources_and_previews_as_exact_bytes(self):
        source = self.source()
        resume_directory = source / 'inputs/resume-sources'
        resume_directory.mkdir()
        files = {
            'resume.tex': b'\\documentclass{resume}\nExact resume source.\n',
            'resume.cls': b'\\ProvidesClass{resume}\n',
            'format.sty': b'\\ProvidesPackage{format}\n',
            'papers.bib': b'@article{actual, title={Actual publication}}\n',
            'review.diff': b'--- resume.tex\n+++ resume.tex\n',
            'change.patch': b'@@ -1 +1 @@\n-old\n+new\n',
            'preview.png': b'\x89PNG\r\n\x1a\n\x00\xffpreview',
            'preview.jpg': b'\xff\xd8\xff\x00preview',
            'preview.jpeg': b'\xff\xd8\xff\x00another-preview',
            'preview.webp': b'RIFF\x00\x00\x00\x00WEBPpreview',
        }
        for name, content in files.items():
            (resume_directory / name).write_bytes(content)
        for name in ('build.sh', 'compile.py', 'executable.bin'):
            (resume_directory / name).write_bytes(b'excluded executable content')
        destination = self.root / 'resume-export'
        summary = export_workspace(source, destination)
        exported = destination / 'inputs/resume-sources'
        for name, content in files.items():
            with self.subTest(name=name):
                self.assertEqual((exported / name).read_bytes(), content)
                self.assertEqual((exported / name).stat().st_mode & 0o777, 0o600)
        for name in ('build.sh', 'compile.py', 'executable.bin'):
            self.assertFalse((exported / name).exists())
            self.assertTrue(any(name in entry for entry in summary['skipped_files']))
        self.assertEqual(len(summary['skipped_files']), 3)

    def test_source_workflow_lock_prevents_partial_export(self):
        source = self.source()
        self.accounts.invite('owner@example.com')
        with (source / 'data/workflow.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'source workflow is running'):
                self.accounts.adopt('owner@example.com', source)
        self.assertFalse(self.accounts.workspace(self.accounts.lookup_email('owner@example.com')).exists())
        self.assertEqual(list(self.accounts.workspaces.iterdir()), [])

    def test_export_includes_committed_wal_state_without_assigning_identity(self):
        source = self.source()
        store = Store(source / 'data/jobflow.sqlite3')
        store.db.execute('PRAGMA wal_autocheckpoint=0')
        store.put('research', 'latest', {'id': 'latest', 'text': 'Committed in WAL'})
        self.assertTrue((source / 'data/jobflow.sqlite3-wal').exists())
        destination = self.root / 'portable-owner'
        summary = export_workspace(source, destination)
        copied = Store(destination / 'data/jobflow.sqlite3')
        self.assertEqual(copied.get('research', 'latest')['text'], 'Committed in WAL')
        copied.close()
        store.close()
        self.assertEqual(summary['counts']['research'], 1)
        self.assertFalse((destination / 'config/hosted.json').exists())
        self.assertFalse((destination / 'data/accounts.sqlite3').exists())
        self.assertEqual(self.accounts.list_accounts(), [])
        with self.assertRaises(ValueError):
            export_workspace(source, destination)

    def test_export_rejects_recursive_destinations_and_invalid_profiles(self):
        source = self.source()
        for destination in (source / 'inputs/export', source / 'reports/export'):
            with self.assertRaises(ValueError):
                export_workspace(source, destination)
        profile = source / 'config/profile.json'
        profile.write_text('{"evidence": []}')
        with self.assertRaises(ValueError):
            export_workspace(source, self.root / 'export')
        self.assertFalse((self.root / 'export').exists())

    def test_export_cleans_failed_staging_and_rejects_config_credentials(self):
        source = self.source()
        config_path = source / 'config/settings.json'
        config = json.loads(config_path.read_text())
        config['api_key'] = 'do-not-copy'
        config_path.write_text(json.dumps(config))
        destination = self.root / 'portable'
        with self.assertRaisesRegex(ValueError, 'credential fields'):
            export_workspace(source, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob('.owner-export-*')), [])

    def test_mark_complete_requires_valid_actual_workspace(self):
        account = self.accounts.invite('owner@example.com')
        with self.assertRaises((ValueError, OSError)):
            self.accounts.mark_onboarding_complete(account)
        self.assertFalse(self.accounts.lookup_email(account['email'])['onboarding_complete'])


if __name__ == '__main__':
    unittest.main()

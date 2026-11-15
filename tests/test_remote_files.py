import copy
import gzip
import hashlib
import json
import os
import tempfile
import unittest
import uuid
import sqlite3
import time
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch, Mock

from jobflow import remote_files


class FileBackend:
    def __init__(self):
        self.rows = {}
        self.objects = {}
        self.events = []
        self.fail_manifest = False

    def rpc(self, name, args):
        workspace = args['p_workspace']
        if name == 'jobflow_file_get':
            return copy.deepcopy(self.rows.get((workspace, args['p_path'])))
        if name == 'jobflow_file_list':
            return [copy.deepcopy(row) for (owner, path), row in sorted(self.rows.items())
                    if owner == workspace and path.startswith(args['p_prefix']) and path > args['p_after']][:args['p_limit']]
        if name == 'jobflow_file_put':
            self.events.append('manifest')
            if self.fail_manifest:
                raise RuntimeError('Fenced manifest write failed')
            if args.get('p_token') != 'lease':
                raise RuntimeError('No lease')
            row = copy.deepcopy(args['p_file'])
            self.rows[workspace, row['path']] = row
            return copy.deepcopy(row)
        if name == 'jobflow_workspace_create':
            return True
        if name == 'jobflow_file_stage':
            return True
        raise AssertionError(name)

    def storage_upload(self, bucket, key, content, content_type=None):
        self.events.append('upload')
        self.objects[bucket, key] = content

    def storage_download(self, bucket, key):
        self.events.append('download')
        return self.objects[bucket, key]


class RemoteFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'workspace'
        (self.root / 'config').mkdir(parents=True)
        self.workspace = str(uuid.uuid4())
        (self.root / 'config/remote.json').write_text(json.dumps({'workspace_id': self.workspace}))
        self.backend = FileBackend()
        self.patches = [patch.dict(os.environ, {'JOBFLOW_BACKEND': 'supabase'}),
                        patch('jobflow.remote.client_from_env', return_value=self.backend),
                        patch('jobflow.remote.token_for', return_value='lease')]
        for value in self.patches:
            value.start()

    def tearDown(self):
        for value in reversed(self.patches):
            value.stop()
        self.temp.cleanup()

    def write(self, relative, body):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def test_restart_download_preserves_bytes_and_private_permissions(self):
        body = b'owner resume\n' * 1000
        path = self.write('inputs/resumes/original.txt', body)
        row = remote_files.persist_file(self.root, path)
        self.assertEqual(self.backend.events[:2], ['upload', 'manifest'])
        self.assertEqual(row['encoding'], 'gzip')
        self.assertLess(row['stored_size'], row['size'])
        path.unlink()
        self.assertEqual(remote_files.ensure_file(self.root, 'inputs/resumes/original.txt').read_bytes(), body)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        before = len(self.backend.events)
        remote_files.ensure_file(self.root, 'inputs/resumes/original.txt')
        self.assertEqual(len(self.backend.events), before)

    def test_failed_manifest_does_not_replace_authoritative_version(self):
        path = self.write('config/profile.json', b'{"name":"old"}')
        old = remote_files.persist_file(self.root, path)
        path.write_bytes(b'{"name":"new"}')
        self.backend.fail_manifest = True
        with self.assertRaises(RuntimeError):
            remote_files.persist_file(self.root, path)
        self.assertEqual(self.backend.rows[self.workspace, 'config/profile.json'], old)
        self.assertEqual(remote_files.ensure_file(self.root, 'config/profile.json').read_bytes(), b'{"name":"old"}')

    def test_no_lease_prevents_upload(self):
        path = self.write('inputs/a.txt', b'private')
        with patch('jobflow.remote.token_for', return_value=None), self.assertRaises(ValueError):
            remote_files.persist_file(self.root, path)
        self.assertEqual(self.backend.events, [])

    def test_cross_workspace_object_and_corrupt_content_are_rejected(self):
        path = self.write('inputs/a.txt', b'private')
        row = remote_files.persist_file(self.root, path)
        path.unlink()
        row['object_key'] = str(uuid.uuid4()) + '/' + row['sha256']
        with self.assertRaises(ValueError):
            remote_files.ensure_file(self.root, 'inputs/a.txt', row)
        row = self.backend.rows[self.workspace, 'inputs/a.txt']
        self.backend.objects[remote_files.BUCKET, row['object_key']] = b'tampered'
        with self.assertRaises(ValueError):
            remote_files.ensure_file(self.root, 'inputs/a.txt')
        self.assertFalse(path.exists())

    def test_path_traversal_symlink_and_runtime_files_are_rejected(self):
        for relative in ('../profile.json', '/tmp/a', 'inputs/../a', 'inputs//a',
                         'config/remote.json', 'data/jobflow.sqlite3', 'inputs/web/request.json'):
            with self.subTest(path=relative), self.assertRaises(ValueError):
                remote_files.ensure_file(self.root, relative)
        external = Path(self.temp.name) / 'external'
        external.mkdir()
        (self.root / 'inputs').symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            remote_files.ensure_file(self.root, 'inputs/secret.txt')

    def test_hydration_fetches_metadata_without_resume_bodies(self):
        metadata_name = 'inputs/resumes/' + 'a' * 32 + '.json'
        for relative, body in [('config/profile.json', b'{}'),
                               (metadata_name, b'{"id":"a"}'),
                               ('inputs/resumes/original.pdf', b'%PDF-private')]:
            path = self.write(relative, body)
            remote_files.persist_file(self.root, path)
            path.unlink()
        self.backend.events.clear()
        remote_files.hydrate_workspace(self.root)
        self.assertTrue((self.root / metadata_name).exists())
        self.assertFalse((self.root / 'inputs/resumes/original.pdf').exists())
        self.assertEqual(self.backend.events.count('download'), 2)

    def test_local_workspaces_remain_local(self):
        with patch.dict(os.environ, {'JOBFLOW_BACKEND': ''}):
            path = self.write('inputs/local.txt', b'local')
            self.assertIsNone(remote_files.persist_file(self.root, path))
            self.assertEqual(remote_files.ensure_file(self.root, 'inputs/local.txt').read_bytes(), b'local')
            self.assertEqual(self.backend.events, [])

    def test_missing_manifest_does_not_accept_failed_local_write(self):
        path = self.write('config/profile.json', b'not committed')
        remote_files.ensure_file(self.root, 'config/profile.json')
        self.assertFalse(path.exists())

    def test_gc_finishes_only_after_storage_delete_succeeds(self):
        key = self.workspace + '/' + 'a' * 64
        backend = Mock()
        backend.rpc.side_effect = [[key], None, []]
        with patch('jobflow.remote.client_from_env', return_value=backend):
            self.assertEqual(remote_files.prune_unreferenced(self.root), 1)
        backend.request.assert_called_once_with('DELETE', '/storage/v1/object/jobflow-private', payload={'prefixes': [key]})
        self.assertEqual(backend.rpc.call_args_list[1].args[0], 'jobflow_file_gc_finish')
        backend.reset_mock()
        backend.rpc.side_effect = [[key]]
        backend.request.side_effect = RuntimeError('Storage unavailable')
        with patch('jobflow.remote.client_from_env', return_value=backend), self.assertRaises(RuntimeError):
            remote_files.prune_unreferenced(self.root)
        self.assertEqual(backend.rpc.call_count, 1)

    def test_gc_cannot_delete_another_workspaces_object(self):
        backend = Mock()
        backend.rpc.return_value = [str(uuid.uuid4()) + '/' + 'a' * 64]
        with patch('jobflow.remote.client_from_env', return_value=backend), self.assertRaises(ValueError):
            remote_files.prune_unreferenced(self.root)
        backend.request.assert_not_called()

    def test_import_failure_is_reported_without_submitting_every_file(self):
        from jobflow.remote_migration import _upload_files
        progress, called = [], []
        def upload(snapshot, root, relative, expected):
            called.append(relative)
            if relative == 'inputs/0.txt':
                raise ValueError('Invalid private content')
            time.sleep(0.02)
            return 1
        files = {'inputs/%s.txt' % i: {} for i in range(100)}
        with patch('jobflow.remote_migration._upload_file_retry', side_effect=upload), self.assertRaises(ValueError):
            _upload_files(self.root, self.root, files, workers=2, progress=progress.append)
        self.assertLessEqual(len(called), 2)
        self.assertEqual(progress[0]['status'], 'failed')
        self.assertEqual(progress[0]['path'], 'inputs/0.txt')

    def test_import_retries_only_transient_idempotent_transfers(self):
        from jobflow.remote import RemoteError
        from jobflow.remote_migration import _upload_file_retry
        with patch('jobflow.remote_migration._upload_file', side_effect=[RemoteError('Unavailable', 503), 42]) as upload, \
             patch('jobflow.remote_migration.time.sleep'):
            self.assertEqual(_upload_file_retry(self.root, self.root, 'inputs/a.txt', {}), 42)
            self.assertEqual(upload.call_count, 2)
        for error in (RemoteError('Denied', 401), ValueError('Integrity failed')):
            with patch('jobflow.remote_migration._upload_file', side_effect=error) as upload, \
                 self.assertRaises(type(error)):
                _upload_file_retry(self.root, self.root, 'inputs/a.txt', {})
            self.assertEqual(upload.call_count, 1)
        exhausted = RemoteError('Unavailable', 503)
        exhausted.retry_exhausted = True
        with patch('jobflow.remote_migration._upload_file', side_effect=exhausted) as upload, \
             self.assertRaises(RemoteError):
            _upload_file_retry(self.root, self.root, 'inputs/a.txt', {})
        self.assertEqual(upload.call_count, 1)

    def test_failed_adoption_reuses_frozen_snapshot_after_source_edit(self):
        from jobflow.remote_migration import adopt_owner
        source = Path(self.temp.name) / 'source'
        source.mkdir()
        (source / 'evidence.txt').write_bytes(b'original history')
        account = {'id': str(uuid.uuid4()), 'status': 'invited'}
        accounts = Mock()
        accounts.data = self.root / 'data'
        accounts.lookup_email.return_value = account
        def export(given, destination):
            destination.mkdir(parents=True)
            (destination / 'evidence.txt').write_bytes((given / 'evidence.txt').read_bytes())
            return {'counts': {}, 'skipped_files': []}
        captured = []
        def failed(*args):
            captured.append((args[2] / 'evidence.txt').read_bytes())
            raise RuntimeError('Transient upload failure')
        with patch('jobflow.cloud_accounts.export_workspace', side_effect=export) as snapshot, \
             patch('jobflow.remote_migration._adopt_snapshot', side_effect=failed):
            with self.assertRaises(RuntimeError):
                adopt_owner(accounts, 'owner@example.com', source)
            (source / 'evidence.txt').write_bytes(b'new local edits')
            with self.assertRaises(RuntimeError):
                adopt_owner(accounts, 'owner@example.com', source)
            self.assertEqual(snapshot.call_count, 1)
        self.assertEqual(captured, [b'original history', b'original history'])
        self.assertEqual((source / 'evidence.txt').read_bytes(), b'new local edits')

    def test_owner_adoption_preserves_records_runs_files_and_retries(self):
        from jobflow.remote_migration import _adopt_snapshot
        source = Path(self.temp.name) / 'source'
        (source / 'config').mkdir(parents=True)
        (source / 'data').mkdir()
        (source / 'inputs/application-resumes').mkdir(parents=True)
        profile = b'{"name": "Existing owner", "evidence": [{"text": "Real work"}]}\n'
        (source / 'config/profile.json').write_bytes(profile)
        (source / 'config/settings.json').write_bytes(b'{"schedule":{"hour":8}}\n')
        resume = b'%PDF-saved-original\n%%EOF'
        (source / 'inputs/application-resumes/resume-original').write_bytes(resume)
        db = sqlite3.connect(source / 'data/jobflow.sqlite3')
        db.executescript('CREATE TABLE records(kind,id,body,created_at,updated_at);'
                         'CREATE TABLE runs(id,started_at,finished_at,status,details);')
        body = json.dumps({'id': 'done-task', 'status': 'done', 'actual_minutes': 37,
                           'notes': 'User edited history', 'completed_at': '2024-01-02T12:00:00-08:00'})
        record = ('tasks', 'done-task', body, '2024-01-01T00:00:00Z', '2024-01-02T20:00:00Z')
        run = (7, '2024-01-03T00:00:00Z', '2024-01-03T01:00:00Z', 'partial', '{"error":"source"}')
        db.execute('INSERT INTO records VALUES(?,?,?,?,?)', record)
        db.execute('INSERT INTO runs VALUES(?,?,?,?,?)', run)
        db.commit()
        db.close()
        originals = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob('*') if path.is_file()}

        class ImportedStore:
            rows, runs, checks = [], [], 0
            def __init__(self, path):
                pass
            def import_records(self, rows):
                type(self).rows = copy.deepcopy(rows)
            def import_runs(self, rows):
                type(self).runs = copy.deepcopy(rows)
            def verify_import(self, records=(), runs=()):
                type(self).checks += 1
                return {'records_mismatched': int(bool(records) and list(records) != self.rows),
                        'runs_mismatched': int(bool(runs) and list(runs) != self.runs)}
            def stats(self):
                return {'records': {'tasks': len(self.rows)}, 'runs': len(self.runs)}
            def close(self):
                pass

        root, workspace = self.root, self.workspace
        account = {'id': 'owner', 'email': 'owner@example.com', 'workspace_id': workspace, 'status': 'invited'}
        class ImportAccounts:
            fingerprint = None
            complete = False
            def workspace(self, account):
                return root
            def begin_adoption(self, account, fingerprint):
                if self.fingerprint and self.fingerprint != fingerprint:
                    raise ValueError('Different source')
                self.fingerprint = fingerprint
                return not self.complete
            def _record_adoption(self, account, receipt):
                self.complete = True
                self.receipt = receipt
            def lookup_email(self, email):
                return account
        accounts = ImportAccounts()
        summary = {'counts': {'tasks': 1, 'runs': 1}, 'skipped_files': []}
        with patch('jobflow.remote.RemoteLease', side_effect=lambda root: nullcontext()), \
             patch('jobflow.remote_store.RemoteStore', ImportedStore):
            result = _adopt_snapshot(accounts, account, source, summary)
            self.assertTrue(result['adopted'])
            self.assertEqual(ImportedStore.checks, 2)
            self.assertEqual(ImportedStore.rows[0]['body'], body)
            self.assertEqual(ImportedStore.rows[0]['created_at'], record[3])
            self.assertEqual(ImportedStore.runs[0]['id'], 7)
            self.assertEqual(ImportedStore.runs[0]['details'], run[4])
            self.assertEqual((root / 'config/profile.json').read_bytes(), profile)
            self.assertEqual(remote_files.ensure_file(root, 'inputs/application-resumes/resume-original').read_bytes(), resume)
            retry = _adopt_snapshot(accounts, account, source, summary)
            self.assertTrue(retry['already_adopted'])
            self.assertEqual(ImportedStore.checks, 2)
            (source / 'config/profile.json').write_bytes(b'{"name":"different"}')
            with self.assertRaises(ValueError):
                _adopt_snapshot(accounts, account, source, summary)
            (source / 'config/profile.json').write_bytes(profile)
        self.assertEqual({str(path.relative_to(source)): path.read_bytes() for path in source.rglob('*') if path.is_file()}, originals)

import gzip
import io
import json
import os
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from jobflow import remote
from jobflow.remote_store import RemoteStore, read_records
from jobflow.storage import Store


class Reply(io.BytesIO):
    def __init__(self, value, compressed=False):
        content = json.dumps(value).encode()
        super().__init__(gzip.compress(content) if compressed else content)
        self.headers = {'Content-Encoding': 'gzip'} if compressed else {}


class Opener:
    def __init__(self, reply):
        self.reply = reply
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class MemoryRPC:
    def __init__(self):
        self.rows = {}
        self.calls = []
        self.token = None
        self.lease_available = True
        self.revision = 0
        self.runs = []
        self.conflict_after_first_page = False

    def rpc(self, name, params):
        self.calls.append((name, params))
        if name == 'jobflow_workspace_lease':
            action = params['p_action']
            if action == 'acquire':
                if not self.lease_available:
                    return False
                self.token = params['p_token']
                return True
            if action == 'renew':
                return self.token == params['p_token']
            if action == 'release':
                if self.token != params['p_token']:
                    return False
                self.token = None
                return True
        if name == 'jobflow_record_get':
            value = self.rows.get((params['p_kind'], params['p_id']))
            return value['body'] if value else None
        if name == 'jobflow_records_put':
            if not self.token or self.token != params['p_token']:
                raise remote.RemoteBusyError('expired')
            for row in params['p_rows']:
                self.rows[row['kind'], row['id']] = dict(row)
            self.revision += 1
            return len(params['p_rows'])
        if name == 'jobflow_records_page':
            if self.conflict_after_first_page and params['p_after_id']:
                return {'conflict': True}
            rows = sorted(self.rows.values(), key=lambda r: (r['kind'], r['created_at'], r['id']))
            rows = [r for r in rows if (not params['p_kind'] or r['kind'] == params['p_kind'])
                    and (r['kind'], r['created_at'], r['id']) >
                    (params['p_after_kind'], params['p_after_created'], params['p_after_id'])]
            if params['p_mode'] in ('projected', 'matching'):
                rows = [{**r, 'body': {k: v for k, v in r['body'].items() if k != 'description'}} for r in rows]
            return {'records': rows[:params['p_limit']], 'revision': self.revision,
                    'count': sum(r['kind'] == 'jobs' for r in self.rows.values()),
                    'runs': self.runs, 'conflict': False}
        if name == 'jobflow_import_verify':
            return {'records_mismatched': sum(self.rows.get((r['kind'], r['id'])) != r
                                             for r in params.get('p_records', [])),
                    'runs_mismatched': 0}
        if name == 'jobflow_runs_import':
            self.runs.extend(params['p_rows'])
            return len(params['p_rows'])
        if name == 'jobflow_workspace_stats':
            return {'records': {'jobs': len(self.rows)}, 'runs': 0, 'revision': self.revision}
        raise AssertionError(name)


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / 'config').mkdir()
        self.workspace = str(uuid.uuid4())
        (self.root / 'config/remote.json').write_text(json.dumps({'workspace_id': self.workspace}))
        self.env = patch.dict(os.environ, {'JOBFLOW_BACKEND': 'supabase',
            'SUPABASE_URL': 'https://project.supabase.co', 'SUPABASE_SECRET_KEY': 'sb_secret_test'}, clear=True)
        self.env.start()
        self.client = MemoryRPC()

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def row(self, identifier, description='full description'):
        return {'kind': 'jobs', 'id': identifier,
                'body': {'id': identifier, 'description': description, 'eligible': True, 'score': 50},
                'created_at': '2026-01-02T03:04:05+00:00', 'updated_at': '2026-02-03T04:05:06+00:00'}

    def test_secret_transport_is_gzipped_and_never_follows_redirects(self):
        opener = Opener(Reply({'ok': True}, compressed=True))
        client = remote.SupabaseClient('https://project.supabase.co', 'sb_secret_test', opener=opener)
        self.assertEqual({'ok': True}, client.rpc('jobflow_test', {'private': 'data'}))
        request = opener.requests[0]
        self.assertEqual('gzip', request.get_header('Accept-encoding'))
        self.assertEqual('sb_secret_test', request.get_header('Apikey'))
        self.assertGreater(client.received_bytes, 0)
        self.assertNotIn('sb_secret_test', request.full_url)
        with self.assertRaises(remote.RemoteError):
            remote._NoRedirect().redirect_request(request, None, 302, '', {}, 'https://other.example')

    def test_transport_errors_do_not_echo_key_or_response(self):
        error = HTTPError('https://project.supabase.co', 403, 'secret failure', {},
                          io.BytesIO(b'{"message":"sb_secret_test private profile", "code":"42501"}'))
        client = remote.SupabaseClient('https://project.supabase.co', 'sb_secret_test', opener=Opener(error))
        with self.assertRaises(remote.RemoteError) as result:
            client.rpc('jobflow_test', {})
        self.assertNotIn('sb_secret_test', str(result.exception))
        self.assertNotIn('private profile', str(result.exception))
        self.assertEqual('42501', result.exception.code)

    def test_transport_rejects_public_keys_and_untrusted_paths(self):
        with self.assertRaises(remote.RemoteError):
            remote.SupabaseClient('http://project.supabase.co', 'sb_secret_test')
        with self.assertRaises(remote.RemoteError):
            remote.SupabaseClient('https://project.supabase.co', 'sb_publishable_test')
        client = remote.SupabaseClient('https://project.supabase.co', 'sb_secret_test')
        with self.assertRaises(remote.RemoteError):
            client.request('GET', '//other.example/data')
        with self.assertRaises(remote.RemoteError):
            client.request('GET', '/rest/v1/', headers={'Authorization': 'different'})
        with self.assertRaises(remote.RemoteError):
            client.storage_download('private', '../other/file')

    def test_marker_is_required_and_canonical(self):
        self.assertEqual(self.workspace, remote.workspace_id(self.root))
        (self.root / 'config/remote.json').write_text('{"workspace_id":"../someone"}')
        with self.assertRaises(remote.RemoteError):
            remote.workspace_id(self.root)

    def test_marker_symlink_rejected(self):
        marker = self.root / 'config/remote.json'
        content = marker.read_text()
        marker.unlink()
        target = self.root / 'marker'
        target.write_text(content)
        marker.symlink_to(target)
        with self.assertRaises(remote.RemoteError):
            remote.workspace_id(self.root)

    def test_store_factory_keeps_local_workspaces_local(self):
        with patch('jobflow.remote_store.client_from_env', return_value=self.client):
            self.assertIsInstance(Store(self.root / 'data/jobflow.sqlite3'), RemoteStore)
        with patch.dict(os.environ, {'JOBFLOW_BACKEND': ''}):
            store = Store(self.root / 'data/jobflow.sqlite3')
            self.assertNotIsInstance(store, RemoteStore)
            store.close()

    def test_lease_nested_and_child_environment_are_isolated(self):
        before = dict(os.environ)
        with remote.RemoteLease(self.root, client=self.client) as outer:
            self.assertEqual(outer.token, remote.token_for(self.root))
            child = remote.lease_environment(self.root)
            self.assertEqual(self.workspace, child['JOBFLOW_LEASE_WORKSPACE_ID'])
            self.assertEqual(before, dict(os.environ))
            with remote.RemoteLease(self.root, client=self.client) as nested:
                self.assertEqual(outer.token, nested.token)
            self.assertEqual(outer.token, self.client.token)
        self.assertIsNone(self.client.token)
        with self.assertRaises(remote.RemoteError):
            remote.token_for(self.root)
        self.assertEqual(1, sum(n == 'jobflow_workspace_lease' and p['p_action'] == 'release'
                                for n, p in self.client.calls))

    def test_inherited_token_cannot_authorize_another_workspace(self):
        with patch.dict(os.environ, {'JOBFLOW_LEASE_WORKSPACE_ID': str(uuid.uuid4()),
                                     'JOBFLOW_LEASE_TOKEN': 'x' * 43}):
            with self.assertRaises(remote.RemoteError):
                remote.token_for(self.root)
        with patch.dict(os.environ, {'JOBFLOW_LEASE_WORKSPACE_ID': self.workspace,
                                     'JOBFLOW_LEASE_TOKEN': 'x' * 43}):
            with self.assertRaises(remote.RemoteBusyError):
                with remote.RemoteLease(self.root, client=self.client):
                    pass

    def test_busy_and_lost_leases_fail_closed(self):
        self.client.lease_available = False
        with self.assertRaises(remote.RemoteBusyError):
            with remote.RemoteLease(self.root, client=self.client):
                self.fail('entered occupied lease')
        self.client.lease_available = True
        with remote.RemoteLease(self.root, client=self.client) as lease:
            lease._lost.set()
            with self.assertRaises(remote.RemoteBusyError):
                remote.token_for(self.root)

    def test_nested_lease_observes_parent_renewal_failure(self):
        with remote.RemoteLease(self.root, client=self.client) as outer:
            with remote.RemoteLease(self.root, client=self.client):
                outer._lost.set()
                with self.assertRaises(remote.RemoteBusyError):
                    remote.token_for(self.root)

    def test_writes_require_lease_and_use_marker_workspace(self):
        store = RemoteStore(self.root / 'data/jobflow.sqlite3', self.client)
        with self.assertRaises(remote.RemoteError):
            store.put('jobs', 'one', {'id': 'one'})
        with remote.RemoteLease(self.root, client=self.client):
            store.put('jobs', 'one', {'id': 'one'})
        mutation = [p for n, p in self.client.calls if n == 'jobflow_records_put'][0]
        self.assertEqual(self.workspace, mutation['p_workspace'])
        self.assertNotIn('workspace_id', mutation['p_rows'][0]['body'])

    def test_batch_preloads_gets_and_saves_bounded_updates(self):
        self.client.rows = {('jobs', str(i)): self.row(str(i)) for i in range(12)}
        store = RemoteStore(self.root / 'data/jobflow.sqlite3', self.client)
        with remote.RemoteLease(self.root, client=self.client):
            with patch('jobflow.remote_store.BATCH_SIZE', 5):
                with store.batch(preload=('jobs',)):
                    for i in range(12):
                        value = {**store.get('jobs', str(i)), 'score': 70}
                        store.put('jobs', str(i), value)
                    self.assertEqual(70, store.get('jobs', '11')['score'])
        self.assertEqual(0, sum(n == 'jobflow_record_get' for n, _ in self.client.calls))
        writes = [p for n, p in self.client.calls if n == 'jobflow_records_put']
        self.assertEqual([5, 5, 2], [len(p['p_rows']) for p in writes])

    def test_batch_saves_completed_work_on_validation_failure(self):
        store = RemoteStore(self.root / 'data/jobflow.sqlite3', self.client)
        with remote.RemoteLease(self.root, client=self.client):
            with self.assertRaisesRegex(ValueError, 'validation'):
                with store.batch():
                    store.put('tasks', 'done', {'id': 'done', 'status': 'done'})
                    raise ValueError('validation')
        self.assertEqual('done', self.client.rows['tasks', 'done']['body']['status'])

    def test_batch_bounds_utf8_payload_before_adding_next_row(self):
        store = RemoteStore(self.root / 'data/jobflow.sqlite3', self.client)
        bodies = [self.row(str(i), '\u4e2d' * length)['body']
                  for i, length in enumerate((80, 80, 500, 80, 80))]
        with remote.RemoteLease(self.root, client=self.client):
            with patch('jobflow.remote_store.BATCH_BYTES', 750):
                store.put_many('jobs', iter(bodies))
        writes = [p['p_rows'] for n, p in self.client.calls if n == 'jobflow_records_put']
        self.assertEqual([1, 1, 1, 1, 1], [len(rows) for rows in writes])
        self.assertEqual(bodies, [row['body'] for rows in writes for row in rows])
        for rows in writes:
            size = len(json.dumps(rows, ensure_ascii=False, separators=(',', ':')).encode())
            self.assertTrue(size <= 752 or (len(rows) == 1 and rows[0]['id'] == '2'))

    def test_replacing_buffered_row_counts_only_latest_payload(self):
        store = RemoteStore(self.root / 'data/jobflow.sqlite3', self.client)
        with remote.RemoteLease(self.root, client=self.client):
            with patch('jobflow.remote_store.BATCH_BYTES', 750):
                with store.batch():
                    for score in range(6):
                        store.put('jobs', 'a', {'id': 'a', 'description': '\u4e2d' * 80, 'score': score})
                    store.put('jobs', 'b', {'id': 'b'})
        writes = [p['p_rows'] for n, p in self.client.calls if n == 'jobflow_records_put']
        self.assertEqual([2], [len(rows) for rows in writes])
        self.assertEqual(5, self.client.rows['jobs', 'a']['body']['score'])

    def test_import_and_verification_share_count_and_byte_bounds(self):
        store = RemoteStore(self.root / 'data/jobflow.sqlite3', self.client)
        records = [self.row(str(i), '\u4e2d' * 80) for i in range(5)]
        runs = [{'id': i, 'details': '\u4e2d' * 100} for i in range(5)]
        with patch('jobflow.remote_store.BATCH_SIZE', 2):
            with patch('jobflow.remote_store.BATCH_BYTES', 750):
                with remote.RemoteLease(self.root, client=self.client):
                    store.import_records(iter(records))
                    store.import_runs(iter(runs))
                checked = store.verify_import(iter(records), iter(runs))
        self.assertEqual({'records_mismatched': 0, 'runs_mismatched': 0}, checked)
        self.assertEqual(runs, self.client.runs)
        for name, params in self.client.calls:
            if name not in ('jobflow_records_put', 'jobflow_runs_import', 'jobflow_import_verify'):
                continue
            rows = params.get('p_rows', params.get('p_records', params.get('p_runs')))
            self.assertLessEqual(len(rows), 2)
            self.assertLessEqual(len(json.dumps(rows, ensure_ascii=False,
                                              separators=(',', ':')).encode()), 752)

    def test_batch_does_not_flush_after_lease_loss(self):
        store = RemoteStore(self.root / 'data/jobflow.sqlite3', self.client)
        with remote.RemoteLease(self.root, client=self.client) as lease:
            with self.assertRaises(remote.RemoteBusyError):
                with store.batch():
                    store.put('tasks', 'done', {'id': 'done'})
                    lease._lost.set()
        self.assertNotIn(('tasks', 'done'), self.client.rows)

    def test_group_update_is_one_transaction(self):
        store = RemoteStore(self.root / 'data/jobflow.sqlite3', self.client)
        with remote.RemoteLease(self.root, client=self.client):
            store.put_groups({'applications': [{'id': 'a'}], 'tasks': [{'id': 't'}]})
        self.assertEqual(1, sum(n == 'jobflow_records_put' for n, _ in self.client.calls))

    def test_import_preserves_timestamps_and_verifies_without_downloading_bodies(self):
        store = RemoteStore(self.root / 'data/jobflow.sqlite3', self.client)
        row = self.row('owner')
        original = {**row, 'body': json.dumps(row['body'])}
        with remote.RemoteLease(self.root, client=self.client):
            store.import_records([original])
        self.assertEqual(row, self.client.rows['jobs', 'owner'])
        self.assertEqual({'records_mismatched': 0, 'runs_mismatched': 0}, store.verify_import([original]))
        self.assertFalse(any(n == 'jobflow_records_page' for n, _ in self.client.calls))

    def test_paginated_projection_keeps_full_description_in_database(self):
        self.client.rows = {('jobs', str(i)): self.row(str(i)) for i in range(5)}
        store = RemoteStore(self.root / 'data/jobflow.sqlite3', self.client)
        with patch('jobflow.remote_store.PAGE_SIZE', 2):
            projected = store.all_projected_jobs()
        self.assertEqual(5, len(projected))
        self.assertTrue(all('description' not in row for row in projected))
        self.assertTrue(all('description' in row['body'] for row in self.client.rows.values()))
        self.assertEqual(3, sum(n == 'jobflow_records_page' for n, _ in self.client.calls))

    def test_read_records_refuses_mixed_revisions(self):
        self.client.rows = {('jobs', str(i)): self.row(str(i)) for i in range(3)}
        self.client.conflict_after_first_page = True
        with patch('jobflow.remote_store.client_from_env', return_value=self.client):
            with patch('jobflow.remote_store.UI_PAGE_SIZE', 2):
                with self.assertRaises(remote.RemoteError) as result:
                    read_records(self.root)
        self.assertEqual(409, result.exception.status)

    def test_ui_read_uses_bounded_500_row_pages_and_preserves_revision(self):
        self.client.rows = {('jobs', str(i)): self.row(str(i)) for i in range(1000)}
        self.client.revision = 17
        self.client.runs = [{'id': 3, 'details': '{"source_errors":[]}'}]
        with patch('jobflow.remote_store.client_from_env', return_value=self.client):
            records, runs, count = read_records(self.root)
        pages = [args for name, args in self.client.calls if name == 'jobflow_records_page']
        self.assertEqual(len(pages), 3)  # Includes the empty terminal page.
        self.assertTrue(all(args['p_limit'] == 500 for args in pages))
        self.assertEqual([args['p_revision'] for args in pages], [None, 17, 17])
        self.assertEqual({row['id'] for row in records['jobs']}, {str(i) for i in range(1000)})
        self.assertTrue(all('description' not in row for row in records['jobs']))
        self.assertTrue(all('description' in row['body'] for row in self.client.rows.values()))
        self.assertEqual(count, 1000)
        self.assertEqual(runs[0]['details'], {'source_errors': []})

    def test_ui_read_reduces_oversized_pages_without_advancing_cursor(self):
        self.client.rows = {('jobs', str(i)): self.row(str(i)) for i in range(501)}
        rpc = self.client.rpc
        attempts = []

        def bounded_rpc(name, args):
            attempts.append(args)
            if args['p_limit'] > 200:
                raise remote.RemoteResponseTooLarge('The private service response exceeded its size limit.')
            return rpc(name, args)

        with patch.object(self.client, 'rpc', side_effect=bounded_rpc), \
                patch('jobflow.remote_store.client_from_env', return_value=self.client):
            records, unused, count = read_records(self.root)
        self.assertEqual([args['p_limit'] for args in attempts], [500, 200, 200, 200])
        self.assertEqual(attempts[0]['p_after_id'], attempts[1]['p_after_id'])
        self.assertEqual(len({row['id'] for row in records['jobs']}), 501)
        self.assertEqual(count, 501)


if __name__ == '__main__':
    unittest.main()

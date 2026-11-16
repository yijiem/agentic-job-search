import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jobflow.cloud_accounts import Accounts
from jobflow.cloud_jobs import WorkQueue
from jobflow.remote_accounts import RemoteAccounts
from jobflow.remote_auth_state import RemotePendingSignIns, RemoteRateLimiter
from jobflow.remote_jobs import RemoteWorkQueue

ACCOUNT = '45f9e61a-68fc-4a0f-99fa-eb96fc5cabfd'
WORKSPACE = 'fe3d78ee-33f5-4b9e-844c-4aab84840249'
ROW = {'id': ACCOUNT, 'workspace_id': WORKSPACE, 'email': 'owner@example.test',
       'status': 'invited', 'onboarding_complete': True, 'adopted_at': None}


class FakeClient:
    def __init__(self):
        self.calls = []
        self.results = {}

    def rpc(self, name, params):
        self.calls.append((name, params))
        return self.results.get(name)


class RemoteAccountsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.client = FakeClient()
        self.accounts = RemoteAccounts(self.directory.name, self.client)

    def tearDown(self):
        self.directory.cleanup()

    def test_workspace_uses_registry_not_supplied_workspace(self):
        self.client.results['jobflow_account_get'] = ROW
        root = self.accounts.workspace({'id': ACCOUNT, 'workspace_id': '../other-user'})
        self.assertEqual(root.name, WORKSPACE)
        self.assertEqual(json.loads((root / 'config/remote.json').read_text()), {'workspace_id': WORKSPACE})
        self.assertEqual(self.client.calls, [('jobflow_account_get', {'p_account': ACCOUNT})])
        self.assertFalse((root / 'data/jobflow.sqlite3').exists())

    def test_unavailable_registry_account_has_no_workspace(self):
        with self.assertRaises(ValueError):
            self.accounts.workspace({'id': ACCOUNT, 'workspace_id': WORKSPACE})
        self.assertEqual(list(self.accounts.workspaces.iterdir()), [])
        self.client.results['jobflow_account_get'] = {**ROW, 'status': 'disabled'}
        with self.assertRaises(ValueError):
            self.accounts.workspace(ROW)
        self.assertEqual(list(self.accounts.workspaces.iterdir()), [])

    def test_symlink_workspace_is_rejected(self):
        self.client.results['jobflow_account_get'] = ROW
        (self.accounts.workspaces / WORKSPACE).symlink_to(self.directory.name)
        with self.assertRaises(ValueError):
            self.accounts.workspace(ROW)

    def test_sessions_transmit_hash_only(self):
        self.client.results['jobflow_session_create'] = True
        token, csrf = self.accounts.create_session(ROW)
        self.assertEqual(len(token), 43)
        self.assertEqual(len(csrf), 43)
        name, payload = self.client.calls[-1]
        self.assertEqual(name, 'jobflow_session_create')
        self.assertEqual(payload['p_token_hash'], hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, json.dumps(payload))
        self.accounts.session(token)
        self.accounts.revoke_session(token)
        self.assertNotIn(token, json.dumps(self.client.calls))

    def test_invalid_tokens_make_no_network_requests(self):
        for token in ('', 'x' * 42, '!' * 43, None, 12):
            self.assertIsNone(self.accounts.session(token))
            self.accounts.revoke_session(token)
        self.assertEqual(self.client.calls, [])

    def test_invite_normalizes_and_creates_workspace_idempotently(self):
        self.client.results['jobflow_account_invite'] = ROW
        self.assertEqual(self.accounts.invite('Owner@Example.TEST'), ROW)
        self.assertEqual(self.client.calls, [
            ('jobflow_account_invite', {'p_email': 'owner@example.test'}),
            ('jobflow_workspace_create', {'p_workspace': WORKSPACE})])

    def test_identity_binding_rejects_uninvited_and_invalid_identity(self):
        with self.assertRaises(ValueError):
            self.accounts.authenticate_identity('verified-provider-id', ROW['email'])
        count = len(self.client.calls)
        with self.assertRaises(ValueError):
            self.accounts.authenticate_identity('invalid identity', ROW['email'])
        self.assertEqual(len(self.client.calls), count)

    def test_owner_receipt_must_match_account_and_workspace(self):
        receipt = {'account_id': ACCOUNT, 'workspace_id': WORKSPACE,
                   'onboarding_complete': True, 'adopted_at': '2026-11-14T00:00:00+00:00',
                   'source_id': 'a' * 64}
        with self.assertRaises(ValueError):
            self.accounts.record_adoption(ROW, {**receipt, 'workspace_id': ACCOUNT})
        self.assertEqual(self.client.calls, [])
        self.client.results['jobflow_account_adopted'] = ROW
        self.accounts.record_adoption(ROW, receipt)
        self.assertEqual(self.client.calls[-1][1]['p_source_id'], 'a' * 64)

    def test_remote_factories_are_explicit_and_local_mode_unchanged(self):
        with patch.dict(os.environ, {'JOBFLOW_BACKEND': 'supabase'}), \
                patch('jobflow.remote_accounts.client_from_env', return_value=self.client), \
                patch('jobflow.remote_jobs.client_from_env', return_value=self.client):
            self.assertIsInstance(Accounts(self.directory.name), RemoteAccounts)
            self.assertIsInstance(WorkQueue(self.directory.name), RemoteWorkQueue)
        with patch.dict(os.environ, {'JOBFLOW_BACKEND': ''}):
            self.assertIsInstance(Accounts(self.directory.name), Accounts)
            self.assertIsInstance(WorkQueue(self.directory.name), WorkQueue)


class RemoteAuthStateTests(unittest.TestCase):
    def test_pending_tokens_are_hashed_and_verifier_is_bound(self):
        client = FakeClient()
        pending = RemotePendingSignIns(client=client)
        token, verifier = pending.start('Owner@example.test', 'z' * 43)
        self.assertEqual(len(verifier), 86)
        payload = client.calls[-1][1]
        self.assertEqual(payload['p_email'], 'owner@example.test')
        self.assertEqual(payload['p_verifier'], verifier)
        self.assertEqual(payload['p_token_hash'], hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(payload['p_old_hash'], hashlib.sha256(('z' * 43).encode()).hexdigest())
        self.assertNotIn(token, json.dumps(payload))
        pending.consume(token)
        pending.revoke(token)
        self.assertNotIn(token, json.dumps(client.calls))

    def test_invalid_pending_tokens_do_not_call_database(self):
        client = FakeClient()
        pending = RemotePendingSignIns(client=client)
        for token in (None, '', '!' * 43):
            self.assertIsNone(pending.consume(token))
            pending.revoke(token)
        self.assertEqual(client.calls, [])

    def test_rate_limit_hashes_keys(self):
        client = FakeClient()
        client.results['jobflow_rate_allow'] = True
        limiter = RemoteRateLimiter(client=client)
        self.assertTrue(limiter.allow('start:owner@example.test', 5, 900))
        self.assertNotIn('owner@example.test', json.dumps(client.calls))


class RemoteQueueTests(unittest.TestCase):
    def test_completion_is_fenced_by_claim(self):
        client = FakeClient()
        queue = RemoteWorkQueue(client=client)
        client.results['jobflow_queue_finish'] = False
        self.assertFalse(queue.finish({'account_id': ACCOUNT, 'claim_token': 'a' * 32}, 'done', 'Done.'))
        self.assertEqual(client.calls[-1][1]['p_token'], 'a' * 32)

    def test_invalid_command_never_reaches_database(self):
        client = FakeClient()
        queue = RemoteWorkQueue(client=client)
        with self.assertRaises(ValueError):
            queue.enqueue(ACCOUNT, 'arbitrary-script')
        self.assertEqual(client.calls, [])
        client.results['jobflow_queue_enqueue'] = True
        self.assertTrue(queue.enqueue(ACCOUNT, 'refresh'))


if __name__ == '__main__':
    unittest.main()

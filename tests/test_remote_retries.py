import hashlib
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from jobflow.remote import RemoteError, SupabaseClient


class Response(io.BytesIO):
    def __init__(self, body=b'{"ok":true}', headers=None):
        super().__init__(body)
        self.headers = headers or {}


class SequenceOpener:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append(request)
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def http_error(status):
    return HTTPError('https://project.supabase.co/private', status, 'private response', {},
                     io.BytesIO(b'{"message":"must not be echoed"}'))


class SafeTransportRetryTests(unittest.TestCase):
    def client(self, outcomes):
        opener = SequenceOpener(outcomes)
        return SupabaseClient('https://project.supabase.co', 'sb_secret_test', opener=opener), opener

    def test_read_only_rpc_recovers_with_two_bounded_retries(self):
        client, opener = self.client([URLError('temporary'), http_error(503), Response()])
        with patch('jobflow.remote.time.sleep') as sleep:
            self.assertEqual(client.rpc('jobflow_file_get', {'p_path': 'config/profile.json'}), {'ok': True})
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(client.requests, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.25, 0.75])
        self.assertEqual(opener.calls[0].data, opener.calls[2].data)

    def test_retries_exhaust_after_three_total_attempts(self):
        client, opener = self.client([TimeoutError(), TimeoutError(), TimeoutError(), Response()])
        with patch('jobflow.remote.time.sleep'), self.assertRaises(RemoteError) as caught:
            client.rpc('jobflow_records_page', {})
        self.assertEqual(len(opener.calls), 3)
        self.assertTrue(caught.exception.retry_exhausted)

    def test_http_retry_codes_are_explicit_and_auth_is_not_retried(self):
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                client, opener = self.client([http_error(status), Response()])
                with patch('jobflow.remote.time.sleep'):
                    self.assertEqual(client.rpc('jobflow_record_get', {}), {'ok': True})
                self.assertEqual(len(opener.calls), 2)
        for status in (400, 401, 403, 404, 409, 422):
            with self.subTest(status=status):
                client, opener = self.client([http_error(status), Response()])
                with patch('jobflow.remote.time.sleep') as sleep, self.assertRaises(RemoteError) as caught:
                    client.rpc('jobflow_file_get', {})
                self.assertEqual(len(opener.calls), 1)
                self.assertNotIn('must not be echoed', str(caught.exception))
                sleep.assert_not_called()

    def test_mutating_rpc_and_unknown_rpc_are_never_replayed(self):
        for name in ('jobflow_pending_consume', 'jobflow_queue_claim', 'jobflow_run_start',
                     'jobflow_records_put', 'jobflow_rate_allow', 'jobflow_workspace_lease',
                     'jobflow_account_invite', 'jobflow_future_operation'):
            with self.subTest(name=name):
                client, opener = self.client([URLError('uncertain response'), Response()])
                with patch('jobflow.remote.time.sleep') as sleep, self.assertRaises(RemoteError):
                    client.rpc(name, {})
                self.assertEqual(len(opener.calls), 1)
                sleep.assert_not_called()

    def test_invalid_json_and_compression_are_not_transport_retries(self):
        for response in (Response(b'not json'), Response(b'not gzip', {'Content-Encoding': 'gzip'})):
            client, opener = self.client([response, Response()])
            with patch('jobflow.remote.time.sleep') as sleep, self.assertRaises(RemoteError):
                client.rpc('jobflow_file_get', {})
            self.assertEqual(len(opener.calls), 1)
            sleep.assert_not_called()

    def test_storage_reads_and_immutable_uploads_can_retry_identical_bytes(self):
        client, opener = self.client([URLError('temporary'), Response(b'private bytes')])
        with patch('jobflow.remote.time.sleep'):
            self.assertEqual(client.storage_download('jobflow-private', 'workspace/file'), b'private bytes')
        self.assertEqual(len(opener.calls), 2)
        payload = b'private immutable bytes'
        key = '11111111-1111-1111-1111-111111111111/' + hashlib.sha256(payload).hexdigest()
        client, opener = self.client([TimeoutError(), Response()])
        with patch('jobflow.remote.time.sleep'):
            client.storage_upload('jobflow-private', key, payload)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(opener.calls[0].data, payload)
        self.assertEqual(opener.calls[1].data, payload)
        self.assertEqual(opener.calls[0].full_url, opener.calls[1].full_url)
        client, opener = self.client([TimeoutError(), Response()])
        with patch('jobflow.remote.time.sleep') as sleep, self.assertRaises(RemoteError):
            client.storage_upload('jobflow-private', 'mutable/report.json', payload)
        self.assertEqual(len(opener.calls), 1)
        sleep.assert_not_called()

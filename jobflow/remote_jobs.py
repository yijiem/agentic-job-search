"""Shared durable queue with PostgreSQL claims and fenced worker completion."""
import secrets

from .cloud_jobs import PRIORITY
from .remote import client_from_env
from .remote_accounts import _uuid


class RemoteWorkQueue:
    def __init__(self, root=None, client=None):
        self.client = client if client is not None else client_from_env()

    def enqueue(self, account_id, command='run'):
        if command not in PRIORITY:
            raise ValueError('Unknown background command.')
        return bool(self.client.rpc('jobflow_queue_enqueue',
            {'p_account': _uuid(account_id), 'p_command': command}))

    def status(self, account_id):
        return self.client.rpc('jobflow_queue_status', {'p_account': _uuid(account_id)})

    def claim(self):
        return self.client.rpc('jobflow_queue_claim', {'p_token': secrets.token_hex(16)})

    def finish(self, item, status, message):
        if status not in ('retry', 'done', 'partial', 'failed'):
            raise ValueError('Unknown background result.')
        if not isinstance(message, str) or len(message) > 1000:
            raise ValueError('Invalid background result message.')
        return bool(self.client.rpc('jobflow_queue_finish', {
            'p_account': _uuid(item['account_id']), 'p_token': item['claim_token'],
            'p_status': status, 'p_message': message}))

    def renew(self, item):
        return bool(self.client.rpc('jobflow_queue_renew', {
            'p_account': _uuid(item['account_id']), 'p_token': item['claim_token']}))

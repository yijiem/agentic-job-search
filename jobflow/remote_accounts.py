"""Invite-only account registry and opaque sessions in private PostgreSQL tables."""
import hashlib
import json
import re
import secrets
import uuid
from pathlib import Path

from .cloud_accounts import SESSION_SECONDS, _private_directory, normalize_email
from .remote import client_from_env
from .workflow import load_config


def _uuid(value):
    try:
        parsed = str(uuid.UUID(value))
        if parsed != value:
            raise ValueError
        return value
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Invalid account identifier.') from None


def _token_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', value):
        return None
    return hashlib.sha256(value.encode('ascii')).hexdigest()


class RemoteAccounts:
    """The server is the only caller; browser identity never selects a workspace."""
    def __init__(self, root, client=None):
        supplied = Path(root).expanduser()
        if supplied.is_symlink():
            raise ValueError('The cloud storage root must not be a symbolic link.')
        self.root = _private_directory(supplied.resolve())
        self.data = _private_directory(self.root / 'data')
        self.workspaces = _private_directory(self.root / 'workspaces')
        self.client = client if client is not None else client_from_env()

    def invite(self, email):
        account = self.client.rpc('jobflow_account_invite', {'p_email': normalize_email(email)})
        # Retry-safe if a connection failed between invitation and workspace creation.
        self.client.rpc('jobflow_workspace_create', {'p_workspace': _uuid(account['workspace_id'])})
        return account

    def lookup_email(self, email):
        return self.client.rpc('jobflow_account_lookup', {'p_email': normalize_email(email)})

    def list_accounts(self):
        return self.client.rpc('jobflow_account_list', {})

    def authenticate_identity(self, user_id, email):
        if (not isinstance(user_id, str) or not user_id or len(user_id) > 255
                or any(ord(char) < 33 for char in user_id)):
            raise ValueError('The authentication provider did not return a valid identity.')
        account = self.client.rpc('jobflow_account_authenticate',
            {'p_identity': user_id, 'p_email': normalize_email(email)})
        if not account:
            raise ValueError('This account is not invited to this application.')
        return account

    def workspace(self, account):
        account_id = _uuid(account.get('id') if isinstance(account, dict) else None)
        # Resolve again from the registry. Ignore caller-supplied email/workspace IDs.
        row = self.client.rpc('jobflow_account_get', {'p_account': account_id})
        if not row or row.get('status') not in ('active', 'invited'):
            raise ValueError('Unknown or unavailable account.')
        workspace_id = _uuid(row['workspace_id'])
        if self.workspaces.is_symlink():
            raise ValueError('Private workspace storage must not be a symbolic link.')
        destination = _private_directory(self.workspaces / workspace_id)
        config = _private_directory(destination / 'config')
        marker = config / 'remote.json'
        if marker.is_symlink():
            raise ValueError('Private workspace files must not be symbolic links.')
        content = json.dumps({'workspace_id': workspace_id}) + '\n'
        if not marker.exists() or marker.read_text() != content:
            marker.write_text(content, encoding='utf-8')
            marker.chmod(0o600)
        return destination

    def create_session(self, account):
        account_id = _uuid(account.get('id') if isinstance(account, dict) else None)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        created = self.client.rpc('jobflow_session_create', {
            'p_account': account_id, 'p_token_hash': _token_hash(token),
            'p_csrf': csrf, 'p_seconds': SESSION_SECONDS})
        if not created:
            raise ValueError('Sign in before creating a session.')
        return token, csrf

    def session(self, raw_token):
        digest = _token_hash(raw_token)
        if digest is None:
            return None
        return self.client.rpc('jobflow_session_get', {'p_token_hash': digest})

    def revoke_session(self, raw_token):
        digest = _token_hash(raw_token)
        if digest is not None:
            self.client.rpc('jobflow_session_revoke', {'p_token_hash': digest})

    def mark_onboarding_complete(self, account):
        load_config(self.workspace(account))
        return self.client.rpc('jobflow_account_complete', {'p_account': _uuid(account['id'])})

    def record_adoption(self, account, receipt):
        if (receipt.get('account_id') != account.get('id') or
                receipt.get('workspace_id') != account.get('workspace_id') or
                not receipt.get('onboarding_complete') or not receipt.get('adopted_at')):
            raise ValueError('The owner migration receipt does not match this account.')
        result = self.client.rpc('jobflow_account_adopted', {
            'p_account': _uuid(account['id']), 'p_adopted_at': receipt['adopted_at'],
            'p_source_id': receipt.get('source_id', '')})
        if not result:
            raise ValueError('The account cannot accept this owner migration.')
        return result

    _record_adoption = record_adoption

    def begin_adoption(self, account, source_id):
        if not isinstance(source_id, str) or not re.fullmatch(r'[0-9a-f]{64}', source_id):
            raise ValueError('Invalid owner migration fingerprint.')
        return self.client.rpc('jobflow_account_begin_adoption', {
            'p_account': _uuid(account['id']), 'p_source_id': source_id})

    def adopt(self, email, source):
        from .remote_migration import adopt_owner
        return adopt_owner(self, email, source)

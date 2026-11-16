"""Restart-safe, one-use PKCE bindings and bounded sign-in rate limits."""
import hashlib
import secrets

from .cloud_accounts import normalize_email
from .cloud_auth import PendingSignIns
from .remote import client_from_env


class RemotePendingSignIns:
    def __init__(self, maximum_keys=10000, ttl=600, client=None):
        if not 1 <= int(maximum_keys) <= 10000 or not 1 <= int(ttl) <= 600:
            raise ValueError('Invalid pending sign-in limits.')
        self.maximum_keys, self.ttl = int(maximum_keys), int(ttl)
        self.client = client if client is not None else client_from_env()

    def start(self, email, old_token=''):
        token, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        self.client.rpc('jobflow_pending_start', {
            'p_token_hash': PendingSignIns._key(token),
            'p_old_hash': PendingSignIns._key(old_token),
            'p_email': normalize_email(email), 'p_verifier': verifier,
            'p_seconds': self.ttl, 'p_maximum': self.maximum_keys})
        return token, verifier

    def consume(self, token):
        digest = PendingSignIns._key(token)
        if digest is None:
            return None
        return self.client.rpc('jobflow_pending_consume', {'p_token_hash': digest})

    def revoke(self, token):
        digest = PendingSignIns._key(token)
        if digest is not None:
            self.client.rpc('jobflow_pending_revoke', {'p_token_hash': digest})


class RemoteRateLimiter:
    def __init__(self, maximum_keys=10000, client=None):
        self.maximum_keys = min(10000, max(1, int(maximum_keys)))
        self.client = client if client is not None else client_from_env()

    def allow(self, key, limit, window):
        if not isinstance(key, str) or not 1 <= int(limit) <= 10000 or not 1 <= int(window) <= 86400:
            raise ValueError('Invalid rate limit.')
        return bool(self.client.rpc('jobflow_rate_allow', {
            'p_key_hash': hashlib.sha256(key.encode('utf-8')).hexdigest(),
            'p_limit': int(limit), 'p_window': int(window), 'p_maximum': self.maximum_keys}))

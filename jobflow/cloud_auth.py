"""Email-code authentication via Supabase; provider tokens never reach disk/browser.

Supabase verifies the email. Jobflow independently enforces its invite list and
issues an opaque, HttpOnly application session. No passwords are collected.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from urllib.parse import urlsplit


class AuthError(ValueError):
    pass


class SupabaseEmailAuth:
    def __init__(self, url=None, key=None):
        self.url = (url or os.environ.get('SUPABASE_URL', '')).rstrip('/')
        self.key = key or os.environ.get('SUPABASE_PUBLISHABLE_KEY', '')
        parsed = urlsplit(self.url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path):
            raise AuthError('Set SUPABASE_URL to your HTTPS Supabase project URL.')
        if not self.key or '\n' in self.key or '\r' in self.key:
            raise AuthError('Set SUPABASE_PUBLISHABLE_KEY to the project publishable or legacy anon key.')
        if self.key.startswith('sb_secret_'):
            raise AuthError('Use the publishable key, not a Supabase secret key.')

    def _post(self, endpoint, payload):
        request = urllib.request.Request(self.url + '/auth/v1/' + endpoint,
            data=json.dumps(payload).encode('utf-8'), method='POST',
            headers={'apikey': self.key, 'Content-Type': 'application/json'})
        try:
            # Never include provider response bodies in errors or logs: they may
            # contain credentials or reveal whether an address is registered.
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise AuthError('The sign-in provider returned an invalid response.')
                value = json.loads(raw)
        except urllib.error.HTTPError:
            raise AuthError('Sign-in could not be completed. Request a fresh code and try again.') from None
        except (OSError, ValueError):
            raise AuthError('The sign-in provider is unavailable. Please try again shortly.') from None
        if not isinstance(value, dict):
            raise AuthError('The sign-in provider returned an invalid response.')
        return value

    def start(self, email):
        # The caller checks the local invite list before allowing account creation.
        self._post('otp', {'email': email, 'create_user': True})

    def verify(self, email, token):
        if not isinstance(token, str) or not re.fullmatch(r'[0-9]{6,10}', token):
            raise AuthError('Enter the numeric code from your sign-in email.')
        value = self._post('verify', {'email': email, 'token': token, 'type': 'email'})
        user = value.get('user')
        if (not isinstance(user, dict) or not user.get('id') or
                str(user.get('email', '')).casefold() != email.casefold() or
                not user.get('email_confirmed_at')):
            raise AuthError('The sign-in provider did not confirm this email address.')
        # Deliberately discard access/refresh tokens: this milestone connects only
        # identity. Provider API access is a separate, future consent flow.
        return {'id': str(user['id']), 'email': str(user['email']).casefold()}


class RateLimiter:
    """Bounded single-instance rate limiting; provider limits also remain active."""
    def __init__(self, maximum_keys=10000):
        self.entries = OrderedDict()
        self.maximum_keys = maximum_keys
        self.lock = threading.Lock()

    def allow(self, key, limit, window):
        now = time.monotonic()
        with self.lock:
            count, start = self.entries.pop(key, (0, now))
            if now - start >= window:
                count, start = 0, now
            self.entries[key] = (count + 1, start)
            while len(self.entries) > self.maximum_keys:
                self.entries.popitem(last=False)
            return count < limit

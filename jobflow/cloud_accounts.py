"""Invite-only cloud account routing, opaque sessions, and safe workspace adoption.

Identity verification belongs to the configured authentication provider. Call
``authenticate_identity`` only with the provider's verified, confirmed identity;
request bodies and decoded-but-unverified tokens are not identity evidence.

The pilot keeps one existing Jobflow workspace per account. All filesystem
routing is derived from registry-generated UUIDs, never a request-supplied path.
"""
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .storage import utcnow
from .workflow import load_config

SESSION_SECONDS = 12 * 60 * 60
_EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}$")
_COPY_EXTENSIONS = {'.json', '.csv', '.tsv', '.md', '.txt', '.pdf', '.docx', '.xlsx', '.html',
                    '.tex', '.cls', '.sty', '.bib', '.diff', '.patch', '.png', '.jpg', '.jpeg', '.webp'}
_RESUME_FILE = re.compile(r'^resume-[0-9a-f]{32}$')
_SECRET_NAME = re.compile(r'(?:^|[._-])(?:credentials?|secrets?|tokens?|api[_-]?keys?)(?:[._-]|$)', re.I)
_SECRET_JSON = re.compile(rb'"(?:access_token|refresh_token|client_secret|api_key|private_key|password)"\s*:', re.I)


def normalize_email(email):
    if not isinstance(email, str):
        raise ValueError('Enter a valid email address.')
    email = email.strip().lower()
    if len(email) > 254 or not _EMAIL.fullmatch(email) or '..' in email or '@.' in email:
        raise ValueError('Enter a valid email address.')
    return email


def _private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Private workspace directories must not be symbolic links.')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError('Private workspace storage must be a directory.')
    path.chmod(0o700)
    return path


def _private_file(path):
    path = Path(path)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    if path.is_symlink():
        raise ValueError('Private workspace files must not be symbolic links.')
    descriptor = os.open(str(path), flags, 0o600)
    os.close(descriptor)
    path.chmod(0o600)


def _account(row):
    if row is None:
        return None
    return {key: row[key] for key in ('id', 'email', 'workspace_id', 'status', 'created_at', 'adopted_at')} | {
        'onboarding_complete': bool(row['onboarding_complete'])}


class Accounts:
    def __new__(cls, root):
        if cls is Accounts and os.environ.get('JOBFLOW_BACKEND') == 'supabase':
            from .remote_accounts import RemoteAccounts
            return RemoteAccounts(root)
        return super().__new__(cls)

    def __init__(self, root):
        supplied = Path(root).expanduser()
        if supplied.is_symlink():
            raise ValueError('The cloud storage root must not be a symbolic link.')
        self.root = _private_directory(supplied.resolve())
        self.data = _private_directory(self.root / 'data')
        self.workspaces = _private_directory(self.root / 'workspaces')
        self.path = self.data / 'accounts.sqlite3'
        _private_file(self.path)
        with self._connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                    workspace_id TEXT NOT NULL UNIQUE, identity_id TEXT UNIQUE,
                    status TEXT NOT NULL DEFAULT 'invited', created_at TEXT NOT NULL,
                    onboarding_complete INTEGER NOT NULL DEFAULT 0, adopted_at TEXT);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    csrf_token TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS sessions_account ON sessions(account_id);
            ''')

    @contextmanager
    def _connect(self):
        if self.path.is_symlink() or self.data.is_symlink():
            raise ValueError('The account registry must not be a symbolic link.')
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def invite(self, email):
        """Create an administrative allowlist entry; sends no email."""
        email = normalize_email(email)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM accounts WHERE email=?', (email,)).fetchone()
            if row is None:
                account_id, workspace_id = str(uuid.uuid4()), str(uuid.uuid4())
                db.execute('INSERT INTO accounts(id,email,workspace_id,created_at) VALUES(?,?,?,?)',
                           (account_id, email, workspace_id, utcnow()))
                row = db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
            return _account(row)

    def lookup_email(self, email):
        email = normalize_email(email)
        with self._connect() as db:
            return _account(db.execute('SELECT * FROM accounts WHERE email=?', (email,)).fetchone())

    def list_accounts(self):
        with self._connect() as db:
            return [_account(row) for row in db.execute('SELECT * FROM accounts ORDER BY created_at,id')]

    def get_account(self, account_id):
        with self._connect() as db:
            return _account(db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone())

    def authenticate_identity(self, user_id, email):
        """Bind an existing invite to a provider-verified, email-confirmed identity.

        The caller must obtain both fields from the trusted provider response.
        Neither a matching submitted email nor a JWT payload alone is sufficient.
        A bound identity can never claim a second email or replace another owner.
        """
        email = normalize_email(email)
        if (not isinstance(user_id, str) or not user_id or len(user_id) > 255
                or any(ord(char) < 33 for char in user_id)):
            raise ValueError('The authentication provider did not return a valid identity.')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM accounts WHERE email=?', (email,)).fetchone()
            if row is None or row['status'] not in ('invited', 'active'):
                raise ValueError('This account is not invited to this application.')
            if row['identity_id'] and row['identity_id'] != user_id:
                raise ValueError('This invitation is already bound to another identity.')
            other = db.execute('SELECT id FROM accounts WHERE identity_id=?', (user_id,)).fetchone()
            if other and other['id'] != row['id']:
                raise ValueError('This identity is already bound to another account.')
            db.execute("UPDATE accounts SET identity_id=?,status='active' WHERE id=?", (user_id, row['id']))
            return _account(db.execute('SELECT * FROM accounts WHERE id=?', (row['id'],)).fetchone())

    def workspace(self, account):
        """Resolve a registry account; ignore any caller-supplied workspace ID."""
        account_id = account.get('id') if isinstance(account, dict) else None
        with self._connect() as db:
            row = db.execute('SELECT workspace_id FROM accounts WHERE id=?', (account_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown account.')
        workspace_id = row['workspace_id']
        try:
            if str(uuid.UUID(workspace_id)) != workspace_id:
                raise ValueError('Invalid workspace identifier.')
        except (ValueError, TypeError, AttributeError):
            raise ValueError('Invalid workspace identifier.') from None
        if self.workspaces.is_symlink():
            raise ValueError('Private workspace storage must not be a symbolic link.')
        destination = self.workspaces / workspace_id
        if destination.is_symlink():
            raise ValueError('Private workspace storage must not be a symbolic link.')
        return destination

    def create_session(self, account):
        account_id = account.get('id') if isinstance(account, dict) else None
        token, csrf_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status,identity_id FROM accounts WHERE id=?', (account_id,)).fetchone()
            if row is None or row['status'] != 'active' or not row['identity_id']:
                raise ValueError('Sign in before creating a session.')
            db.execute('DELETE FROM sessions WHERE expires_at<=?', (now,))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(), account_id, csrf_token, now, now + SESSION_SECONDS))
        return token, csrf_token

    def session(self, raw_token):
        if not isinstance(raw_token, str) or len(raw_token) != 43:
            return None
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        with self._connect() as db:
            session = db.execute('SELECT * FROM sessions WHERE token_hash=? AND expires_at>?',
                                 (token_hash, time.time())).fetchone()
            if not session:
                return None
            account = db.execute("SELECT * FROM accounts WHERE id=? AND status='active'",
                                 (session['account_id'],)).fetchone()
            if not account:
                return None
            return {'account': _account(account), 'csrf_token': session['csrf_token']}

    def revoke_session(self, raw_token):
        if not isinstance(raw_token, str) or len(raw_token) != 43:
            return
        with self._connect() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (hashlib.sha256(raw_token.encode()).hexdigest(),))

    def mark_onboarding_complete(self, account):
        """Mark completion only after this account's workspace passes validation."""
        root = self.workspace(account)
        load_config(root)
        with self._connect() as db:
            db.execute('UPDATE accounts SET onboarding_complete=1 WHERE id=?', (account['id'],))
            return _account(db.execute('SELECT * FROM accounts WHERE id=?', (account['id'],)).fetchone())

    def adopt(self, email, source):
        """Snapshot an existing workspace for one invited account, without merging.

        Uses the existing workflow lock and SQLite backup API, so committed WAL
        data and original record/run timestamps survive. A migration receipt
        makes retries safe and prevents a later import from replacing live data.
        No source records are changed, rescored, or regenerated.
        """
        account = self.lookup_email(email)
        if account is None or account['status'] not in ('invited', 'active'):
            raise ValueError('Invite this email before adopting an existing workspace.')
        source = Path(source).expanduser()
        if source.is_symlink():
            raise ValueError('The source workspace must not be a symbolic link.')
        source = source.resolve()
        if source.is_relative_to(self.workspaces):
            raise ValueError('An existing cloud account cannot be imported into another account.')
        for relative in ('data', 'config', 'data/jobflow.sqlite3', 'config/settings.json', 'config/profile.json'):
            path = source / relative
            if path.is_symlink() or not path.exists():
                raise ValueError('The source workspace must contain regular configuration and database files.')
        destination = self.workspace(account)
        source_id = hashlib.sha256(str(source).encode()).hexdigest()
        lock_path = self.data / ('adopt-' + account['id'] + '.lock')
        _private_file(lock_path)
        with lock_path.open('a') as account_lock:
            fcntl.flock(account_lock, fcntl.LOCK_EX)
            receipt_path = destination / 'data/cloud-migration.json'
            if receipt_path.is_symlink():
                raise ValueError('The migration receipt must not be a symbolic link.')
            if receipt_path.is_file():
                receipt = json.loads(receipt_path.read_text())
                if receipt.get('account_id') != account['id'] or receipt.get('source_id') != source_id:
                    raise ValueError('This workspace already belongs to a different migration.')
                self._record_adoption(account, receipt)
                return self._adoption_result(account, receipt, False)
            if destination.exists() and any(destination.iterdir()):
                raise ValueError('The destination workspace already contains data; adoption never overwrites it.')
            stage = None
            try:
                # Export into a unique sibling before attaching any owner metadata.
                stage = Path(tempfile.mkdtemp(prefix='.adopt-', dir=str(self.workspaces)))
                summary = export_workspace(source, stage)
                adopted_at = utcnow()
                marker = {'completed': True, 'migrated': True, 'source': 'existing_workspace',
                          'completed_at': adopted_at}
                for relative, value in (('config/onboarding.json', marker),
                                        ('config/hosted.json', {'version': 1})):
                    target = stage / relative
                    target.write_text(json.dumps(value, indent=2) + '\n')
                    target.chmod(0o600)
                receipt = {'version': 1, 'account_id': account['id'], 'workspace_id': account['workspace_id'],
                           'source_id': source_id, 'adopted_at': adopted_at, 'counts': summary['counts'],
                           'onboarding_complete': True, 'skipped_files': summary['skipped_files']}
                target_receipt = stage / 'data/cloud-migration.json'
                target_receipt.write_text(json.dumps(receipt, indent=2) + '\n')
                target_receipt.chmod(0o600)
                if destination.exists():
                    # Removing an empty directory is safe; os.replace does not merge.
                    destination.rmdir()
                os.replace(str(stage), str(destination))
                stage = None
                self._record_adoption(account, receipt)
                return self._adoption_result(account, receipt, True)
            finally:
                if stage is not None:
                    shutil.rmtree(stage)

    def _record_adoption(self, account, receipt):
        with self._connect() as db:
            db.execute('UPDATE accounts SET adopted_at=?,onboarding_complete=? WHERE id=?',
                       (receipt['adopted_at'], int(receipt['onboarding_complete']), account['id']))

    def _adoption_result(self, account, receipt, adopted):
        return {'account': self.lookup_email(account['email']), 'workspace_id': account['workspace_id'],
                'adopted': adopted, 'already_adopted': not adopted,
                'counts': receipt['counts'], 'skipped_files': receipt['skipped_files']}

    @staticmethod
    def _copy_private_tree(source, destination, label, skipped, resume_files=None):
        if source.is_symlink():
            skipped.append(label + ' (symbolic link)')
            return
        if not source.exists():
            return
        if not source.is_dir():
            skipped.append(label + ' (not a directory)')
            return
        _private_directory(destination)
        for path in sorted(source.iterdir()):
            relative = label + '/' + path.name
            if path.is_symlink():
                skipped.append(relative + ' (symbolic link)')
            elif path.name.startswith('.') or _SECRET_NAME.search(path.name):
                skipped.append(relative + ' (excluded private credential name)')
            elif path.is_dir():
                Accounts._copy_private_tree(path, destination / path.name, relative, skipped, resume_files)
            elif label == 'inputs/application-resumes' and _RESUME_FILE.fullmatch(path.name):
                # Library storage is intentionally extensionless. Only copy a
                # known immutable resume that agrees with the backed-up record;
                # this does not broaden the general private-file allowlist.
                metadata = (resume_files or {}).get(path.name)
                content = None
                if metadata:
                    try:
                        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
                        descriptor = os.open(str(path), flags)
                        with os.fdopen(descriptor, 'rb') as stream:
                            details = os.fstat(stream.fileno())
                            if stat.S_ISREG(details.st_mode) and details.st_size == metadata['size']:
                                content = stream.read(metadata['size'] + 1)
                    except OSError:
                        pass
                if (content is None or len(content) != metadata['size'] or
                        hashlib.sha256(content).hexdigest() != metadata['sha256']):
                    skipped.append(relative + ' (resume metadata or integrity mismatch)')
                    continue
                target = destination / path.name
                target.write_bytes(content)
                target.chmod(0o600)
            elif not path.is_file() or path.suffix.lower() not in _COPY_EXTENSIONS:
                skipped.append(relative + ' (unsupported file type)')
            elif path.suffix.lower() == '.json' and _SECRET_JSON.search(path.read_bytes()):
                skipped.append(relative + ' (credential fields)')
            else:
                target = destination / path.name
                shutil.copyfile(path, target)
                target.chmod(0o600)


def export_workspace(source, destination_directory):
    """Export a consistent portable owner snapshot without assigning an account.

    The destination must be absent or empty. Only the existing database, exact
    profile/settings, and allowlisted private input/report files are exported.
    No credentials, cloud account registry, sessions, or provider tokens are
    included. The original workspace remains authoritative until explicitly
    adopted and scheduling is moved to the host.
    """
    source, destination = Path(source).expanduser(), Path(destination_directory).expanduser()
    if source.is_symlink() or destination.is_symlink():
        raise ValueError('Workspace export paths must not be symbolic links.')
    source, destination = source.resolve(), destination.resolve()
    for name in ('inputs', 'reports'):
        if destination.is_relative_to(source / name):
            raise ValueError('Export outside the source inputs and reports directories.')
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError('The export destination must be absent or empty; exports never overwrite data.')
    for relative in ('data', 'config', 'data/jobflow.sqlite3', 'config/settings.json', 'config/profile.json'):
        path = source / relative
        if path.is_symlink() or not path.exists():
            raise ValueError('The source workspace must contain regular configuration and database files.')
    source_lock = source / 'data/workflow.lock'
    if source_lock.is_symlink():
        raise ValueError('The source workflow lock must not be a symbolic link.')
    with source_lock.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('The source workflow is running; retry export after it finishes.') from None
        load_config(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='.owner-export-', dir=str(destination.parent)))
        try:
            _private_directory(stage / 'config')
            _private_directory(stage / 'data')
            for name in ('profile.json', 'settings.json'):
                source_file = source / 'config' / name
                if _SECRET_JSON.search(source_file.read_bytes()):
                    raise ValueError('Move credential fields out of configuration before exporting.')
                target = stage / 'config' / name
                shutil.copyfile(source_file, target)
                target.chmod(0o600)
            target_db = stage / 'data/jobflow.sqlite3'
            _private_file(target_db)
            original = sqlite3.connect((source / 'data/jobflow.sqlite3').as_uri() + '?mode=ro', uri=True)
            backup = sqlite3.connect(str(target_db))
            try:
                original.backup(backup)
                if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('The copied workspace database did not pass its integrity check.')
                counts = dict(backup.execute('SELECT kind,COUNT(*) FROM records GROUP BY kind'))
                counts['runs'] = backup.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
                resume_files = {}
                for identifier, body in backup.execute("SELECT id,body FROM records WHERE kind='application_resumes'"):
                    row = json.loads(body)
                    if (isinstance(row, dict) and _RESUME_FILE.fullmatch(identifier) and row.get('id') == identifier and
                            type(row.get('size')) is int and 0 < row['size'] <= 5 * 1024 * 1024 and
                            isinstance(row.get('sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', row['sha256'])):
                        resume_files[identifier] = {'size': row['size'], 'sha256': row['sha256']}
            finally:
                backup.close()
                original.close()
            skipped = []
            for name in ('inputs', 'reports'):
                Accounts._copy_private_tree(source / name, stage / name, name, skipped, resume_files)
            if destination.exists():
                destination.rmdir()
            os.replace(str(stage), str(destination))
            stage = None
            return {'counts': counts, 'skipped_files': skipped, 'onboarding_complete': True}
        finally:
            if stage is not None:
                shutil.rmtree(stage)

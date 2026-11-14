"""Durable single-host work queue for isolated account workspaces.

The first hosted release intentionally uses one persistent disk. The web process
and this worker share it; separate Render cron services cannot. One coalesced
followup preserves profile edits received while a prior job is already running.
"""
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .onboarding import onboarding_state

PRIORITY = {'due': 0, 'rematch': 1, 'run': 2}
LEASE_SECONDS = 1900
WORKFLOW_TIMEOUT = 1800
MAX_ATTEMPTS = 3


def _stronger(first, second):
    return max((first, second), key=lambda command: PRIORITY[command]) if second else first


class WorkQueue:
    def __init__(self, root):
        self.path = Path(root) / 'data/cloud_jobs.sqlite3'
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError('The background queue must not be a symbolic link.')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(str(self.path), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(descriptor)
        os.chmod(self.path, 0o600)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS work (
                  account_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                  command TEXT NOT NULL, requested_at REAL NOT NULL,
                  started_at REAL, finished_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
                  available_at REAL NOT NULL, lease_until REAL, claim_token TEXT,
                  next_command TEXT, message TEXT NOT NULL DEFAULT '');
            ''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(work)')}
            for name in ('claim_token', 'next_command'):
                if name not in columns:
                    db.execute('ALTER TABLE work ADD COLUMN ' + name + ' TEXT')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, account_id, command='run'):
        if command not in PRIORITY:
            raise ValueError('Unknown background command.')
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM work WHERE account_id=?', (account_id,)).fetchone()
            if row and row['status'] == 'pending':
                if PRIORITY[command] <= PRIORITY[row['command']]:
                    return False
                db.execute("UPDATE work SET command=?,available_at=?,attempts=0,message='' WHERE account_id=?",
                           (command, now, account_id))
                return True
            if row and row['status'] == 'running':
                # An hourly schedule check never creates redundant work. A
                # profile saved after a job began needs a later pass, even when
                # the running job has the same command.
                if command == 'due':
                    return False
                followup = _stronger(command, row['next_command'])
                if followup == row['next_command']:
                    return False
                db.execute('UPDATE work SET next_command=? WHERE account_id=?', (followup, account_id))
                return True
            db.execute('''INSERT INTO work(account_id,status,command,requested_at,available_at)
                VALUES(?,'pending',?,?,?) ON CONFLICT(account_id) DO UPDATE SET
                status='pending',command=excluded.command,requested_at=excluded.requested_at,
                available_at=excluded.available_at,attempts=0,started_at=NULL,
                finished_at=NULL,lease_until=NULL,claim_token=NULL,next_command=NULL,message='' ''',
                       (account_id, command, now, now))
        return True

    def status(self, account_id):
        with self.connect() as db:
            row = db.execute('''SELECT status,command,next_command,requested_at,started_at,finished_at,message
                                FROM work WHERE account_id=?''', (account_id,)).fetchone()
            return dict(row) if row else None

    def claim(self):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            expired = db.execute("SELECT * FROM work WHERE status='running' AND lease_until<?", (now,)).fetchall()
            for row in expired:
                if row['attempts'] >= MAX_ATTEMPTS and not row['next_command']:
                    db.execute("""UPDATE work SET status='failed',finished_at=?,lease_until=NULL,
                               claim_token=NULL,message='Background work was interrupted repeatedly. Try again.'
                               WHERE account_id=?""", (now, row['account_id']))
                    continue
                # A fresh claim reads the latest profile, so its stronger command
                # can absorb any work requested during the expired lease.
                command = _stronger(row['command'], row['next_command'])
                db.execute("""UPDATE work SET status='pending',command=?,available_at=?,
                           lease_until=NULL,claim_token=NULL,next_command=NULL WHERE account_id=?""",
                           (command, now, row['account_id']))
            row = db.execute("""SELECT * FROM work WHERE status='pending' AND available_at<=?
                                ORDER BY requested_at,account_id LIMIT 1""", (now,)).fetchone()
            if not row:
                return None
            claim_token = secrets.token_hex(16)
            db.execute("""UPDATE work SET status='running',started_at=?,lease_until=?,
                          attempts=attempts+1,claim_token=? WHERE account_id=?""",
                       (now, now + LEASE_SECONDS, claim_token, row['account_id']))
            return {**dict(row), 'status': 'running', 'started_at': now,
                    'lease_until': now + LEASE_SECONDS, 'claim_token': claim_token,
                    'attempts': row['attempts'] + 1}

    def finish(self, item, status, message):
        if status not in ('retry', 'done', 'partial', 'failed'):
            raise ValueError('Unknown background result.')
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("""SELECT * FROM work WHERE account_id=? AND claim_token=?
                                AND status='running'""", (item['account_id'], item['claim_token'])).fetchone()
            if not row:
                return False  # An expired or duplicate worker cannot finish a newer claim.
            if status == 'retry' and row['attempts'] < MAX_ATTEMPTS:
                command = _stronger(row['command'], row['next_command'])
                attempts = row['attempts'] if command == row['command'] else 0
                db.execute("""UPDATE work SET status='pending',command=?,attempts=?,message=?,
                           finished_at=NULL,available_at=?,lease_until=NULL,claim_token=NULL,next_command=NULL
                           WHERE account_id=?""", (command, attempts, message, now + 60, item['account_id']))
            elif row['next_command']:
                db.execute("""UPDATE work SET status='pending',command=next_command,next_command=NULL,
                           attempts=0,requested_at=?,started_at=NULL,finished_at=NULL,available_at=?,
                           lease_until=NULL,claim_token=NULL,message='A newer workspace update is queued.'
                           WHERE account_id=?""", (now, now, item['account_id']))
            else:
                db.execute('''UPDATE work SET status=?,message=?,finished_at=?,lease_until=NULL,
                           claim_token=NULL WHERE account_id=?''',
                           ('failed' if status == 'retry' else status, message, now, item['account_id']))
        return True


def _report_outcome(root):
    """Missing or malformed source evidence never establishes a healthy run."""
    health_path, runs_path = root / 'reports/source_health.json', root / 'reports/runs.json'
    if not health_path.is_file() or not runs_path.is_file():
        return 'partial', 'Search returned without complete source-health reports. Review the workspace reports.'
    health = json.loads(health_path.read_text())
    runs = json.loads(runs_path.read_text())
    if (not isinstance(health, list) or any(not isinstance(row, dict) or
            row.get('status') not in ('ok', 'manual', 'error') for row in health)
            or not isinstance(runs, list) or not runs or not isinstance(runs[0], dict)):
        return 'partial', 'Search reports could not be verified. Review source health before using these results.'
    if any(row.get('status') == 'error' for row in health) or runs[0].get('status') == 'partial':
        return 'partial', 'Search completed with source errors. Review Source health.'
    if runs[0].get('status') != 'completed':
        return 'partial', 'The latest search did not record a completed run. Review the workspace reports.'
    return 'done', 'Your workspace is up to date.'


def run_item(accounts, queue, item):
    try:
        account = next((a for a in accounts.list_accounts() if a['id'] == item['account_id']), None)
        if not account or account.get('status') not in ('active', 'invited'):
            queue.finish(item, 'failed', 'This account is unavailable.')
            return
        root = accounts.workspace(account)
        if not onboarding_state(root)['complete']:
            queue.finish(item, 'failed', 'Complete your profile before starting a search.')
            return
        if item['command'] not in PRIORITY:
            queue.finish(item, 'failed', 'This background command is unavailable.')
            return
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        queue.finish(item, 'failed', 'The workspace configuration needs attention before background work can continue.')
        return
    command = [sys.executable, '-m', 'jobflow', '--root', str(root), '--hosted',
               'run' if item['command'] == 'due' else item['command']]
    if item['command'] == 'due':
        command.append('--due')
    environment = dict(os.environ)
    environment.pop('OPENAI_API_KEY', None)
    try:
        result = subprocess.run(command, cwd=str(Path(__file__).resolve().parents[1]),
            env=environment, capture_output=True, text=True, timeout=WORKFLOW_TIMEOUT, check=False)
        if 'Another workflow process is running' in result.stderr:
            queue.finish(item, 'retry', 'Another workspace update is in progress; retrying shortly.')
        elif result.returncode:
            queue.finish(item, 'retry', 'The workflow failed. Check the workspace configuration and source health.')
        elif item['command'] == 'due' and result.stdout.strip() == 'Not due; no changes.':
            queue.finish(item, 'done', 'The next search follows your saved schedule.')
        elif item['command'] == 'rematch':
            queue.finish(item, 'done', 'Matches and preparation updated from your saved profile.')
        else:
            status, message = _report_outcome(root)
            queue.finish(item, status, message)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError):
        queue.finish(item, 'retry', 'The background workflow could not finish; retrying shortly.')


def worker_loop(accounts, queue, stop, once=False):
    next_schedule = 0
    while not stop.is_set():
        if time.monotonic() >= next_schedule:
            next_schedule = time.monotonic() + 3600
            try:
                account_rows = accounts.list_accounts()
            except (OSError, sqlite3.Error, ValueError):
                account_rows = []
                next_schedule = time.monotonic() + 60
            for account in account_rows:
                if account.get('status') != 'active':
                    continue
                try:
                    complete = onboarding_state(accounts.workspace(account))['complete']
                except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
                    # Queue one item so the user gets an actionable failed state,
                    # and continue scheduling every other account.
                    complete = True
                if complete:
                    queue.enqueue(account['id'], 'due')
        item = queue.claim()
        if item:
            run_item(accounts, queue, item)
        elif once:
            return
        else:
            stop.wait(5)

"""Small durable shared store. Agents exchange versioned JSON records via SQLite."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from copy import deepcopy

from .record_concurrency import RecordTransactions, RecordConflictError, INGESTION_KINDS


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Store(RecordTransactions):
    def __new__(cls, path):
        if cls is Store:
            from .remote import workspace_id
            if workspace_id(Path(path).parent.parent):
                from .remote_store import RemoteStore
                return RemoteStore(path)
        return super().__new__(cls)

    def __init__(self, path):
        self.root = Path(path).parent.parent
        self._init_transactions()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS records (
              kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS runs (
              id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
              finished_at TEXT, status TEXT NOT NULL, details TEXT NOT NULL);
        ''')
        self.db.commit()

    def get(self, kind, key):
        pending = self._pending_value(kind, key)
        if pending is not None:
            return deepcopy(pending['body'])
        row = self.db.execute('SELECT body FROM records WHERE kind=? AND id=?', (kind, str(key))).fetchone()
        return self._observe(kind, key, json.loads(row['body']) if row else None)

    def all(self, kind):
        rows = [json.loads(row['body']) for row in self.db.execute(
            'SELECT body FROM records WHERE kind=? ORDER BY created_at,id', (kind,))]
        self._observe_all(kind, rows)
        return self._overlay(kind, rows)

    def put(self, kind, key, value):
        if self._optimistic_depth:
            return self._stage(kind, key, value)
        if kind not in INGESTION_KINDS:
            return self._commit_checked([{'kind': kind, 'id': str(key), 'body': value}])
        now = utcnow()
        self.db.execute('''INSERT INTO records VALUES(?,?,?,?,?)
            ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at''',
            (kind, str(key), json.dumps(value, ensure_ascii=False), now, now))
        self.db.commit()

    def put_many(self, kind, values):
        if self._optimistic_depth or kind not in INGESTION_KINDS:
            return self.put_groups({kind: values})
        now = utcnow()
        rows, size = [], 0
        for value in values:
            # Matching generators and JSON encoding can be expensive. Neither
            # belongs inside the SQLite writer transaction.
            body = json.dumps(value, ensure_ascii=False)
            encoded_size = len(body.encode('utf-8'))
            if rows and (len(rows) >= 50 or size + encoded_size > 256 * 1024):
                self._write_ingestion_rows(rows)
                rows, size = [], 0
            rows.append((kind, str(value['id']), body, now, now))
            size += encoded_size
        if rows:
            self._write_ingestion_rows(rows)

    def _write_ingestion_rows(self, rows):
        with self.db:
            self.db.executemany('''INSERT INTO records VALUES(?,?,?,?,?)
                ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at''', rows)

    def put_groups(self, groups):
        """Commit an input update and its derived plan as one transaction."""
        rows = [{'kind': kind, 'id': str(value['id']), 'body': value}
                for kind, values in groups.items() for value in values]
        if self._optimistic_depth:
            for row in rows:
                self._stage(row['kind'], row['id'], row['body'])
            return
        return self._commit_checked(rows)

    def _commit_checked(self, rows, foreground=False):
        self._observe_writes(rows)
        now = utcnow()
        if foreground and self._optimistic_refresh:
            rows = rows + [{'kind': 'projection_requests', 'id': 'current',
                            'body': {'id': 'current', 'requested_at': now}}]
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for (kind, key), expected in self._observed.items():
                current = self.db.execute('SELECT body FROM records WHERE kind=? AND id=?', (kind, key)).fetchone()
                if (json.loads(current['body']) if current else None) != expected:
                    raise RecordConflictError()
            for kind, expected in self._observed_collections.items():
                current = {row[0] for row in self.db.execute('SELECT id FROM records WHERE kind=?', (kind,))}
                if current != expected:
                    raise RecordConflictError()
            for row in rows:
                self.db.execute('''INSERT INTO records VALUES(?,?,?,?,?)
                    ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at''',
                    (row['kind'], row['id'], json.dumps(row['body'], ensure_ascii=False), now, now))
        self._accept_writes(rows)

    def start_run(self):
        cursor = self.db.execute('INSERT INTO runs(started_at,status,details) VALUES(?,?,?)',
                                 (utcnow(), 'running', '{}'))
        self.db.commit()
        return cursor.lastrowid

    def finish_run(self, run_id, status, details):
        self.db.execute('UPDATE runs SET finished_at=?,status=?,details=? WHERE id=?',
                        (utcnow(), status, json.dumps(details), run_id))
        self.db.commit()

    def recent_runs(self, limit=10):
        return [dict(row) for row in self.db.execute('SELECT * FROM runs ORDER BY id DESC LIMIT ?', (limit,))]

    def close(self):
        self.db.close()

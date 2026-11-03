"""Small durable shared store. Agents exchange versioned JSON records via SQLite."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
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
        row = self.db.execute('SELECT body FROM records WHERE kind=? AND id=?', (kind, str(key))).fetchone()
        return json.loads(row['body']) if row else None

    def all(self, kind):
        return [json.loads(row['body']) for row in self.db.execute(
            'SELECT body FROM records WHERE kind=? ORDER BY created_at,id', (kind,))]

    def put(self, kind, key, value):
        now = utcnow()
        self.db.execute('''INSERT INTO records VALUES(?,?,?,?,?)
            ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at''',
            (kind, str(key), json.dumps(value, ensure_ascii=False), now, now))
        self.db.commit()

    def put_many(self, kind, values):
        now = utcnow()
        with self.db:
            for value in values:
                self.db.execute('''INSERT INTO records VALUES(?,?,?,?,?)
                    ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at''',
                    (kind, str(value['id']), json.dumps(value, ensure_ascii=False), now, now))

    def put_groups(self, groups):
        """Commit an input update and its derived plan as one transaction."""
        now = utcnow()
        with self.db:
            for kind, values in groups.items():
                for value in values:
                    self.db.execute('''INSERT INTO records VALUES(?,?,?,?,?)
                        ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at''',
                        (kind, str(value['id']), json.dumps(value, ensure_ascii=False), now, now))

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

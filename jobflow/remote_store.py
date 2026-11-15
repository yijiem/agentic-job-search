"""Supabase record store with bounded transfers and server-side projections."""
import json
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .remote import RemoteError, client_from_env, token_for, workspace_id


PAGE_SIZE = 200
# Keep each write below the hosted database's short statement timeout. Byte
# limits include row metadata and UTF-8 expansion; one larger row travels alone.
BATCH_SIZE = 50
BATCH_BYTES = 256 * 1024


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row_size(row):
    return len(json.dumps(row, ensure_ascii=False, separators=(',', ':'),
                          allow_nan=False).encode('utf-8')) + 1


def _row_batches(rows, parse_body=False):
    batch, size = [], 0
    for row in rows:
        row = dict(row)
        if parse_body and isinstance(row['body'], str):
            row['body'] = json.loads(row['body'])
        encoded_size = _row_size(row)
        if batch and (len(batch) >= BATCH_SIZE or size + encoded_size > BATCH_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(row)
        size += encoded_size
    if batch:
        yield batch


class RemoteStore:
    def __init__(self, path, client=None):
        self.root = Path(path).parent.parent
        self.workspace = workspace_id(self.root, required=True)
        self.client = client or client_from_env()
        self._cache = {}
        self._complete = set()
        self._pending = {}
        self._pending_sizes = {}
        self._pending_bytes = 0
        self._batch_depth = 0

    def _params(self, **values):
        return {'p_workspace': self.workspace, **values}

    def _write(self, name, **values):
        return self.client.rpc(name, self._params(p_token=token_for(self.root), **values))

    def get(self, kind, key):
        key = str(key)
        cached = self._cache.get(kind, {})
        if key in cached or kind in self._complete:
            return cached.get(key)
        value = self.client.rpc('jobflow_record_get', self._params(p_kind=kind, p_id=key))
        if self._batch_depth:
            self._cache.setdefault(kind, {})[key] = value
        return value

    def iter_rows(self, kind=None, mode='all', minimum=40, job_id=None):
        self.flush()
        cursor = {'p_after_kind': '', 'p_after_created': '', 'p_after_id': ''}
        revision = None
        while True:
            response = self.client.rpc('jobflow_records_page', self._params(
                p_kind=kind, p_mode=mode, p_minimum=minimum, p_job=job_id,
                p_limit=PAGE_SIZE, p_revision=revision, **cursor))
            if not isinstance(response, dict) or not isinstance(response.get('records'), list):
                raise RemoteError('The private record response is invalid.')
            if response.get('conflict'):
                raise RemoteError('The workspace changed while it was being read. Please refresh.', 409)
            revision = response['revision']
            rows = response['records']
            yield from rows
            if len(rows) < PAGE_SIZE:
                return
            last = rows[-1]
            cursor = {'p_after_kind': last['kind'], 'p_after_created': last['created_at'],
                      'p_after_id': last['id']}

    def all(self, kind):
        if kind in self._complete:
            return list(self._cache[kind].values())
        result = [row['body'] for row in self.iter_rows(kind)]
        if self._batch_depth:
            self._cache[kind] = {str(row['id']): row for row in result}
            self._complete.add(kind)
        return result

    def all_projected_jobs(self):
        return [row['body'] for row in self.iter_rows('jobs', mode='projected')]

    def put(self, kind, key, value):
        now = _now()
        row = {'kind': kind, 'id': str(key), 'body': value,
               'created_at': now, 'updated_at': now}
        if self._batch_depth:
            # Check the lease before accepting an update into a local buffer.
            token_for(self.root)
            pending_key = (kind, str(key))
            encoded_size = _row_size(row)
            replacement_size = self._pending_sizes.get(pending_key, 0)
            if self._pending and self._pending_bytes - replacement_size + encoded_size > BATCH_BYTES:
                self.flush()
                replacement_size = 0
            self._cache.setdefault(kind, {})[str(key)] = value
            self._pending[pending_key] = row
            self._pending_sizes[pending_key] = encoded_size
            self._pending_bytes += encoded_size - replacement_size
            if len(self._pending) >= BATCH_SIZE or self._pending_bytes >= BATCH_BYTES:
                self.flush()
        else:
            self._write('jobflow_records_put', p_rows=[row], p_import=False)
            if kind in self._cache:
                self._cache[kind][str(key)] = value

    def put_many(self, kind, values):
        with self.batch():
            for value in values:
                self.put(kind, value['id'], value)

    def put_groups(self, groups):
        """Keep an input update and its derived plan in one DB transaction."""
        self.flush()
        now = _now()
        rows = [{'kind': kind, 'id': str(value['id']), 'body': value,
                 'created_at': now, 'updated_at': now}
                for kind, values in groups.items() for value in values]
        if not rows:
            return
        self._write('jobflow_records_put', p_rows=rows, p_import=False)
        for row in rows:
            if row['kind'] in self._cache:
                self._cache[row['kind']][row['id']] = row['body']

    def flush(self):
        if not self._pending:
            return
        rows = list(self._pending.values())
        self._write('jobflow_records_put', p_rows=rows, p_import=False)
        self._pending.clear()
        self._pending_sizes.clear()
        self._pending_bytes = 0

    @contextmanager
    def batch(self, preload=()):
        self._batch_depth += 1
        try:
            for kind in preload:
                self.all(kind)
            yield self
        finally:
            try:
                # CLI workflows already preserve validated progress on errors.
                # This retains those updates, while the server fences lost leases.
                self.flush()
            finally:
                self._batch_depth -= 1
                if not self._batch_depth:
                    self._cache.clear()
                    self._complete.clear()

    def import_records(self, rows):
        """Import exact record timestamps; each bounded request is idempotent."""
        self.flush()
        for batch in _row_batches(rows, parse_body=True):
            self._write('jobflow_records_put', p_rows=batch, p_import=True)
        self._cache.clear()
        self._complete.clear()

    def import_runs(self, rows):
        self.flush()
        for batch in _row_batches(rows):
            self._write('jobflow_runs_import', p_rows=batch)

    def stats(self):
        self.flush()
        return self.client.rpc('jobflow_workspace_stats', self._params())

    def verify_import(self, records=(), runs=()):
        """Compare original data in Postgres without downloading it a second time."""
        result = {'records_mismatched': 0, 'runs_mismatched': 0}
        for name, values in (('p_records', records), ('p_runs', runs)):
            for batch in _row_batches(values, parse_body=name == 'p_records'):
                checked = self.client.rpc('jobflow_import_verify', self._params(**{name: batch}))
                for key in result:
                    result[key] += checked[key]
        return result

    def start_run(self):
        self.flush()
        return self._write('jobflow_run_start', p_started_at=_now())

    def finish_run(self, run_id, status, details):
        self.flush()
        self._write('jobflow_run_finish', p_id=run_id, p_finished_at=_now(),
                    p_status=status, p_details=json.dumps(details))

    def recent_runs(self, limit=10):
        self.flush()
        return self.client.rpc('jobflow_runs_recent', self._params(p_limit=limit))

    def close(self):
        self.flush()
        self._cache.clear()
        self._complete.clear()


def read_records(root, job_mode='matching', minimum=40, job_id=None):
    store = RemoteStore(Path(root) / 'data/jobflow.sqlite3')
    records = defaultdict(list)
    cursor = {'p_after_kind': '', 'p_after_created': '', 'p_after_id': ''}
    revision, count, runs = None, 0, []
    while True:
        page = store.client.rpc('jobflow_records_page', store._params(
            p_kind=None, p_mode=job_mode, p_minimum=minimum, p_job=job_id,
            p_limit=PAGE_SIZE, p_revision=revision, **cursor))
        if page.get('conflict'):
            raise RemoteError('The workspace changed while it was being read. Please refresh.', 409)
        revision, count = page['revision'], page['count']
        if not cursor['p_after_kind']:
            runs = page['runs']
        rows = page['records']
        for row in rows:
            records[row['kind']].append(row['body'])
        if len(rows) < PAGE_SIZE:
            break
        last = rows[-1]
        cursor = {'p_after_kind': last['kind'], 'p_after_created': last['created_at'],
                  'p_after_id': last['id']}
    for run in runs:
        try:
            run['details'] = json.loads(run['details'])
        except (TypeError, ValueError):
            pass
    return records, runs, count

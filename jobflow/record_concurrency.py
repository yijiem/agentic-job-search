"""Short record transactions shared by the local and hosted stores.

Background ingestion owns jobs; interactive commands own only the explicitly
listed record families. Reads retain independent values because callers often
edit the dictionaries returned by the store in place.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path


_active_store = ContextVar('jobflow_optimistic_store', default=None)


def optimistic_store(root):
    store = _active_store.get()
    return store if store is not None and Path(root).resolve() == store.root.resolve() else None


FOREGROUND_KINDS = frozenset((
    'applications', 'interviews', 'tasks', 'activities', 'plans', 'coaching',
    'application_batches', 'application_resumes', 'resume_profile',
    'resume_drafts', 'resume_reviews',
))
INGESTION_KINDS = frozenset(('jobs', 'source_health', 'schedule'))


class RecordConflictError(RuntimeError):
    """No records in this transaction changed; its inputs must be reread."""

    def __init__(self):
        super().__init__('This record changed while it was being saved. Read its latest version and retry.')


class RecordTransactions:
    def _init_transactions(self):
        self._observed = {}
        self._observed_collections = {}
        self._optimistic_depth = 0
        self._optimistic_rows = {}
        self._optimistic_files = {}
        self._optimistic_refresh = False

    def request_refresh(self):
        if not self._optimistic_depth:
            raise ValueError('A projection refresh must accompany an interactive transaction.')
        self._optimistic_refresh = True

    def stage_file(self, metadata):
        if not self._optimistic_depth:
            raise ValueError('Private file publication requires an interactive transaction.')
        self._optimistic_files[metadata['path']] = deepcopy(metadata)

    def _observe(self, kind, key, value):
        if kind not in INGESTION_KINDS:
            self._observed.setdefault((kind, str(key)), deepcopy(value))
        return value

    def _observe_all(self, kind, values):
        if kind not in INGESTION_KINDS:
            self._observed_collections.setdefault(kind, {str(row['id']) for row in values})
            for row in values:
                self._observe(kind, row['id'], row)
        return values

    def _pending_value(self, kind, key):
        return self._optimistic_rows.get((kind, str(key)))

    def _overlay(self, kind, values):
        result = {str(value['id']): value for value in values}
        for (row_kind, key), row in self._optimistic_rows.items():
            if row_kind == kind:
                result[key] = deepcopy(row['body'])
        return list(result.values())

    def _observe_writes(self, rows):
        for row in rows:
            key = (row['kind'], row['id'])
            if row['kind'] not in INGESTION_KINDS and key not in self._observed:
                self.get(*key)

    def _stage(self, kind, key, value):
        if kind not in FOREGROUND_KINDS:
            raise ValueError('This record belongs in the background workflow.')
        row = {'kind': kind, 'id': str(key), 'body': deepcopy(value)}
        self._observe_writes([row])
        self._optimistic_rows[kind, str(key)] = row

    def _checks(self):
        expected = [{'kind': kind, 'id': key, 'body': deepcopy(value)}
                    for (kind, key), value in self._observed.items()]
        collections = [{'kind': kind, 'ids': sorted(keys)}
                       for kind, keys in self._observed_collections.items()]
        return expected, collections

    def _accept_writes(self, rows):
        for row in rows:
            kind, key = row['kind'], row['id']
            if kind not in INGESTION_KINDS:
                self._observed[kind, key] = deepcopy(row['body'])
                if kind in self._observed_collections:
                    self._observed_collections[kind].add(key)

    def _reset_transaction_reads(self):
        self._observed.clear()
        self._observed_collections.clear()

    @contextmanager
    def optimistic(self):
        """Buffer an allowlisted foreground command and commit it atomically.

        Callers must publish successful output only after this context exits.
        A conflict or validation error publishes no record writes. Immutable
        file staging, if needed, must use its own publication safeguards.
        """
        outer = self._optimistic_depth == 0
        if outer:
            flush = getattr(self, 'flush', None)
            if flush:
                flush()
            self._reset_transaction_reads()
            self._optimistic_rows.clear()
            self._optimistic_files.clear()
            self._optimistic_refresh = False
        self._optimistic_depth += 1
        context_token = _active_store.set(self)
        try:
            yield self
            if outer and (self._optimistic_rows or self._optimistic_files or self._optimistic_refresh):
                rows = list(self._optimistic_rows.values())
                self._commit_checked(rows, foreground=True)
        finally:
            _active_store.reset(context_token)
            self._optimistic_depth -= 1
            if outer:
                self._optimistic_rows.clear()
                self._optimistic_files.clear()
                self._optimistic_refresh = False
                self._reset_transaction_reads()

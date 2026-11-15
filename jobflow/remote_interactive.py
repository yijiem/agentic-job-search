"""Bound interactive work to small projections; full exports run in the worker."""
from contextlib import nullcontext


class InteractiveStore:
    def __init__(self, store):
        self.store = store

    def __getattr__(self, name):
        return getattr(self.store, name)

    def all(self, kind):
        if kind == 'jobs':
            return self.store.all_projected_jobs()
        return self.store.all(kind)

    def put(self, kind, key, value):
        if kind == 'jobs':
            raise ValueError('Job ingestion must run in the background worker.')
        return self.store.put(kind, key, value)

    def put_many(self, kind, values):
        if kind == 'jobs':
            raise ValueError('Job ingestion must run in the background worker.')
        return self.store.put_many(kind, values)

    def put_groups(self, groups):
        if 'jobs' in groups:
            raise ValueError('Job ingestion must run in the background worker.')
        return self.store.put_groups(groups)


def execution_context(root):
    from .remote import workspace_id, RemoteLease
    return RemoteLease(root) if workspace_id(root) else nullcontext()

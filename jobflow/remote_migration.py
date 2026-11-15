"""Verified, resumable adoption of a local owner workspace into Supabase."""
import contextvars
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import contextmanager
from pathlib import Path

from .storage import utcnow


def _sha(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


@contextmanager
def _database(path):
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _fingerprint(snapshot):
    files = {}
    for section in ('config', 'inputs', 'reports'):
        for path in sorted((snapshot / section).rglob('*')):
            if path.is_symlink():
                raise ValueError('Migration files cannot be symbolic links.')
            if path.is_file():
                files[path.relative_to(snapshot).as_posix()] = {'sha256': _sha(path), 'size': path.stat().st_size}
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode())
    with _database(snapshot / 'data/jobflow.sqlite3') as db:
        for table, ordering in (('records', 'kind,id'), ('runs', 'id')):
            digest.update(table.encode())
            for row in db.execute('SELECT * FROM ' + table + ' ORDER BY ' + ordering):
                digest.update(json.dumps(dict(row), sort_keys=True, ensure_ascii=False,
                                         separators=(',', ':')).encode('utf-8'))
                digest.update(b'\n')
    return digest.hexdigest(), files


def _batches(cursor, limit=100):
    while True:
        rows = cursor.fetchmany(limit)
        if not rows:
            return
        yield [dict(row) for row in rows]


def _upload_file(snapshot, root, relative, expected):
    from .remote_files import persist_file, ensure_file
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copyfile(snapshot / relative, destination)
    destination.chmod(0o600)
    metadata = persist_file(root, destination)
    if metadata['sha256'] != expected['sha256'] or metadata['size'] != expected['size']:
        raise ValueError('A migrated file did not retain its original bytes.')
    # Force an independent round trip. Large text objects are stored compressed;
    # their original SHA-256 is checked after bounded decompression.
    destination.unlink()
    ensure_file(root, relative, metadata=metadata)
    if _sha(destination) != expected['sha256']:
        raise ValueError('A migrated file failed remote integrity verification.')
    # Keep configuration and small onboarding metadata in the disposable cache.
    if not relative.startswith('config/'):
        destination.unlink()
    return metadata['stored_size']


def _upload_file_retry(snapshot, root, relative, expected):
    """Retry only idempotent transfers after narrowly classified transient errors."""
    from .remote import RemoteError
    for attempt in range(3):
        try:
            return _upload_file(snapshot, root, relative, expected)
        except RemoteError as error:
            transient = (error.status in (408, 429, 500, 502, 503, 504) or
                         (error.status is None and str(error) ==
                          'The private service could not be reached. Please retry.'))
            if not transient or attempt == 2:
                raise
            time.sleep(2 ** attempt)


def _upload_files(snapshot, root, files, workers=4, progress=None):
    """Keep only a bounded set in flight, and report failures before shutdown.

    Submitting every file up front hides an early exception while the executor
    drains hundreds of later uploads. Bounded submission limits that wait to the
    other active requests and leaves a resumable manifest after interruption.
    """
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, 8)))
    items = iter(files.items())
    pending = {}
    stored_bytes, completed = 0, 0

    def submit_next():
        item = next(items, None)
        if item is None:
            return False
        relative, expected = item
        future = pool.submit(contextvars.copy_context().run, _upload_file_retry,
                             snapshot, root, relative, expected)
        pending[future] = relative
        return True

    try:
        for unused in range(max(1, min(workers, 8))):
            if not submit_next():
                break
        while pending:
            done, unused = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                relative = pending.pop(future)
                try:
                    stored_bytes += future.result()
                except Exception as error:
                    if progress:
                        progress({'phase': 'files', 'status': 'failed', 'path': relative,
                                  'completed': completed, 'total': len(files),
                                  'error': type(error).__name__ + ': ' + str(error)})
                    raise
                completed += 1
                if progress and (completed == 1 or completed % 25 == 0 or completed == len(files)):
                    progress({'phase': 'files', 'completed': completed, 'total': len(files)})
                submit_next()
        return stored_bytes
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)


def _adopt_snapshot(accounts, account, snapshot, export_summary, workers=4, progress=None):
    from .remote import RemoteLease, client_from_env, token_for, workspace_id
    from .remote_files import persist_file, ensure_file, _cache_write
    from .remote_store import RemoteStore
    source_id, files = _fingerprint(snapshot)
    root = Path(accounts.workspace(account))
    identifier = workspace_id(root, required=True)
    # begin_adoption binds the content fingerprint and rejects live/populated
    # accounts or a different migration. Pending adoption cannot authenticate.
    fresh = accounts.begin_adoption(account, source_id)
    if not fresh:
        receipt_path = ensure_file(root, 'data/cloud-migration.json')
        if not receipt_path.is_file():
            raise ValueError('The completed migration receipt is unavailable.')
        receipt = json.loads(receipt_path.read_text())
        if receipt.get('source_id') != source_id:
            raise ValueError('This account belongs to a different migration.')
        return {'account': accounts.lookup_email(account['email']), 'adopted': False,
                'already_adopted': True, 'workspace_id': identifier,
                'counts': receipt['counts'], 'files': receipt['files'],
                'skipped_files': receipt.get('skipped_files', [])}
    client_from_env().rpc('jobflow_workspace_create', {'p_workspace': identifier})
    with RemoteLease(root):
        store = RemoteStore(root / 'data/jobflow.sqlite3')
        stored_bytes = 0
        try:
            stored_bytes = _upload_files(snapshot, root, files, workers, progress)
            with _database(snapshot / 'data/jobflow.sqlite3') as db:
                for batch in _batches(db.execute('SELECT * FROM records ORDER BY kind,id')):
                    store.import_records(batch)
                    result = store.verify_import(records=batch)
                    if result.get('records_mismatched') or result.get('runs_mismatched'):
                        raise ValueError('Migrated records failed verification.')
                for batch in _batches(db.execute('SELECT * FROM runs ORDER BY id')):
                    store.import_runs(batch)
                    result = store.verify_import(runs=batch)
                    if result.get('records_mismatched') or result.get('runs_mismatched'):
                        raise ValueError('Migrated run history failed verification.')
            stats = store.stats()
            expected = dict(export_summary['counts'])
            run_count = expected.pop('runs', 0)
            if stats['records'] != expected or stats['runs'] != run_count:
                raise ValueError('Migrated record counts do not match the owner snapshot.')
            adopted_at = utcnow()
            marker = {'completed': True, 'migrated': True,
                      'source': 'existing_workspace', 'completed_at': adopted_at}
            for relative, value in (('config/onboarding.json', marker),
                                    ('config/hosted.json', {'version': 2})):
                path = root / relative
                _cache_write(path, (json.dumps(value, indent=2) + '\n').encode())
                persist_file(root, path)
            receipt = {'version': 2, 'account_id': account['id'], 'workspace_id': identifier,
                       'source_id': source_id, 'adopted_at': adopted_at,
                       'counts': export_summary['counts'], 'files': len(files),
                       'stored_bytes': stored_bytes, 'onboarding_complete': True,
                       'verification': 'every record, run, and file round-trip verified',
                       'skipped_files': export_summary.get('skipped_files', [])}
            receipt_path = root / 'data/cloud-migration.json'
            _cache_write(receipt_path, (json.dumps(receipt, indent=2) + '\n').encode())
            persist_file(root, receipt_path)
            accounts._record_adoption(account, receipt)
        finally:
            store.close()
    return {'account': accounts.lookup_email(account['email']), 'adopted': True,
            'already_adopted': False, 'workspace_id': identifier,
            'counts': receipt['counts'], 'files': receipt['files'],
            'stored_bytes': stored_bytes, 'skipped_files': receipt['skipped_files']}


def adopt_owner(accounts, email, source, workers=4, progress=None):
    """Import a durable frozen copy, without changing the source workspace.

    The transfer snapshot survives process errors and local source edits. Reusing
    the same account/source command resumes those exact bytes; selecting another
    source is rejected. The checkpoint remains as a private recovery artifact.
    """
    from .cloud_accounts import export_workspace
    account = accounts.lookup_email(email)
    if account is None or account.get('status') not in ('invited', 'active'):
        raise ValueError('Invite this email before adopting an existing workspace.')
    source = Path(source).expanduser()
    if source.is_symlink():
        raise ValueError('The owner source cannot be a symbolic link.')
    source = source.resolve()
    transfers = Path(accounts.data) / 'owner-transfers'
    if transfers.is_symlink():
        raise ValueError('Owner transfer snapshots cannot use symbolic links.')
    transfers.mkdir(mode=0o700, parents=True, exist_ok=True)
    identifier = account['id']
    if not isinstance(identifier, str) or Path(identifier).name != identifier or identifier in ('.', '..'):
        raise ValueError('Invalid owner account identifier.')
    checkpoint = transfers / identifier
    if checkpoint.is_symlink():
        raise ValueError('The owner transfer snapshot cannot be a symbolic link.')
    if not checkpoint.exists():
        with tempfile.TemporaryDirectory(prefix='.owner-transfer-', dir=str(transfers)) as temporary:
            stage = Path(temporary) / 'checkpoint'
            stage.mkdir(mode=0o700)
            summary = export_workspace(source, stage / 'workspace')
            metadata = {'source': str(source), 'summary': summary}
            metadata_path = stage / 'source.json'
            metadata_path.write_text(json.dumps(metadata, indent=2) + '\n')
            metadata_path.chmod(0o600)
            # Never merge with or replace an existing account's frozen snapshot.
            os.rename(stage, checkpoint)
    metadata_path = checkpoint / 'source.json'
    if metadata_path.is_symlink() or (checkpoint / 'workspace').is_symlink():
        raise ValueError('Owner transfer snapshots cannot use symbolic links.')
    metadata = json.loads(metadata_path.read_text())
    if metadata.get('source') != str(source):
        raise ValueError('This account already has a frozen transfer from a different source. Resume that original transfer.')
    if progress:
        progress({'phase': 'snapshot', 'path': str(checkpoint / 'workspace'), 'status': 'ready'})
    return _adopt_snapshot(accounts, account, checkpoint / 'workspace', metadata['summary'], workers, progress)

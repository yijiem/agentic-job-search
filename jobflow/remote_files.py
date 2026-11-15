"""Private durable files with a disposable local cache.

Storage objects are immutable and addressed by their content hash. A fenced
Postgres manifest is the authority; a successful local write is not durability.
"""
import gzip
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath

BUCKET = 'jobflow-private'
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_STORED_BYTES = 50 * 1024 * 1024
CONFIG_FILES = ('config/settings.json', 'config/profile.json',
                'config/onboarding.json', 'config/hosted.json')


def is_remote_root(root):
    from .remote import workspace_id
    return workspace_id(root, required=False) is not None


def _context(root):
    from .remote import client_from_env, workspace_id
    identifier = workspace_id(root, required=False)
    return (client_from_env(), identifier) if identifier else (None, None)


def _relative(value):
    value = str(value)
    path = PurePosixPath(value)
    if (not value or '\\' in value or path.is_absolute() or
            any(part in ('', '.', '..') for part in value.split('/')) or
            any(ord(char) < 32 for char in value) or len(value) > 2000):
        raise ValueError('Invalid private file path.')
    allowed = (value in CONFIG_FILES or value == 'data/cloud-migration.json' or
               value.startswith(('inputs/', 'reports/')))
    if not allowed or value.startswith('inputs/web/') or any(part.startswith('.') for part in path.parts):
        raise ValueError('This file is not durable workspace content.')
    return value


def _path(root, relative):
    root = Path(root).absolute()
    relative = _relative(relative)
    if root.is_symlink():
        raise ValueError('Private workspace storage cannot use symbolic links.')
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Private workspace files cannot use symbolic links.')
    return current


def _sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.remote-', dir=str(path.parent))
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def file_manifest(root, prefix=''):
    client, identifier = _context(root)
    if not client:
        return []
    rows, after = [], ''
    while True:
        page = client.rpc('jobflow_file_list', {'p_workspace': identifier,
                         'p_prefix': prefix, 'p_after': after, 'p_limit': 500}) or []
        rows.extend(page)
        if len(page) < 500:
            return rows
        next_after = page[-1]['path']
        if next_after <= after:
            raise ValueError('Invalid private file manifest pagination.')
        after = next_after


def ensure_file(root, relative, metadata=None):
    """Materialize current authoritative bytes, fetching only a missing version."""
    path = _path(root, relative)
    client, identifier = _context(root)
    if not client:
        return path
    row = metadata if metadata is not None else client.rpc('jobflow_file_get',
                    {'p_workspace': identifier, 'p_path': str(relative)})
    if row is None:
        # A leftover failed local write must never masquerade as durable state.
        if path.is_file():
            path.unlink()
        return path
    if row.get('path') != str(relative) or not re.fullmatch(r'[a-f0-9]{64}', row.get('sha256', '')):
        raise ValueError('Invalid private file manifest.')
    size = row.get('size')
    if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
        raise ValueError('Private file exceeds the supported size.')
    if path.is_file() and path.stat().st_size == size and _sha(path) == row['sha256']:
        return path
    expected_key = identifier + '/' + row['sha256'] + ('.gz' if row.get('encoding') == 'gzip' else '')
    if row.get('object_key') != expected_key or row.get('encoding') not in ('identity', 'gzip'):
        raise ValueError('Invalid private object reference.')
    encoded = client.storage_download(BUCKET, expected_key)
    if row['encoding'] == 'gzip':
        # Bound decompression before allocating arbitrary untrusted content.
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(encoded)) as stream:
            content = stream.read(size + 1)
    else:
        content = encoded
    if len(content) != size or hashlib.sha256(content).hexdigest() != row['sha256']:
        raise ValueError('Private file integrity verification failed.')
    _cache_write(path, content)
    return path


def persist_file(root, path):
    """Upload bytes first, then atomically publish their fenced manifest entry."""
    from .remote import token_for
    client, identifier = _context(root)
    if not client:
        return None
    root = Path(root).absolute()
    path = Path(path).absolute()
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        raise ValueError('Private file is outside its workspace.') from None
    path = _path(root, relative)
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('Private file is missing or exceeds the supported size.')
    token = token_for(root)
    if not token:
        raise ValueError('A workspace lease is required to save private files.')
    digest, size = _sha(path), path.stat().st_size
    previous = client.rpc('jobflow_file_get', {'p_workspace': identifier, 'p_path': relative})
    if previous and previous.get('sha256') == digest and previous.get('size') == size:
        return previous
    content = path.read_bytes()
    encoding = 'identity'
    if len(content) > 1024 and path.suffix.lower() in ('.json', '.md', '.txt', '.csv', '.html', '.svg', '.xml'):
        compressed = gzip.compress(content, compresslevel=6, mtime=0)
        if len(compressed) < len(content):
            content, encoding = compressed, 'gzip'
    object_key = identifier + '/' + digest + ('.gz' if encoding == 'gzip' else '')
    if len(content) > MAX_STORED_BYTES:
        raise ValueError('Private file exceeds the free storage upload limit after compression.')
    client.rpc('jobflow_file_stage', {'p_workspace': identifier, 'p_token': token,
                                     'p_object_key': object_key})
    client.storage_upload(BUCKET, object_key, content, content_type='application/octet-stream')
    metadata = {'path': relative, 'object_key': object_key, 'sha256': digest,
                'size': size, 'stored_size': len(content), 'encoding': encoding}
    return client.rpc('jobflow_file_put', {'p_workspace': identifier, 'p_token': token, 'p_file': metadata})


def _find_root(path):
    path = Path(path).absolute()
    for parent in path.parents:
        if (parent / 'config/remote.json').is_file():
            return parent if is_remote_root(parent) else None
    return None


def ensure_path(path):
    root = _find_root(path)
    return ensure_file(root, Path(path).absolute().relative_to(root).as_posix()) if root else Path(path)


def persist_path(path):
    root = _find_root(path)
    return persist_file(root, path) if root else None


def hydrate_workspace(root):
    """Fetch small configuration/metadata, leaving resume and report bodies lazy."""
    if not is_remote_root(root):
        return
    for relative in CONFIG_FILES:
        ensure_file(root, relative)
    hydrate_upload_metadata(root)


def hydrate_upload_metadata(root):
    if not is_remote_root(root):
        return
    for row in file_manifest(root, 'inputs/resumes/'):
        if re.fullmatch(r'inputs/resumes/[a-f0-9]{32}\.json', row['path']):
            ensure_file(root, row['path'], metadata=row)


def prune_unreferenced(root, maximum=500):
    """Retire obsolete versions after a day, preserving every current file path.

    The claim RPC fences the lease and tombstones each object so a concurrent or
    restarted writer cannot publish a reference while deletion is in flight.
    Failed deletions remain claimed and are retried by the next worker run.
    """
    from .remote import token_for
    client, identifier = _context(root)
    if not client:
        return 0
    token = token_for(root)
    if not token:
        raise ValueError('A workspace lease is required for private file cleanup.')
    removed = 0
    while removed < maximum:
        keys = client.rpc('jobflow_file_gc_claim', {'p_workspace': identifier,
                          'p_token': token, 'p_limit': min(100, maximum - removed)}) or []
        if not keys:
            break
        if any(not re.fullmatch(re.escape(identifier) + r'/[a-f0-9]{64}(?:\.gz)?', key) for key in keys):
            raise ValueError('Private object cleanup returned an invalid reference.')
        client.request('DELETE', '/storage/v1/object/' + BUCKET, payload={'prefixes': keys})
        client.rpc('jobflow_file_gc_finish', {'p_workspace': identifier,
                   'p_token': token, 'p_keys': keys})
        removed += len(keys)
    return removed

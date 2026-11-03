"""Validated, round-trippable JSON/CSV tracking imports."""
import csv
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from .reports import atomic_write

STAGES = ('saved', 'applied', 'recruiter', 'technical', 'onsite', 'offer', 'rejected', 'withdrawn')
STATUSES = ('todo', 'doing', 'done', 'skipped', 'unscheduled')
KINDS = ('applications', 'interviews', 'tasks', 'contacts', 'jobs', 'research')


def valid_url(value):
    parsed = urlparse(value)
    return parsed.scheme in ('https', 'http') and bool(parsed.netloc)


def validate_record(kind, row, store):
    row = dict(row)
    if kind == 'jobs':
        for key in ('company', 'title', 'url', 'description', 'location'):
            if not row.get(key):
                raise ValueError('Job import requires ' + key)
        if not valid_url(row['url']):
            raise ValueError('Job URL must be an HTTP(S) URL.')
        row.setdefault('source_id', 'manual-import')
        row.setdefault('external_id', '')
        row.setdefault('compensation', {'tc_status': 'unknown'})
        row.setdefault('remote', False)
        if isinstance(row['remote'], str) and row['remote'].lower() in ('true', 'false'):
            row['remote'] = row['remote'].lower() == 'true'
        if not isinstance(row['remote'], bool):
            raise ValueError('Job remote must be a boolean (true or false).')
        if not isinstance(row['compensation'], dict):
            raise ValueError('Job compensation must be an object; use compensation_json in CSV.')
        for field in ('base_min', 'base_max', 'tc_min', 'tc_max'):
            value = row['compensation'].get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
                raise ValueError('Compensation amounts must be nonnegative numbers or null.')
        return row
    if not row.get('id'):
        if kind == 'applications':
            row['id'] = row.get('job_id', '')
        else:
            row['id'] = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()[:16]
    if not row['id'] or not isinstance(row['id'], str):
        raise ValueError('Every record needs a nonempty string ID.')
    # IDs become local report filenames.
    if any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in row['id']):
        raise ValueError('Record IDs may contain only letters, digits, underscores, and hyphens.')
    if kind in ('applications', 'interviews'):
        job = store.get('jobs', row.get('job_id', ''))
        if not job:
            raise ValueError('Unknown job_id: ' + str(row.get('job_id')))
        row['company'], row['title'] = job['company'], job['title']
    if kind == 'applications':
        row['id'] = row['job_id']
        if row.get('stage') not in STAGES:
            raise ValueError('Application stage must be one of: ' + ', '.join(STAGES))
    elif kind == 'interviews':
        value = datetime.fromisoformat(row.get('starts_at', '').replace('Z', '+00:00'))
        if value.tzinfo is None:
            raise ValueError('Interview starts_at needs an explicit timezone offset.')
        if not row.get('round'):
            raise ValueError('Interview round is required.')
    elif kind == 'tasks':
        if row.get('status') not in STATUSES:
            raise ValueError('Invalid task status.')
        if row.get('due_date'):
            date.fromisoformat(row['due_date'])
        elif row['status'] != 'unscheduled':
            raise ValueError('Tasks need a due_date unless unscheduled.')
        row['minutes'] = int(row.get('minutes', 0))
        if row['minutes'] <= 0 or not row.get('title'):
            raise ValueError('Tasks need positive minutes and a title.')
    elif kind == 'contacts':
        if not row.get('name') or not row.get('company'):
            raise ValueError('Contacts need name and company.')
        if row.get('relationship', 'public') not in ('direct', 'mutual', 'public'):
            raise ValueError('Contact relationship must be direct, mutual, or public.')
        row.setdefault('relationship', 'public')
        if row['relationship'] == 'mutual' and not row.get('via'):
            raise ValueError('Mutual contacts need the named introducer in via.')
        if not any(valid_url(row.get(k, '')) for k in ('profile_url', 'evidence_url')):
            raise ValueError('Contacts need a public professional profile or evidence URL.')
    elif kind == 'research':
        if not row.get('text') or not isinstance(row.get('sources'), list) or not row['sources']:
            raise ValueError('Research needs text and a nonempty list of source objects.')
        if not all(valid_url(s.get('url', '')) for s in row['sources']):
            raise ValueError('Research sources need HTTP(S) URLs.')
        row.setdefault('mode', 'imported_research_requires_review')
    return row


def read_records(path):
    path = Path(path)
    if path.suffix.lower() == '.csv':
        with path.open(newline='', encoding='utf-8-sig') as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            if None in row:
                raise ValueError('CSV row contains more values than its header; quote fields containing commas.')
            for key in list(row):
                if key.endswith('_json'):
                    row[key[:-5]] = json.loads(row.pop(key) or 'null')
        # Rebuild to preserve unexposed metadata during CSV round trips.
        result = []
        for row in rows:
            extras = json.loads(row.pop('_extra', '') or '{}')
            if not isinstance(extras, dict):
                raise ValueError('CSV _extra metadata must be a JSON object.')
            extras.update(row)
            result.append(extras)
        return result
    rows = json.loads(path.read_text())
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError('Import must contain an object or list of objects.')
    return rows


CSV_COLUMNS = {
    'applications': ['id', 'job_id', 'company', 'title', 'stage', 'notes'],
    'interviews': ['id', 'job_id', 'company', 'round', 'starts_at', 'notes'],
    'tasks': ['id', 'due_date', 'title', 'category', 'company', 'minutes', 'status', 'source_url', 'notes'],
    'contacts': ['id', 'name', 'company', 'title', 'profile_url', 'relationship', 'via', 'team', 'evidence_url', 'notes'],
}


def export_tracking(store, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for kind, columns in CSV_COLUMNS.items():
        records = store.all(kind)
        atomic_write(directory / (kind + '.json'), json.dumps(records, indent=2, ensure_ascii=False) + '\n')
        with (directory / (kind + '.csv')).open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=columns + ['_extra'])
            writer.writeheader()
            for record in records:
                row = {k: record.get(k, '') for k in columns}
                row['_extra'] = json.dumps({k: v for k, v in record.items() if k not in columns})
                writer.writerow(row)

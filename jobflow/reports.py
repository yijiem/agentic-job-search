"""Reviewable local exports. Machine state remains in SQLite."""
import json
import os
from pathlib import Path

from .storage import utcnow
from .workflow import ranked_jobs


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(content, encoding='utf-8')
    os.replace(temporary, path)


def cell(value):
    return str(value or '').replace('|', '\\|').replace('\n', ' ')


def write_reports(root, store, config):
    output = Path(root) / 'reports'
    jobs = ranked_jobs(store, config)
    lines = ['# Job search review', '', 'Updated: ' + utcnow(), '',
        'Target: approximately $500,000 annual USD total compensation. '
        'Posted base pay is not total compensation; unverified TC stays visible.', '',
        '## Source coverage', '', '| Source | State | Detail |', '|---|---|---|']
    for source in store.all('source_health'):
        lines.append('| %s | %s | %s |' % (cell(source['id']), source['status'], cell(source.get('message'))))
    lines += ['', '## Ranked opportunities', '',
              '| Job ID | Company / role | Score | Location | TC evidence | Availability |', '|---|---|---:|---|---|---|']
    for job in jobs[:100]:
        lines.append('| `%s` | [%s — %s](%s) | %s | %s | %s | %s |' % (
            job['id'], cell(job['company']), cell(job['title']), job['url'], job['score'],
            cell(job['location']), cell(job.get('compensation_status')), job['availability']))
    lines += ['', 'Scores are transparent keyword heuristics, not a probability of interview or offer. '
              'See jobs.json for match reasons, gaps, description, compensation, and timestamps.', '',
              '## Review queue', '']
    for packet in store.all('packets'):
        key = packet['id']
        lines.append('- [%s — %s](packets/%s.md) · [Resume draft](resumes/%s.md)' % (
            packet.get('company', ''), packet.get('title', key), key, key))
        atomic_write(output / 'packets' / (key + '.md'), packet['markdown'])
        atomic_write(output / 'resumes' / (key + '.md'), packet.get('resume_markdown', ''))
    lines += ['', '[Preparation plan](preparation.md) · [Applications](applications.json) · '
              '[Interview schedule](interviews.json) · [Tasks](tasks.json) · [Research notes](research.md)', '',
              'Edit tracking through the CLI or export → edit → import. Report files are regenerated.']
    atomic_write(output / 'review.md', '\n'.join(lines) + '\n')
    plan = store.get('plans', 'current')
    if plan:
        atomic_write(output / 'preparation.md', plan['markdown'])
    for kind in ('jobs', 'applications', 'interviews', 'tasks', 'source_health', 'contacts', 'research', 'candidates'):
        atomic_write(output / (kind + '.json'), json.dumps(store.all(kind), indent=2, ensure_ascii=False) + '\n')
    research_lines = ['# Research notes', '', 'Public leads and interview anecdotes require review; they do not establish personal connections.', '']
    for note in store.all('research'):
        research_lines += ['## ' + note.get('role', 'Imported note'), '', note.get('retrieved_at', ''), '', note['text'], '']
        research_lines += ['- [%s](%s)' % (source.get('title', source['url']), source['url']) for source in note.get('sources', [])]
        research_lines.append('')
    if not store.all('research'):
        research_lines.append('No live research recorded. Public ATS jobs and generic preparation work without a research provider.')
    atomic_write(output / 'research.md', '\n'.join(research_lines) + '\n')
    atomic_write(output / 'runs.json', json.dumps(store.recent_runs(), indent=2) + '\n')
    return output / 'review.md'

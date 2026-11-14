"""Reviewable local exports. Machine state remains in SQLite."""
import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .storage import utcnow
from .coaching import build_coaching
from .workflow import ranked_jobs
from .salary_references import reference_index, salary_reference


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(content, encoding='utf-8')
    os.replace(temporary, path)


def cell(value):
    return str(value or '').replace('|', '\\|').replace('\n', ' ')


def company_paths(companies):
    """Readable filenames that cannot escape the company output folder."""
    slugs = {name: re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'company'
             for name in companies}
    counts = Counter(slugs.values())
    return {name: slug + ('-' + hashlib.sha256(name.encode()).hexdigest()[:12]
                         if counts[slug] > 1 else '') + '.md'
            for name, slug in slugs.items()}


def job_order(job):
    return (-job.get('score', 0), job['title'].casefold(), job['id'])


def seen_date(value, local_zone):
    if not value:
        return 'Unknown'
    try:
        observed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return observed.astimezone(local_zone).date().isoformat() if observed.tzinfo else value[:10]
    except ValueError:
        return str(value)


def job_table(jobs, packet_ids, packet_prefix, local_zone):
    if not jobs:
        return ['No listings in this section.']
    lines = ['| Job ID | Role | Score | Location | TC evidence | Levels.fyi salary reference | Availability | Last seen | Review |',
             '|---|---|---:|---|---|---|---|---|---|']
    for job in sorted(jobs, key=job_order):
        key = job['id']
        review = '[Packet](%s/%s.md)' % (packet_prefix, key) if key in packet_ids else '—'
        salary = job.get('levels_fyi') or salary_reference(job, {})
        salary_link = '[%s](%s)' % (cell(salary['label']), salary['url'])
        lines.append('| `%s` | [%s](%s) | %s | %s | %s | %s | %s | %s | %s |' % (
            key, cell(job['title']), job['url'], job.get('score', 0), cell(job['location']),
            cell(job.get('compensation_status')), salary_link, cell(job.get('availability') or 'unknown'),
            cell(seen_date(job.get('last_seen'), local_zone)), review))
    return lines


def write_company_reports(output, all_jobs, matching_jobs, packets, updated, local_zone):
    groups = {job['company']: [] for job in all_jobs}
    for job in matching_jobs:
        groups[job['company']].append(job)
    companies = sorted(groups, key=lambda name: (name.casefold(), name))
    paths = company_paths(companies)
    packet_ids = {packet['id'] for packet in packets}
    index = ['## Openings by company', '',
             'All matching roles, grouped alphabetically by company and ranked by score within each company. '
             'There is no limit on the number shown.', '',
             '| Company | Matching roles |', '|---|---:|']
    for company in companies:
        rows = groups[company]
        filename = paths[company]
        if rows:
            index.append('| [%s](companies/%s) | %s |' % (cell(company), filename, len(rows)))
        lines = ['# %s matching roles' % cell(company), '', '[All companies](../review.md)', '',
                 'Updated: ' + updated, '',
                 '%s roles match your preferences, ordered by score. All matches are shown.' % len(rows), '',
                 'Open means listed at the last successful source check. See each row’s last-seen date; '
                 'manual sources may be incomplete or stale. Listings without open status require rechecking; '
                 'missing listings are not confirmed closed. '
                 'Base pay is not total compensation. Dates use %s.' % local_zone, '',
                 'Levels.fyi links show reported compensation benchmarks, not pay promised for the opening. '
                 'All-level or company-wide links mean the exact grade or role is unconfirmed; directory links need a company lookup.', '',
                 '## Matching your preferences', '',
                 'These pass your current location, level, role and minimum-score filters. '
                 'Scores are keyword-based suggestions, not a hiring prediction.', '']
        lines += job_table(rows, packet_ids, '../packets', local_zone)
        atomic_write(output / 'companies' / filename, '\n'.join(lines) + '\n')
    if not matching_jobs:
        index.append('| No matching roles yet | 0 |')
    return index, paths


def write_reports(root, store, config):
    output = Path(root) / 'reports'
    all_jobs = store.all('jobs')
    salary_index = reference_index(store.all('research'))
    all_jobs = [{**job, 'levels_fyi': salary_reference(job, salary_index)} for job in all_jobs]
    by_id = {job['id']: job for job in all_jobs}
    jobs = [by_id[job['id']] for job in ranked_jobs(store, config)]
    matching_ids = {job['id'] for job in jobs}
    packets = sorted(store.all('packets'), key=lambda p: (
        p.get('company', '').casefold(), p.get('title', '').casefold(), p['id']))
    packet_ids = {packet['id'] for packet in packets}
    local_zone = ZoneInfo(config.get('schedule', {}).get('timezone', 'America/Los_Angeles'))
    updated = datetime.fromisoformat(utcnow()).astimezone(local_zone).isoformat()
    company_index, paths = write_company_reports(
        output, all_jobs, jobs, packets, updated, local_zone)
    lines = ['# Job search review', '', 'Updated: ' + updated, '',
        'Target: approximately $500,000 annual USD total compensation. '
        'Posted base pay is not total compensation; unverified TC stays visible.', '']
    lines += ['Levels.fyi links are reported salary benchmarks. A level-specific link is used only for a verified '
              'grade explicitly named in the job title; other links show all levels, all roles, or the company directory.', '']
    lines += company_index
    lines += ['', '## Source coverage', '', '| Source | State | Detail |', '|---|---|---|']
    for source in store.all('source_health'):
        lines.append('| %s | %s | %s |' % (cell(source['id']), source['status'], cell(source.get('message'))))
    lines += ['', '## Matching opportunities by company', '',
              'All matches are shown, grouped alphabetically by company and ranked within each company. '
              'Listings without open status require rechecking. Dates use %s.' % local_zone, '']
    groups = {}
    for job in jobs:
        groups.setdefault(job['company'], []).append(job)
    for company in sorted(groups, key=lambda name: (name.casefold(), name)):
        lines += ['### ' + cell(company), '',
                  '[%s matching roles](companies/%s)' % (cell(company), paths[company]), '']
        lines += job_table(groups[company], packet_ids, 'packets', local_zone)
        lines.append('')
    if not jobs:
        lines.append('No matching opportunities yet.')
    lines += ['', 'Scores are transparent keyword heuristics, not a probability of interview or offer. '
              'See jobs.json for match reasons, gaps, description, compensation, and timestamps.', '',
              '## Review queue', '']
    for packet in packets:
        key = packet['id']
        if key in matching_ids:
            lines.append('- [%s — %s](packets/%s.md) · [Resume draft](resumes/%s.md)' % (
                packet.get('company', ''), packet.get('title', key), key, key))
        atomic_write(output / 'packets' / (key + '.md'), packet['markdown'])
        atomic_write(output / 'resumes' / (key + '.md'), packet.get('resume_markdown', ''))
    lines += ['', '[Daily goals and coach](daily_coach.md) · [Preparation plan](preparation.md) · [Applications](applications.json) · '
              '[Interview schedule](interviews.json) · [Tasks](tasks.json) · [Research notes](research.md)', '',
              'Edit tracking through the CLI or export → edit → import. Report files are regenerated.']
    atomic_write(output / 'review.md', '\n'.join(lines) + '\n')
    plan = store.get('plans', 'current')
    if plan:
        atomic_write(output / 'preparation.md', plan['markdown'])
    coaching = build_coaching(store.all('applications'), store.all('tasks'), store.all('activities'),
                               store.all('interviews'), store.all('research'), all_jobs, config)
    atomic_write(output / 'daily_coach.md', coaching['markdown'])
    atomic_write(output / 'daily_coach.json', json.dumps(coaching, indent=2, ensure_ascii=False) + '\n')
    for snapshot in store.all('coaching'):
        # Coaching IDs are local dates, assigned by the validated coach command.
        key = datetime.fromisoformat(snapshot['date']).date().isoformat()
        atomic_write(output / 'coaching' / (key + '.md'), snapshot['markdown'])
    for kind in ('jobs', 'applications', 'interviews', 'tasks', 'activities', 'source_health', 'contacts', 'research', 'candidates'):
        rows = all_jobs if kind == 'jobs' else store.all(kind)
        atomic_write(output / (kind + '.json'), json.dumps(rows, indent=2, ensure_ascii=False) + '\n')
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

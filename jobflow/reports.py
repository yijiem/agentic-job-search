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
from .locations import COVERAGE_STATUSES, location_catalog, location_reviews, office_locations


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(content, encoding='utf-8')
    os.replace(temporary, path)
    from .remote_files import persist_path
    persist_path(path)


def atomic_json(path, value):
    """Stream large exports instead of constructing a second full JSON string."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write('\n')
        os.replace(temporary, path)
        from .remote_files import persist_path
        persist_path(path)
    finally:
        temporary.unlink(missing_ok=True)


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


def write_location_reports(output, jobs, catalog, updated, minimum, research=()):
    """Export dated evidence and coverage of the current matching population."""
    def counts(rows):
        return {
            'total': len(rows),
            'with_location_evidence': sum(bool(job['office_locations']) for job in rows),
            'with_office_refs': sum(any(row['relationship'] == 'company_office'
                                       for row in job['office_locations']) for job in rows),
            'with_coordinates': sum(any('latitude' in row and 'longitude' in row
                                        for row in job['office_locations']) for job in rows),
            'with_confirmed_jobsite': sum(any(row['relationship'] == 'job_site' and row['worksite_confirmed']
                                             for row in job['office_locations']) for job in rows),
        }

    def physical_key(row):
        return (row['company'].casefold(), re.sub(r'\W', '', row['address'].casefold()))

    groups = {}
    for job in jobs:
        groups.setdefault(job['company'], []).append(job)
    company_counts = [{'company': company, **counts(rows)} for company, rows in
                      sorted(groups.items(), key=lambda item: item[0].casefold())]
    directory = location_reviews(research, catalog, groups, jobs=jobs)
    by_company = {row['company'].casefold(): row for row in directory}
    for row in company_counts:
        review = by_company[row['company'].casefold()]
        row.update(location_status=review['status'], location_reviewed=review['reviewed'],
                   office_records=review['locations_count'], office_records_with_coordinates=review['coordinates_count'])
    unresolved = [{'company': row['company'], 'roles_without_location_evidence': row['total'] - row['with_location_evidence']}
                  for row in company_counts if row['total'] != row['with_location_evidence']]
    summary = {
        'generated_at': updated,
        'scope': {'eligible': True, 'minimum_score': minimum, 'excluded_availability': ['closed']},
        'definitions': {
            'company_office': 'Sourced company office reference. Matching company and city do not confirm a role works at this address; remote postings receive no inferred office reference.',
            'job_site': 'Dated posting evidence explicitly confirms the address for the linked role. Current company, posting URL and city must still match. Refer to observed_at and the source for current terms.',
            'coordinates': 'A sourced point, not an assertion of entrance accuracy. Census address-range points are approximate; coordinate_method, coordinate_note and precision describe the evidence.',
            'unresolved_companies': 'Companies with one or more current matching roles lacking compatible location evidence. Unresolved roles retain their original employer location string.',
            'company_status': 'Company coverage is independent of role assignment: verified means a sourced office with coordinates; address_only means a sourced address awaiting coordinates; remote_only means a sourced company-wide remote review with no office record; unverified means no validated office or remote-only evidence. Job-site-only evidence counts only while a current matching posting still confirms it. Reviewed counts include completed research with an unresolved outcome.',
            'catalog': 'Catalog totals include retained dated evidence, including job sites no longer attached to a current matching posting. Such stale job sites do not contribute to current company or role coverage.',
        },
        **counts(jobs),
        'companies_total': len(company_counts),
        'companies_with_location_evidence': sum(row['with_location_evidence'] > 0 for row in company_counts),
        'companies_reviewed': sum(row['reviewed'] for row in directory),
        'company_status_counts': {status: sum(row['status'] == status for row in directory) for status in COVERAGE_STATUSES},
        'company_reviews': directory,
        'catalog': {
            'records': len(catalog),
            'companies': len({row['company'].casefold() for row in catalog}),
            'physical_locations': len({physical_key(row) for row in catalog}),
            'records_with_coordinates': sum('latitude' in row and 'longitude' in row for row in catalog),
            'physical_locations_with_coordinates': len({physical_key(row) for row in catalog if 'latitude' in row and 'longitude' in row}),
        },
        'company_counts': company_counts,
        'unresolved_companies': unresolved,
        'addresses_without_coordinates': [row['id'] for row in catalog if 'latitude' not in row or 'longitude' not in row],
    }
    atomic_write(output / 'locations.json', json.dumps(catalog, indent=2, ensure_ascii=False) + '\n')
    atomic_write(output / 'location_reviews.json', json.dumps(directory, indent=2, ensure_ascii=False) + '\n')
    atomic_write(output / 'location_enrichment.json', json.dumps(summary, indent=2, ensure_ascii=False) + '\n')
    lines = ['# Employer location coverage', '', 'Updated: ' + updated, '',
             'Current eligible roles at score %s or above, excluding confirmed closed listings. Missing listings remain visible.' % minimum, '',
             '%s matching roles; %s have compatible location evidence, %s have sourced coordinates, and %s have a posting-confirmed worksite.' % (
                 summary['total'], summary['with_location_evidence'], summary['with_coordinates'], summary['with_confirmed_jobsite']), '',
             '%s evidence records cover %s physical locations at %s companies. %s physical locations have coordinates.' % (
                 summary['catalog']['records'], summary['catalog']['physical_locations'], summary['catalog']['companies'], summary['catalog']['physical_locations_with_coordinates']), '']
    lines += [value + '\n' for value in summary['definitions'].values()]
    lines += ['%s of %s companies have reviewed location evidence: %s with coordinates, %s address-only, %s remote-only, and %s unverified.' % (
        summary['companies_reviewed'], summary['companies_total'],
        summary['company_status_counts']['verified'], summary['company_status_counts']['address_only'],
        summary['company_status_counts']['remote_only'], summary['company_status_counts']['unverified']), '',
        '[Company location reviews](location_reviews.json) retains dated sources and unresolved outcomes.', '']
    lines += ['[Location evidence](locations.json) includes source URLs and observation dates. [Complete job export](jobs.json) preserves employer location strings and includes derived `office_locations`.', '',
              '| Company | Company address status | Matching roles | With location evidence | Office references | Coordinates | Confirmed worksite |',
              '|---|---|---:|---:|---:|---:|---:|']
    lines += ['| %s | %s | %s | %s | %s | %s | %s |' % (
        cell(row['company']), row['location_status'], row['total'], row['with_location_evidence'], row['with_office_refs'], row['with_coordinates'], row['with_confirmed_jobsite']) for row in company_counts]
    atomic_write(output / 'location_enrichment.md', '\n'.join(lines) + '\n')


def write_reports(root, store, config):
    output = Path(root) / 'reports'
    all_jobs = store.all('jobs')
    research = store.all('research')
    salary_index = reference_index(research)
    locations = location_catalog(research)
    all_jobs = [{**job, 'levels_fyi': salary_reference(job, salary_index),
                 'office_locations': office_locations(job, locations)} for job in all_jobs]
    by_id = {job['id']: job for job in all_jobs}
    jobs = sorted([job for job in all_jobs if job.get('availability') != 'closed'
                   and job.get('eligible') and job.get('score', 0) >= config['search']['min_score']],
                  key=lambda job: (-job['score'], job.get('company', ''), job['title']))
    matching_ids = {job['id'] for job in jobs}
    packets = sorted(store.all('packets'), key=lambda p: (
        p.get('company', '').casefold(), p.get('title', '').casefold(), p['id']))
    packet_ids = {packet['id'] for packet in packets}
    local_zone = ZoneInfo(config.get('schedule', {}).get('timezone', 'America/Los_Angeles'))
    updated = datetime.fromisoformat(utcnow()).astimezone(local_zone).isoformat()
    write_location_reports(output, jobs, locations, updated, config['search']['min_score'], research)
    company_index, paths = write_company_reports(
        output, all_jobs, jobs, packets, updated, local_zone)
    lines = ['# Job search review', '', 'Updated: ' + updated, '',
        'Target: approximately $500,000 annual USD total compensation. '
        'Posted base pay is not total compensation; unverified TC stays visible.', '']
    lines += ['Levels.fyi links are reported salary benchmarks. A level-specific link is used only for a verified '
              'grade explicitly named in the job title; other links show all levels, all roles, or the company directory.', '']
    lines += ['[Employer address evidence and coverage](location_enrichment.md) distinguishes company office references from posting-confirmed worksites.', '']
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
        atomic_json(output / (kind + '.json'), rows)
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

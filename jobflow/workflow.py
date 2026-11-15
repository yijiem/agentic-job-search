"""Orchestrate the three agents against one durable state store."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .matching import match_job
from .preparation import build_plan
from .relationships import build_packet
from .sources import canonical_url, fetch_source, job_id
from .storage import utcnow


def validate_daily_goals(config):
    goals = config.get('daily_goals', {})
    if not isinstance(goals, dict):
        raise ValueError('daily_goals must be an object.')
    if 'enabled' in goals and not isinstance(goals['enabled'], bool):
        raise ValueError('daily_goals.enabled must be a boolean.')
    limits = {'applications': (0, 100), 'learning_minutes': (0, 480),
              'coding_minutes': (0, 480), 'system_design_minutes': (0, 480),
              'application_minutes_each': (1, 120)}
    for key, (low, high) in limits.items():
        if key in goals and (isinstance(goals[key], bool) or not isinstance(goals[key], int)
                             or not low <= goals[key] <= high):
            raise ValueError('daily_goals.%s must be an integer from %s to %s.' % (key, low, high))


def load_config(root):
    root = Path(root)
    from .remote_files import hydrate_workspace
    hydrate_workspace(root)
    config = json.loads((root / 'config/settings.json').read_text())
    profile = json.loads((root / 'config/profile.json').read_text())
    schedule = config['schedule']
    ZoneInfo(schedule['timezone'])
    if not 0 <= schedule['hour'] <= 23 or not 0 <= schedule['minute'] <= 59:
        raise ValueError('Schedule hour/minute is invalid.')
    if not schedule['days'] or any(day not in range(7) for day in schedule['days']):
        raise ValueError('Schedule days must be integers 0 (Monday) through 6 (Sunday).')
    prep = config['preparation']
    if not 15 <= prep['daily_minutes'] <= 480 or not 1 <= prep['horizon_days'] <= 90:
        raise ValueError('Preparation budget must be 15–480 minutes and horizon 1–90 days.')
    validate_daily_goals(config)
    if len({s['id'] for s in config['sources']}) != len(config['sources']):
        raise ValueError('Source IDs must be unique.')
    if not profile.get('evidence'):
        raise ValueError('Add resume evidence to config/profile.json first.')
    return config, profile


def ranked_jobs(store, config):
    return sorted([j for j in store.all('jobs') if j.get('availability') != 'closed'
                   and j.get('eligible') and j.get('score', 0) >= config['search']['min_score']],
                  key=lambda j: (-j['score'], j.get('company', ''), j['title']))


def ingest_job(store, raw, profile, config, seen_at=None, index=None):
    now = seen_at or utcnow()
    key = job_id(raw)
    # An imported aggregator record may point to the same canonical employer posting.
    canonical = canonical_url(raw.get('url', ''))
    if index is not None:
        key = index.get(canonical, key)
    else:
        for previous in store.all('jobs'):
            if canonical and canonical_url(previous.get('url', '')) == canonical:
                key = previous['id']
                break
    previous = store.get('jobs', key) or {}
    primary_sources = {s['id'] for s in config.get('sources', []) if s['kind'] != 'manual'}
    if previous.get('source_id') in primary_sources and raw.get('source_id') not in primary_sources:
        previous['also_seen_on'] = sorted(set(previous.get('also_seen_on', []) + [raw.get('source_id', 'manual-import')]))
        store.put('jobs', key, previous)
        return previous, False
    job = dict(previous)
    job.update(raw)
    job.update(match_job(job, profile, config))
    job.update(id=key, first_seen=previous.get('first_seen', now), last_seen=now,
               availability='open', missing_scans=0)
    store.put('jobs', key, job)
    if index is not None and canonical:
        index[canonical] = key
    return job, not bool(previous)


def selected_sources(config, source_ids=None):
    """Validate a retry scope without changing the full configuration or state."""
    active = [s for s in config['sources'] if s.get('enabled', True)]
    if source_ids is None:
        return active
    selected = set(source_ids)
    if not selected:
        raise ValueError('Select at least one source ID.')
    configured = {s['id']: s for s in config['sources']}
    unknown = selected - configured.keys()
    if unknown:
        raise ValueError('Unknown source ID(s): ' + ', '.join(sorted(unknown)))
    disabled = {key for key in selected if not configured[key].get('enabled', True)}
    if disabled:
        raise ValueError('Disabled source ID(s): ' + ', '.join(sorted(disabled)))
    return [s for s in active if s['id'] in selected]


def search(store, config, profile, source_ids=None):
    batch = store.batch(preload=('jobs',)) if hasattr(store, 'batch') else nullcontext()
    with batch:
        return _search(store, config, profile, source_ids)


def _search(store, config, profile, source_ids=None):
    active = selected_sources(config, source_ids)
    scope = {s['id'] for s in active} if source_ids is not None else None
    results = {'new_jobs': 0, 'fetched_jobs': 0, 'source_errors': {}, 'manual_sources': []}
    feeds = []
    index = {canonical_url(j.get('url', '')): j['id'] for j in store.all('jobs')}
    for source in active:
        if source['kind'] == 'manual':
            results['manual_sources'].append(source['id'])
            store.put('source_health', source['id'], {
                'id': source['id'], 'status': 'manual', 'checked_at': utcnow(),
                'message': 'Public search or user import required; no automatic feed.', 'url': source['url']})
        else:
            feeds.append(source)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(fetch_source, source): source for source in feeds}
        for future in as_completed(futures):
            source = futures.pop(future)
            checked_at = utcnow()
            try:
                jobs = future.result()
            except Exception as exc:
                message = str(exc)[:400]
                results['source_errors'][source['id']] = message
                store.put('source_health', source['id'], {
                    'id': source['id'], 'status': 'error', 'checked_at': checked_at,
                    'message': message, 'url': source['url']})
                continue
            seen = set()
            for raw in jobs:
                job, fresh = ingest_job(store, raw, profile, config, checked_at, index)
                seen.add(job['id'])
                results['new_jobs'] += int(fresh)
            results['fetched_jobs'] += len(jobs)
            # Closure is inferred only after two complete successful snapshots; never on failure.
            for old in store.all('jobs'):
                if old.get('source_id') == source['id'] and old['id'] not in seen:
                    old['missing_scans'] = old.get('missing_scans', 0) + 1
                    old['availability'] = 'closed' if old['missing_scans'] >= 2 else 'missing'
                    store.put('jobs', old['id'], old)
            store.put('source_health', source['id'], {
                'id': source['id'], 'status': 'ok', 'checked_at': checked_at,
                'count': len(jobs), 'message': 'Complete public feed snapshot.', 'url': source['url']})
    # A full search applies preference edits to all stored/imported jobs.
    # A retry leaves jobs owned by sources outside its scope untouched.
    for job in store.all('jobs'):
        if scope is not None and job.get('source_id') not in scope:
            continue
        job.update(match_job(job, profile, config))
        store.put('jobs', job['id'], job)
    results['matching_jobs'] = len(ranked_jobs(store, config))
    return results


def personalize(store, config, profile, research=None, selected_id=None, limit=None):
    jobs = ranked_jobs(store, config)
    if selected_id:
        selected = store.get('jobs', selected_id)
        if not selected:
            raise ValueError('Unknown job ID: ' + selected_id)
        jobs = [selected]
    else:
        # Spread the review queue across companies while preserving each company's rank.
        groups = {}
        for job in jobs:
            groups.setdefault(job['company'], []).append(job)
        jobs = []
        while any(groups.values()):
            for group in groups.values():
                if group:
                    jobs.append(group.pop(0))
    contacts = store.all('contacts')
    packets = []
    limit = limit if limit is not None else config['search'].get('max_packets_per_run', 5)
    if limit <= 0:
        return []
    for job in jobs:
        imported_research = [r for r in store.all('research') if r.get('job_id') == job['id']]
        signature = hashlib.sha256(json.dumps(
            [{key: job.get(key) for key in ('description', 'title', 'company', 'department', 'team', 'url', 'matched_skills')},
             profile, contacts, imported_research,
             datetime.now().strftime('%Y-%W') if research else None], sort_keys=True).encode()).hexdigest()
        existing = store.get('packets', job['id'])
        retry_research = bool(research and existing and existing.get('research_notes', {}).get('status') == 'unavailable')
        if (not selected_id and existing and existing.get('input_signature') == signature
                and not retry_research and not existing.get('needs_profile_review')):
            continue
        packet = build_packet(job, profile, contacts, research=research)
        if imported_research:
            packet['imported_research'] = imported_research
            for note in imported_research:
                packet['markdown'] += '\n\n## Imported research for review\n\n' + note['text'] + '\n'
                packet['markdown'] += '\n'.join('- [%s](%s)' % (s.get('title', s['url']), s['url']) for s in note['sources'])
        packet.update(id=job['id'], job_id=job['id'], company=job['company'],
                      title=job['title'], input_signature=signature)
        store.put('packets', job['id'], packet)
        packets.append(packet)
        if len(packets) >= limit:
            break
    return packets


def prepare(store, config, profile, research=None, rebalance_daily=False):
    options = {'research': research}
    if rebalance_daily:
        options['rebalance_daily'] = True
    plan = build_plan(profile, store.all('applications'), store.all('interviews'),
                      store.all('tasks'), config, **options)
    store.put_many('tasks', plan['tasks'])
    store.put('plans', 'current', {**plan, 'id': 'current', 'updated_at': utcnow()})
    coach(store, config)
    return plan


def coach(store, config, today=None):
    from .coaching import build_coaching
    report = build_coaching(store.all('applications'), store.all('tasks'), store.all('activities'),
                            store.all('interviews'), store.all('research'), store.all('jobs'), config, today=today)
    store.put('coaching', report['date'], {**report, 'id': report['date'], 'updated_at': utcnow()})
    return report


def scheduled_due(store, config, now=None):
    schedule = config['schedule']
    local = (now or datetime.now(ZoneInfo(schedule['timezone']))).astimezone(ZoneInfo(schedule['timezone']))
    if local.weekday() not in schedule['days'] or (local.hour, local.minute) < (schedule['hour'], schedule['minute']):
        return False
    state = store.get('schedule', 'daily') or {}
    return state.get('completed_date') != local.date().isoformat()


def run(store, config, profile, research=None):
    run_id = store.start_run()
    summary = {}
    try:
        summary = search(store, config, profile)
        plan = prepare(store, config, profile, research)
        packets = personalize(store, config, profile, research)
        summary.update(packets_created=len(packets), preparation_tasks=len(plan['tasks']))
        status = 'partial' if summary['source_errors'] else 'completed'
        store.finish_run(run_id, status, summary)
        local = datetime.now(ZoneInfo(config['schedule']['timezone']))
        schedule = config['schedule']
        # An early manual run must not consume the upcoming morning's run.
        if local.weekday() in schedule['days'] and (local.hour, local.minute) >= (schedule['hour'], schedule['minute']):
            store.put('schedule', 'daily', {'completed_date': local.date().isoformat(), 'run_id': run_id})
        return summary
    except Exception as exc:
        store.finish_run(run_id, 'failed', {**summary, 'error': str(exc)[:500]})
        raise

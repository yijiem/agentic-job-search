"""Reviewable next-step planning from the account's recorded evidence.

The local planner does not contact a model, employer, calendar, or research site.
It previews the existing scheduler and adds role guidance to unstarted practice
blocks. Approval is bound to the current inputs and exact proposed schedule.
"""

import copy
import hashlib
import json
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from .preparation import INACTIVE_STAGES, build_plan
from .preparation_guides import build_role_guide
from .storage import utcnow


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _now(config, today=None):
    zone = ZoneInfo(config.get('preparation', {}).get('timezone') or
                    config.get('schedule', {}).get('timezone', 'America/Los_Angeles'))
    if today is None:
        return datetime.now(zone)
    if isinstance(today, str):
        today = datetime.fromisoformat(today.replace('Z', '+00:00'))
    if not isinstance(today, datetime):
        today = datetime.combine(today, datetime.min.time())
    return today.replace(tzinfo=zone) if today.tzinfo is None else today.astimezone(zone)


def _upcoming(interviews, applications, now):
    active = {row['job_id']: row for row in applications if row.get('job_id')}
    result = []
    for row in interviews:
        if row.get('status', '').lower() in ('cancelled', 'canceled', 'completed'):
            continue
        if active.get(row.get('job_id'), {}).get('stage') in INACTIVE_STAGES:
            continue
        try:
            starts = datetime.fromisoformat(row['starts_at'].replace('Z', '+00:00'))
            if starts.tzinfo is None:
                starts = starts.replace(tzinfo=now.tzinfo)
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if starts >= now:
            result.append((starts, row))
    return [row for starts, row in sorted(result, key=lambda pair: pair[0])]


def _proposal(store, config, profile, job_id='', today=None):
    now = _now(config, today)
    day = now.date().isoformat()
    applications = store.all('applications')
    interviews = store.all('interviews')
    existing = store.all('tasks')
    research = store.all('research')
    upcoming = _upcoming(interviews, applications, now)
    job = store.get('jobs', job_id) if job_id else None
    if job_id and not job:
        raise ValueError('Choose a role in this workspace before planning its next steps.')
    if not job:
        candidates = [row.get('job_id') for row in upcoming]
        candidates += [row.get('job_id') for row in applications
                       if row.get('stage') not in INACTIVE_STAGES]
        job = next((found for candidate in candidates if candidate
                    for found in [store.get('jobs', candidate)] if found), None)
    selected_id = job['id'] if job else ''
    guide = build_role_guide(job, config, research) if job else None
    plan = build_plan(profile, applications, interviews, existing, config, today=now)
    old = {row['id']: row for row in existing}
    focused = []
    if guide:
        steps = defaultdict(list)
        for step in guide['steps']:
            category = 'learning' if step['category'] == 'research' else step['category']
            steps[category].append(step)
        counters = defaultdict(int)
        for task in sorted(plan['tasks'], key=lambda row: (row.get('due_date', ''), row['id'])):
            if (task.get('status') != 'todo' or not task.get('generated') or
                    not task.get('due_date') or task['due_date'] < day):
                continue
            # Never redirect another role's or interview's preparation.
            if task.get('job_id') not in ('', None, selected_id):
                continue
            if task.get('agent_job_id') not in ('', None, selected_id):
                continue
            options = steps.get(task.get('category'), [])
            if not options:
                continue
            index = counters[task['category']]
            # Introduce each suggestion once rather than repeating it daily.
            if index >= len(options):
                continue
            step = options[index]
            counters[task['category']] += 1
            task.update(agent_job_id=selected_id, agent_title=step['title'],
                        agent_guidance=step['prompt'], agent_reason=step['why_chosen'],
                        agent_sources=copy.deepcopy(step.get('role_evidence', [])))
            if task != old.get(task['id']):
                focused.append(task['id'])
    changes, totals = [], defaultdict(int)
    for task in plan['tasks']:
        before = old.get(task['id'])
        if task != before:
            kind = 'new' if before is None else 'rescheduled' if (
                task.get('due_date'), task.get('status')) != (
                before.get('due_date'), before.get('status')) else 'focused'
            changes.append({'id': task['id'], 'kind': kind, 'title': task['title'],
                            'date': task.get('due_date', ''), 'previous_date': (before or {}).get('due_date', ''),
                            'status': task['status'], 'minutes': task['minutes'],
                            'focus': task.get('agent_title', ''),
                            'guidance': task.get('agent_guidance', '')})
        if task.get('due_date', '') >= day and task['status'] not in ('skipped', 'unscheduled'):
            totals[task['due_date']] += task['minutes']
    next_steps = []
    if upcoming:
        nearest = next((row for row in upcoming if row.get('job_id') == selected_id), upcoming[0])
        next_steps.append({'title': 'Prepare for the next confirmed interview',
                           'detail': '%s · %s. Review the recruiter notes and practice the relevant role exercises.' % (
                               nearest.get('round', 'Interview'), nearest['starts_at']),
                           'kind': 'interview', 'job_id': nearest.get('job_id', '')})
    due = [row for row in applications if row.get('follow_up_date') and
           row['follow_up_date'] <= day and row.get('stage') not in INACTIVE_STAGES]
    for row in due[:3]:
        next_steps.append({'title': 'Review a due application follow-up',
                           'detail': row.get('next_action') or 'Review the saved application and decide the next contact step.',
                           'kind': 'application', 'job_id': row.get('job_id', '')})
    if job:
        next_steps.append({'title': 'Review the resume for %s' % job.get('title', 'this role'),
                           'detail': 'Use a saved resume to draft role-specific changes, then review and save a separate version.',
                           'kind': 'resume', 'job_id': selected_id})
        for step in guide['steps'][:3]:
            next_steps.append({'title': step['title'], 'detail': step['why_chosen'],
                               'prompt': step['prompt'], 'kind': 'practice',
                               'job_id': selected_id, 'sources': step.get('role_evidence', [])})
    else:
        next_steps.append({'title': 'Choose a role to focus your preparation',
                           'detail': 'Select a role above to connect practice and resume suggestions to its recorded requirements.',
                           'kind': 'guidance', 'job_id': ''})
    notes = [row['text'] for row in plan.get('research_notes', [])
             if row.get('kind') == 'scheduling']
    if job and not focused:
        notes.insert(0, 'No additional role focus fits the current unstarted practice blocks. Existing commitments and notes stay intact; use the role guide within a suitable block.')
    proposal = {'job_id': job_id, 'selected_job_id': selected_id,
                'company': (job or {}).get('company', ''), 'title': (job or {}).get('title', ''),
                'date': day, 'timezone': str(now.tzinfo),
                'budget': int(config.get('preparation', {}).get('daily_minutes', 90)),
                'mode': 'local', 'summary': 'A local plan based on your recorded roles, interviews, practice history, and daily budget.',
                'next_steps': next_steps, 'changes': changes,
                'days': [{'date': key, 'minutes': value} for key, value in sorted(totals.items())],
                'notes': notes, 'completed_preserved': sum(row.get('status') == 'done' for row in existing),
                'new_count': sum(row['kind'] == 'new' for row in changes),
                'rescheduled_count': sum(row['kind'] == 'rescheduled' for row in changes),
                'focused_count': len(focused),
                'evidence_note': 'Practice suggestions are original exercises. Employer process claims and reported questions require their own dated sources. No live research or external action is performed.'}
    proposal['fingerprint'] = _digest({'config': config, 'profile': profile, 'job': job,
                                       'applications': applications, 'interviews': interviews,
                                       'tasks': existing, 'research': research, 'day': day,
                                       'plan': plan, 'proposal': proposal})
    return proposal, plan


def perform_agentic_action(store, config, profile, payload, today=None):
    if not isinstance(payload, dict):
        raise ValueError('The planning request must be an object.')
    operation = payload.get('operation')
    if operation not in ('preview', 'apply'):
        raise ValueError('Choose preview or apply for the preparation planner.')
    job_id = payload.get('job_id', '')
    if not isinstance(job_id, str) or len(job_id) > 128:
        raise ValueError('Choose a valid role.')
    proposal, plan = _proposal(store, config, profile, job_id, today)
    if operation == 'preview':
        return {'ok': True, 'proposal': proposal}
    if payload.get('fingerprint') != proposal['fingerprint']:
        raise ValueError('Your workspace changed after this preview. Generate a new plan and review it before applying.')
    store.put_groups({'tasks': plan['tasks'],
                      'plans': [{**plan, 'id': 'current', 'updated_at': utcnow(),
                                 'agent_proposal': proposal}]})
    return {'ok': True, 'applied': True, 'changed': len(proposal['changes'])}

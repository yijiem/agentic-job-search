"""Run from the repository with python3 -m jobflow. No package install required."""
import argparse
import fcntl
import hashlib
import json
import shutil
import sys
from pathlib import Path

from .provider import Researcher
from .reports import atomic_write, write_reports
from .storage import Store, utcnow
from .tracking import KINDS, STAGES, STATUSES, export_tracking, read_records, validate_record
from .workflow import (ingest_job, load_config, personalize, prepare, ranked_jobs, run,
                       scheduled_due, search)
from .preparation import build_plan


def parser():
    result = argparse.ArgumentParser(description='Evidence-grounded multi-agent job search')
    result.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    commands = result.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='Create local configuration and database')
    for name in ('run', 'search', 'personalize', 'prep', 'discover'):
        command = commands.add_parser(name)
        command.add_argument('--research', action='store_true', help='Enable bounded model web research')
        if name == 'run':
            command.add_argument('--due', action='store_true', help='Run only when local daily schedule is due')
        if name == 'personalize':
            command.add_argument('--job')
            command.add_argument('--limit', type=int)
    jobs = commands.add_parser('jobs')
    jobs.add_argument('--limit', type=int, default=20)
    show = commands.add_parser('show')
    show.add_argument('job_id')
    commands.add_parser('status')
    application = commands.add_parser('application')
    application.add_argument('job_id')
    application.add_argument('--stage', choices=STAGES, required=True)
    application.add_argument('--notes', default=None)
    interview = commands.add_parser('interview')
    interview.add_argument('--id', help='Existing ID to update/reschedule')
    interview.add_argument('--job', required=True)
    interview.add_argument('--at', required=True, help='ISO datetime including timezone offset')
    interview.add_argument('--round', required=True)
    interview.add_argument('--notes', default=None)
    task = commands.add_parser('task')
    task.add_argument('id', help='Existing task ID or a new unique ID')
    task.add_argument('--status', choices=STATUSES)
    task.add_argument('--date')
    task.add_argument('--minutes', type=int)
    task.add_argument('--title')
    task.add_argument('--notes')
    export = commands.add_parser('export')
    export.add_argument('--directory', type=Path)
    import_command = commands.add_parser('import')
    import_command.add_argument('path', type=Path)
    import_command.add_argument('--kind', required=True, choices=KINDS)
    commands.add_parser('sources')
    source = commands.add_parser('source-add')
    source.add_argument('--id', required=True)
    source.add_argument('--company', required=True)
    source.add_argument('--kind', required=True, choices=['greenhouse', 'ashby', 'lever', 'manual'])
    source.add_argument('--board', default='')
    source.add_argument('--url', required=True)
    return result


def make_researcher(store, config, enabled):
    if not enabled and not config['research'].get('enabled'):
        return None
    provider = Researcher(config['research'])

    def research(role, prompt):
        # Cache identical prompts per UTC date. Public research is stored for inspection.
        key = hashlib.sha256((utcnow()[:10] + role + prompt).encode()).hexdigest()[:24]
        previous = store.get('research', key)
        if previous:
            return previous
        result = provider(role, prompt)
        result.update(id=key, role=role, retrieved_at=utcnow())
        store.put('research', key, result)
        return result
    return research


def update_tracking(store, kind, rows, config, profile):
    """Validate derived scheduling before committing user changes."""
    datasets = {name: store.all(name) for name in ('applications', 'interviews', 'tasks')}
    merged = {row['id']: row for row in datasets[kind]}
    merged.update({row['id']: row for row in rows})
    datasets[kind] = list(merged.values())
    plan = build_plan(profile, datasets['applications'], datasets['interviews'], datasets['tasks'], config)
    groups = {kind: rows, 'tasks': plan['tasks'],
              'plans': [{**plan, 'id': 'current', 'updated_at': utcnow()}]}
    store.put_groups(groups)


def dispatch(args, store, config, profile):
    root = args.root
    command = args.command
    if command == 'run' and args.due and not scheduled_due(store, config):
        print('Not due; no changes.')
        return
    research = make_researcher(store, config, getattr(args, 'research', False)) if command in (
        'run', 'search', 'personalize', 'prep', 'discover') else None
    if command == 'run':
        print(json.dumps(run(store, config, profile, research), indent=2))
    elif command == 'search':
        print(json.dumps(search(store, config, profile), indent=2))
    elif command == 'personalize':
        if args.limit is not None and args.limit < 1:
            raise ValueError('--limit must be positive.')
        packets = personalize(store, config, profile, research, args.job, args.limit)
        print('Prepared %s review packets.' % len(packets))
    elif command == 'prep':
        plan = prepare(store, config, profile, research)
        print('Preparation plan contains %s tasks.' % len(plan['tasks']))
    elif command == 'jobs':
        for job in ranked_jobs(store, config)[:args.limit]:
            print('%s  %3s  %s | %s | %s' % (job['id'], job['score'], job['company'], job['title'], job['compensation_status']))
        return
    elif command == 'show':
        job = store.get('jobs', args.job_id)
        if not job:
            raise ValueError('Unknown job ID.')
        print(json.dumps(job, indent=2, ensure_ascii=False))
        return
    elif command == 'status':
        print(json.dumps({'jobs': len(store.all('jobs')), 'matches': len(ranked_jobs(store, config)),
                          'sources': store.all('source_health'), 'runs': store.recent_runs(3)}, indent=2))
        return
    elif command == 'application':
        row = store.get('applications', args.job_id) or {'id': args.job_id, 'job_id': args.job_id}
        row.update(stage=args.stage, updated_at=utcnow())
        if args.notes is not None:
            row['notes'] = args.notes
        update_tracking(store, 'applications', [validate_record('applications', row, store)], config, profile)
    elif command == 'interview':
        key = args.id or hashlib.sha256((args.job + args.at + args.round).encode()).hexdigest()[:16]
        row = store.get('interviews', key) or {'id': key}
        row.update(job_id=args.job, starts_at=args.at, round=args.round)
        if args.notes is not None:
            row['notes'] = args.notes
        update_tracking(store, 'interviews', [validate_record('interviews', row, store)], config, profile)
        print('Interview ID: ' + key)
    elif command == 'task':
        row = store.get('tasks', args.id) or {'id': args.id, 'status': 'todo', 'generated': False,
                                             'category': 'custom', 'company': '', 'source_url': ''}
        for key, value in [('status', args.status), ('due_date', args.date), ('minutes', args.minutes),
                           ('title', args.title), ('notes', args.notes)]:
            if value is not None:
                row[key] = value
        update_tracking(store, 'tasks', [validate_record('tasks', row, store)], config, profile)
    elif command == 'export':
        directory = args.directory or root / 'inputs/tracker'
        export_tracking(store, directory)
        print('Editable JSON and CSV trackers: ' + str(directory.resolve()))
        return
    elif command == 'import':
        # Validate the complete batch before writing any records.
        rows = [validate_record(args.kind, row, store) for row in read_records(args.path)]
        if args.kind == 'jobs':
            for row in rows:
                ingest_job(store, row, profile, config)
        elif args.kind in ('tasks', 'interviews', 'applications'):
            update_tracking(store, args.kind, rows, config, profile)
        else:
            store.put_many(args.kind, rows)
        print('Imported %s %s.' % (len(rows), args.kind))
    elif command == 'sources':
        print(json.dumps(config['sources'], indent=2))
        return
    elif command == 'source-add':
        from .tracking import valid_url
        if not valid_url(args.url) or (args.kind != 'manual' and not args.board):
            raise ValueError('A source needs an HTTP(S) URL and a board token for ATS feeds.')
        if any(source['id'] == args.id for source in config['sources']):
            raise ValueError('Source ID already exists; edit config/settings.json to update it.')
        config['sources'].append({'id': args.id, 'company': args.company, 'kind': args.kind,
                                  'board': args.board, 'url': args.url, 'enabled': True})
        atomic_write(root / 'config/settings.json', json.dumps(config, indent=2) + '\n')
        print('Source subscribed: ' + args.id)
    elif command == 'discover':
        companies = [source['company'] for source in config['sources']]
        prompt = ('Find at most %s additional companies with currently open software engineering roles '
                  'in the San Francisco Bay Area or US remote, mid to staff level. Match these skills: %s. '
                  'Exclude existing companies: %s. Target annual total compensation %s %s; distinguish '
                  'base from TC and mark unknown compensation. For each candidate cite official careers and '
                  'job posting URLs, ATS type and board token if evidenced, current date, match reasoning, '
                  'and compensation uncertainty. Treat this as candidate discovery only, not subscription. '
                  'Never claim a live job from a stale search snippet.') % (
                      config['discovery']['max_candidates'], ', '.join(profile['skills']), ', '.join(companies),
                      config['search'].get('currency', 'USD'), config['search'].get('target_tc', 0))
        if research:
            result = research('source_discovery', prompt)
        else:
            result = {'text': 'Research task ready. Run discover --research or complete with Codex web tools.\n\n' + prompt,
                      'sources': [], 'mode': 'pending_research'}
        key = 'discovery-' + utcnow()[:10]
        store.put('candidates', key, {**result, 'id': key, 'status': 'proposed', 'created_at': utcnow()})
        atomic_write(root / 'reports/discovery.md', result['text'] + '\n')
        print('Saved discovery brief; subscribe verified candidates with source-add.')
    path = write_reports(root, store, config)
    print('Review: ' + str(path))


def main(argv=None):
    args = parser().parse_args(argv)
    args.root = args.root.resolve()
    if args.command == 'init':
        (args.root / 'config').mkdir(parents=True, exist_ok=True)
        for name in ('settings', 'profile'):
            destination = args.root / 'config' / (name + '.json')
            if not destination.exists():
                shutil.copyfile(args.root / 'config' / (name + '.example.json'), destination)
        Store(args.root / 'data/jobflow.sqlite3').close()
        print('Initialized. Review config/profile.json and config/settings.json, then run python3 -m jobflow run.')
        return
    (args.root / 'data').mkdir(parents=True, exist_ok=True)
    store = None
    try:
        with (args.root / 'data/workflow.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('Another workflow process is running; try again later.', file=sys.stderr)
                return  # Scheduled duplicate is safely skipped.
            config, profile = load_config(args.root)
            store = Store(args.root / 'data/jobflow.sqlite3')
            dispatch(args, store, config, profile)
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        print('Error: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
    finally:
        if store:
            store.close()

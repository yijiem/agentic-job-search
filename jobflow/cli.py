"""Run from the repository with python3 -m jobflow. No package install required."""
import argparse
import fcntl
import hashlib
import json
import time
import shutil
import sys
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .provider import Researcher
from .reports import atomic_write, write_reports
from .storage import Store, utcnow
from .tracking import (ACTIVITY_CATEGORIES, KINDS, STAGES, STATUSES, export_tracking,
                       read_records, validate_record)
from .workflow import (coach, ingest_job, load_config, personalize, prepare, ranked_jobs, run,
                       scheduled_due, search, selected_sources, validate_daily_goals)
from .preparation import build_plan


def parser():
    result = argparse.ArgumentParser(description='Evidence-grounded multi-agent job search')
    result.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    result.add_argument('--hosted', action='store_true', help=argparse.SUPPRESS)
    result.add_argument('--hosted-interactive', action='store_true', help=argparse.SUPPRESS)
    commands = result.add_subparsers(dest='command', required=True)
    from .cloud_cli import add_parser as add_cloud_parser
    add_cloud_parser(commands)
    commands.add_parser('init', help='Create local configuration and database')
    commands.add_parser('rematch', help='Recalculate matches and plans from saved jobs without polling sources')
    commands.add_parser('refresh', help='Refresh preparation and reports without polling or rescoring jobs')
    web = commands.add_parser('web', help='Open the local job-search web application')
    web.add_argument('--port', type=int, default=8765, help='Loopback port (default: 8765)')
    web.add_argument('--open', action='store_true', help='Open the application in your browser')
    batch = commands.add_parser('batch', help='Manage Codex application batches')
    batch_input = batch.add_mutually_exclusive_group(required=True)
    batch_input.add_argument('--request', type=Path, help='JSON batch action request file; use - for stdin')
    batch_input.add_argument('--codex-run', help='Read and connect to a prepared Codex run')
    batch.add_argument('--worker-id', help='Unique runner ID to retain throughout the Codex run')
    action = commands.add_parser('action', help='Save an interactive edit without waiting for a search')
    action.add_argument('--request', type=Path, required=True, help='Bounded JSON user action')
    agent = commands.add_parser('agent', help='Preview or apply an evidence-grounded preparation plan')
    agent.add_argument('--request', type=Path, required=True, help='JSON planning request file')
    for name in ('run', 'search', 'personalize', 'prep', 'discover'):
        command = commands.add_parser(name)
        command.add_argument('--research', action='store_true', help='Enable bounded model web research')
        if name == 'run':
            command.add_argument('--due', action='store_true', help='Run only when local daily schedule is due')
        if name == 'search':
            command.add_argument('--source', dest='source_ids', action='append', metavar='ID',
                                 help='Search only this enabled source; repeat for multiple sources')
        if name == 'personalize':
            command.add_argument('--job')
            command.add_argument('--limit', type=int)
        if name == 'prep':
            command.add_argument('--rebalance-daily', action='store_true',
                                 help='Move unstarted generated curriculum to retained backlog and adopt daily habits')
    jobs = commands.add_parser('jobs')
    jobs.add_argument('--limit', type=int, default=20)
    show = commands.add_parser('show')
    show.add_argument('job_id')
    commands.add_parser('status')
    application = commands.add_parser('application')
    application.add_argument('job_id')
    application.add_argument('--stage', choices=STAGES, required=True)
    application.add_argument('--notes', default=None)
    application.add_argument('--applied-at', help='Actual submission datetime with timezone offset')
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
    task.add_argument('--category')
    task.add_argument('--source-url')
    task.add_argument('--actual-minutes', type=int, help='Time actually spent; separate from planned minutes')
    task.add_argument('--completed-at', help='Actual completion datetime with timezone offset')
    goals = commands.add_parser('goals', help='Inspect or change daily goals without changing the prep budget')
    goals.add_argument('--applications', type=int)
    goals.add_argument('--learning-minutes', type=int)
    goals.add_argument('--coding-minutes', type=int)
    goals.add_argument('--system-design-minutes', type=int)
    goals.add_argument('--application-minutes-each', type=int)
    coaching = commands.add_parser('coach', help='Review logged daily progress and relevant learning materials')
    coaching.add_argument('--date', help='Local ISO date; defaults to today')
    activity = commands.add_parser('activity', help='Log actual learning or practice time; reuse ID to edit')
    activity.add_argument('id')
    activity.add_argument('--category', choices=ACTIVITY_CATEGORIES)
    activity.add_argument('--minutes', type=int)
    activity.add_argument('--date')
    activity.add_argument('--notes')
    activity.add_argument('--source-url')
    activity.add_argument('--task', dest='task_id')
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
    interactive = getattr(args, 'hosted_interactive', False)
    if interactive:
        from .remote import workspace_id
        if not getattr(args, 'hosted', False) or not workspace_id(root):
            raise ValueError('Interactive cloud execution requires a remote hosted workspace.')
        if command not in ('application', 'task', 'interview', 'import', 'batch', 'agent', 'personalize', 'prep'):
            raise ValueError('This operation belongs in the background worker.')
        if command == 'import' and args.kind == 'jobs':
            raise ValueError('Job ingestion belongs in the background worker.')
    if getattr(args, 'hosted', False):
        # Account-level model credentials are a separate future integration.
        # Never silently spend a process owner's API key for another account.
        config = {**config, 'research': {**config.get('research', {}), 'enabled': False}}
        args.research = False
    if command == 'run' and args.due and not scheduled_due(store, config):
        print('Not due; no changes.')
        return
    research = make_researcher(store, config, getattr(args, 'research', False)) if command in (
        'run', 'search', 'personalize', 'prep', 'discover') else None
    if command == 'agent':
        from .agentic import perform_agentic_action
        if args.request.stat().st_size > 256 * 1024:
            raise ValueError('The planning request is too large.')
        payload = json.loads(args.request.read_text(encoding='utf-8'))
        result = perform_agentic_action(store, config, profile, payload)
        if result.get('applied'):
            try:
                coach(store, config)
                if not interactive:
                    write_reports(root, store, config)
            except (ValueError, OSError, RuntimeError, KeyError):
                result['warning'] = 'Plan saved. Refresh coaching and reports with the CLI.'
        print(json.dumps(result, ensure_ascii=False))
        return
    if command == 'batch':
        from .application_batches import perform_batch_action, workspace_id
        if args.codex_run:
            if not args.worker_id:
                raise ValueError('Use --worker-id with a unique ID for this Codex run; retain it when resuming.')
            payload = {'op': 'codex_context', 'run_id': args.codex_run,
                       'worker_id': args.worker_id, 'workspace_id': workspace_id(root)}
        else:
            if str(args.request) == '-':
                encoded = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
                if len(encoded) > 8 * 1024 * 1024:
                    raise ValueError('The batch request is too large.')
                payload = json.loads(encoded.decode('utf-8'))
            else:
                if args.request.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError('The batch request is too large.')
                payload = json.loads(args.request.read_text(encoding='utf-8'))
        result = perform_batch_action(root, store, config, profile, payload)
        if str(payload.get('op', '')).startswith('codex_'):
            result.setdefault('workspace_id', workspace_id(root))
        if result.pop('application_updated', False):
            try:
                prepare(store, config, profile)
                coach(store, config)
                if not interactive:
                    write_reports(root, store, config)
            except (ValueError, OSError, RuntimeError, KeyError):
                # The receipt and application were committed together already;
                # report projection errors must never invite a second submit.
                result['warning'] = 'Submission saved. Refresh preparation and reports with the CLI.'
        print(json.dumps(result, ensure_ascii=False))
        return
    elif command == 'run':
        print(json.dumps(run(store, config, profile, research), indent=2))
    elif command == 'search':
        print(json.dumps(search(store, config, profile, source_ids=args.source_ids), indent=2))
    elif command == 'rematch':
        from .matching import match_job
        jobs = store.all('jobs')
        job_count = len(jobs)
        def changed_matches():
            for job in jobs:
                matching = match_job(job, profile, config)
                if any(job.get(key) != value for key, value in matching.items()):
                    yield {**job, **matching}
        # Stream changed rows so a rematch does not duplicate or rewrite the
        # entire saved corpus when its matching results are already current.
        store.put_many('jobs', changed_matches())
        del jobs
        # Preserve previous drafts, but make their older profile basis explicit.
        store.put_many('packets', [{**packet, 'needs_profile_review': True}
                                  for packet in store.all('packets')])
        prepare(store, config, profile)
        print('Updated matches for %s saved jobs; no sources polled.' % job_count)
    elif command == 'refresh':
        prepare(store, config, profile)
        print('Refreshed preparation from saved records.')
    elif command == 'personalize':
        if args.limit is not None and args.limit < 1:
            raise ValueError('--limit must be positive.')
        packets = personalize(store, config, profile, research, args.job, args.limit)
        print('Prepared %s review packets.' % len(packets))
    elif command == 'prep':
        plan = prepare(store, config, profile, research, rebalance_daily=args.rebalance_daily)
        print('Preparation plan contains %s tasks.' % len(plan['tasks']))
    elif command == 'goals':
        keys = ('applications', 'learning_minutes', 'coding_minutes', 'system_design_minutes',
                'application_minutes_each')
        changes = {key: getattr(args, key) for key in keys if getattr(args, key) is not None}
        if changes:
            updated = {**config, 'daily_goals': {**config.get('daily_goals', {}), **changes, 'enabled': True}}
            validate_daily_goals(updated)
            # Validate scheduling before writing preferences or derived state.
            build_plan(profile, store.all('applications'), store.all('interviews'), store.all('tasks'), updated)
            atomic_write(root / 'config/settings.json', json.dumps(updated, indent=2) + '\n')
            config.update(updated)
            prepare(store, config, profile)
        report = coach(store, config)
        print(json.dumps(report['goals'], indent=2))
    elif command == 'coach':
        report = coach(store, config, today=args.date)
        print(report['markdown'])
    elif command == 'activity':
        row = store.get('activities', args.id) or {'id': args.id,
            'date': datetime.now(ZoneInfo(config['preparation'].get('timezone',
                                 config['schedule']['timezone']))).date().isoformat()}
        for key in ('category', 'minutes', 'date', 'notes', 'source_url', 'task_id'):
            value = getattr(args, key)
            if value is not None:
                row[key] = value
        row = validate_record('activities', row, store)
        store.put('activities', row['id'], row)
        coach(store, config)
        print('Logged activity: ' + row['id'])
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
        if args.stage == 'applied' and row.get('stage') != 'applied' and not row.get('applied_at'):
            row['applied_at'] = utcnow()
        row.update(stage=args.stage, updated_at=utcnow())
        if args.notes is not None:
            row['notes'] = args.notes
        if args.applied_at is not None:
            row['applied_at'] = args.applied_at
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
        if args.status == 'done' and row.get('status') != 'done' and not row.get('completed_at'):
            row['completed_at'] = utcnow()
        for key, value in [('status', args.status), ('due_date', args.date), ('minutes', args.minutes),
                           ('title', args.title), ('notes', args.notes), ('category', args.category),
                           ('source_url', args.source_url), ('actual_minutes', args.actual_minutes),
                           ('completed_at', args.completed_at)]:
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
    if not interactive:
        path = write_reports(root, store, config)
        print('Review: ' + str(path))


def main(argv=None):
    args = parser().parse_args(argv)
    args.root = args.root.resolve()
    if args.command == 'cloud':
        from .cloud_cli import main as cloud_main
        try:
            cloud_main(args)
        except (ValueError, OSError, RuntimeError) as exc:
            print('Error: ' + str(exc), file=sys.stderr)
            raise SystemExit(1)
        return
    if args.command == 'web':
        # The web process must not hold the workflow lock: its actions invoke
        # short-lived CLI commands, which acquire the existing lock themselves.
        from .web import serve
        if not 1 <= args.port <= 65535:
            parser().error('--port must be between 1 and 65535')
        try:
            serve(args.root, port=args.port, open_browser=args.open)
        except OSError as exc:
            print('Error: ' + str(exc), file=sys.stderr)
            raise SystemExit(1)
        return
    if args.command == 'init':
        (args.root / 'config').mkdir(parents=True, exist_ok=True)
        for name in ('settings', 'profile'):
            destination = args.root / 'config' / (name + '.json')
            if not destination.exists():
                shutil.copyfile(args.root / 'config' / (name + '.example.json'), destination)
        Store(args.root / 'data/jobflow.sqlite3').close()
        print('Initialized. Review config/profile.json and config/settings.json, then run python3 -m jobflow run.')
        return
    if args.command == 'action':
        from .foreground import perform, supported
        from .storage import RecordConflictError
        try:
            if args.request.stat().st_size > 8 * 1024 * 1024:
                raise ValueError('The action request is too large.')
            payload = json.loads(args.request.read_text(encoding='utf-8'))
            if not supported(payload):
                raise ValueError('This operation requires the normal workflow command.')
            config, profile = load_config(args.root)
            if args.hosted:
                config = {**config, 'research': {**config.get('research', {}), 'enabled': False}}
            # Retry only a proven atomic compare-and-set rejection. Transport
            # failures may follow a commit and must never replay a creation.
            for attempt in range(3):
                current = Store(args.root / 'data/jobflow.sqlite3')
                try:
                    with current.optimistic():
                        result = perform(args.root, current, config, profile, payload)
                    print(json.dumps(result, ensure_ascii=False))
                    return
                except RecordConflictError:
                    if attempt == 2:
                        raise
                    time.sleep(0.01 * (attempt + 1))
                finally:
                    current.close()
        except RecordConflictError as error:
            print('Conflict: ' + str(error), file=sys.stderr)
            raise SystemExit(3)
        except (ValueError, OSError, RuntimeError, KeyError) as error:
            print('Error: ' + str(error), file=sys.stderr)
            raise SystemExit(1)
        return
    (args.root / 'data').mkdir(parents=True, exist_ok=True)
    store = None
    try:
        from .remote_interactive import execution_context, InteractiveStore
        with execution_context(args.root), (args.root / 'data/workflow.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('Another workflow process is running; try again later.', file=sys.stderr)
                return  # Scheduled duplicate is safely skipped.
            config, profile = load_config(args.root)
            if args.command == 'search':
                selected_sources(config, args.source_ids)
            store = Store(args.root / 'data/jobflow.sqlite3')
            if getattr(args, 'hosted_interactive', False):
                store = InteractiveStore(store)
            # Retain one worker-scoped record cache across matching, coaching,
            # and report generation to avoid repeated full network downloads.
            batch = store.batch() if hasattr(store, 'batch') else nullcontext()
            with batch:
                dispatch(args, store, config, profile)
            from .remote import workspace_id
            if workspace_id(args.root) and not getattr(args, 'hosted_interactive', False):
                from .remote_files import prune_unreferenced
                prune_unreferenced(args.root)
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        print('Error: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
    finally:
        if store:
            store.close()

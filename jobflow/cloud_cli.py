"""Administrative commands for the hosted pilot; no cloud accounts are created."""
import json
import os
import threading
from pathlib import Path


def add_parser(commands):
    cloud = commands.add_parser('cloud', help='Prepare or operate invite-only cloud hosting')
    cloud.add_argument('--cloud-root', type=Path,
                       default=Path(os.environ.get('JOBFLOW_CLOUD_ROOT', 'data/cloud')),
                       help='Private persistent account storage (never source control)')
    actions = cloud.add_subparsers(dest='cloud_command', required=True)
    serve = actions.add_parser('serve', help='Run authenticated hosted HTTP service')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8765')))
    serve.add_argument('--origin', default=os.environ.get('JOBFLOW_PUBLIC_ORIGIN'))
    serve.add_argument('--no-worker', action='store_true', help='Run a separate worker on the same disk')
    invite = actions.add_parser('invite', help='Allow an email to sign in; sends no message')
    invite.add_argument('--email', required=True)
    adopt = actions.add_parser('adopt', help='Attach an existing workspace to one invited email')
    adopt.add_argument('--email', required=True)
    adopt.add_argument('--source', type=Path, required=True)
    actions.add_parser('accounts', help='List invited accounts and workspace IDs')
    export = actions.add_parser('export-owner', help='Create a consistent private migration snapshot')
    export.add_argument('--output', type=Path, required=True)
    worker = actions.add_parser('worker', help='Process queued and due searches on the same persistent disk')
    worker.add_argument('--once', action='store_true')


def main(args):
    from .cloud_accounts import Accounts, export_workspace
    os.umask(0o077)
    if args.cloud_command == 'export-owner':
        result = export_workspace(args.root, args.output)
        print(json.dumps(result, indent=2))
        print('Private owner snapshot created. Keep it outside Git and transfer it securely.')
        return
    if args.cloud_command == 'serve':
        from .cloud import serve
        if not 1 <= args.port <= 65535:
            raise ValueError('--port must be between 1 and 65535.')
        serve(args.cloud_root, args.host, args.port, args.origin, worker=not args.no_worker)
        return
    accounts = Accounts(args.cloud_root)
    if args.cloud_command == 'invite':
        result = accounts.invite(args.email)
    elif args.cloud_command == 'adopt':
        result = accounts.adopt(args.email, args.source)
    elif args.cloud_command == 'accounts':
        result = accounts.list_accounts()
    elif args.cloud_command == 'worker':
        from .cloud_jobs import WorkQueue, worker_loop
        worker_loop(accounts, WorkQueue(args.cloud_root), threading.Event(), once=args.once)
        return
    else:
        raise ValueError('Unknown cloud command.')
    print(json.dumps(result, indent=2))

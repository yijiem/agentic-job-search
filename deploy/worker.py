"""Run durable queued and due work once, then release paid compute."""
import os
import sys
import threading
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))


def main():
    os.umask(0o077)
    from jobflow.remote import enabled, client_from_env
    if not enabled():
        raise ValueError('The scheduled worker requires Supabase persistence.')
    client_from_env()
    from jobflow.cloud_accounts import Accounts
    from jobflow.cloud_jobs import WorkQueue, worker_loop
    root = Path(os.environ.get('JOBFLOW_CLOUD_ROOT', '/tmp/jobflow'))
    if not root.is_absolute():
        raise ValueError('JOBFLOW_CLOUD_ROOT must be absolute.')
    worker_loop(Accounts(root), WorkQueue(root), threading.Event(), once=True)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError) as error:
        print('Jobflow worker failed: ' + str(error), file=sys.stderr)
        raise SystemExit(1)

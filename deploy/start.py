"""Start the single-instance hosted pilot behind Render's HTTPS proxy.

Only deployment configuration is read here. Account creation and owner import
remain explicit CLI operations; this entry point never seeds sample records or
adopts the first visitor as an owner.
"""
import os
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))


def configuration(environ=None):
    """Resolve non-secret runtime configuration; refuse unsafe defaults."""
    environ = os.environ if environ is None else environ
    remote = environ.get('JOBFLOW_BACKEND', '').casefold() == 'supabase'
    root = Path(environ.get('JOBFLOW_CLOUD_ROOT', '/tmp/jobflow' if remote else '/var/data/jobflow'))
    if not root.is_absolute():
        raise ValueError('JOBFLOW_CLOUD_ROOT must be an absolute workspace path.')
    origin = environ.get('JOBFLOW_PUBLIC_ORIGIN') or environ.get('RENDER_EXTERNAL_URL', '')
    from jobflow.cloud import validate_origin
    origin = validate_origin(origin, '0.0.0.0')
    try:
        port = int(environ.get('PORT', '10000'))
    except (TypeError, ValueError):
        raise ValueError('PORT must be an integer from 1 to 65535.') from None
    if not 1 <= port <= 65535:
        raise ValueError('PORT must be an integer from 1 to 65535.')
    return root, origin, port


def main():
    os.umask(0o077)
    root, origin, port = configuration()
    from jobflow.cloud_auth import SupabaseEmailAuth
    from jobflow.cloud import serve
    # Validate provider configuration before creating any account storage or
    # listening on the network. There is no production authentication bypass.
    SupabaseEmailAuth()
    from jobflow.remote import enabled, client_from_env
    if enabled():
        client_from_env()
    serve(root, host='0.0.0.0', port=port, origin=origin, worker=not enabled())


if __name__ == '__main__':
    from jobflow.remote import RemoteError
    try:
        main()
    except (ValueError, OSError, RemoteError) as error:
        print('Jobflow startup failed: ' + str(error), file=sys.stderr)
        raise SystemExit(1)

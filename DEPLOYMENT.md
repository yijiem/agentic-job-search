# Private Supabase deployment

Jobflow runs independently of your Mac. Supabase provides email identity verification, PostgreSQL records and sessions, a durable work queue, and private object storage. Render serves the website on its Free compute plan and runs a separate scheduled Python worker. The owner is invited and their existing workspace is imported before their first sign-in; new visitors cannot claim it.

## Runtime and cost

`render.yaml` declares a free web service and a 2 GB cron worker on the free Hobby workspace plan. Neither service has a persistent disk. `/tmp/jobflow` is a disposable cache, never the authority for account data. The website does not run a background thread.

The cron service checks queued work at minutes 7, 22, 37 and 52 of every hour. The existing due-aware workflow retains each user's timezone, daily schedule, and completion deduplication. Queued onboarding, matching, and report updates run at the next check. Checks that are not due finish quickly. A single cron execution drains ready work and exits.

Render cron has a $1 monthly minimum and bills active runtime; the 2 GB compute rate currently lists $0.00058/minute. Supabase Free includes 500 MB database, 1 GB file storage, and 5 GB egress. These are provider limits, not a guaranteed total bill. Measure actual worker duration and outbound data. Free Render sleeps on idle and has a cold-start delay; Supabase Free can pause after inactivity. Public multi-user email delivery requires custom SMTP.

References: [Render pricing](https://render.com/pricing), [cron billing](https://render.com/docs/cronjobs), [free web limits](https://render.com/docs/free), [Supabase limits](https://supabase.com/pricing), [email delivery](https://supabase.com/docs/guides/auth/auth-smtp).

## Privacy and durability

- The backend verifies the provider-confirmed email and identity against an administrative invitation. Signing in cannot choose another workspace ID.
- Application cookies are opaque, HttpOnly, Secure and SameSite=Strict, with CSRF checks. The email-link PKCE binding is persisted, browser-bound, expiring and single-use, so cold starts preserve it.
- Private tables live in `jobflow_private`, outside the exposed API schema. RLS and explicit client denials apply. Public RPC functions are `SECURITY INVOKER` and executable only by `service_role`; anonymous and ordinary signed-in clients have no direct database access.
- `SUPABASE_SECRET_KEY` belongs only in backend environment settings. Never put it in browser code, Git, screenshots, or logs. The publishable key does not grant private data access.
- PostgreSQL stores canonical records, original record timestamps, run history, account/session state, file manifests and the queue. Storage bucket `jobflow-private` must be private; no public object policies are needed.
- Immutable objects are addressed by content hash. Files are uploaded before their manifest/reference is committed, and downloads verify SHA-256. Large text exports are compressed. Obsolete unreferenced objects can be retired after a 24-hour grace period; referenced resume versions remain intact.
- Every workspace write carries a renewable database lease. Stale workers cannot overwrite a newer owner. Local cache operations are serialized within the web process.

SQLite remains the default local backend. It is not used as remote durable state or uploaded as a live database object.

## Configure Supabase

Use the existing free project. Review and apply the SQL files under `supabase/migrations/` in order using the Supabase CLI or connected migration tool. The account is intentionally not created by application startup.

Enable Data API access to the `public` RPC schema, disable automatic exposure of new tables, and keep `jobflow_private` unexposed. Do not grant client roles access to the server RPCs. If enabling a previously disabled API leaves its schema cache inactive, save the schema settings and verify a backend RPC before importing data.

Create a private Storage bucket named `jobflow-private` through the Storage API/dashboard, with the Free plan's 50 MB per-object limit. The application compresses large textual artifacts before upload.

Enable confirmed email sign-in. Keep the default magic-link templates. Supabase's built-in SMTP sends only to addresses belonging to the project's organization team. Its current limit is two messages per hour across the project; magic links and optional email codes share that quota. A rate-limit response means to wait before requesting another email; the app does not promise an exact reset time. Configure custom SMTP for production or additional users. See [Supabase email delivery restrictions](https://supabase.com/docs/guides/auth/auth-smtp).

After Render provides the app URL, set it as the Auth Site URL and allow exactly `https://YOUR-HOST/auth/callback` as a redirect. Open the email link in the same browser that requested it.

## Deploy source

Run the offline checks with cloud persistence disabled; tests must never target real accounts:

```sh
JOBFLOW_BACKEND=local python3 -m unittest discover -s tests -q
```

Deploy only reviewed source. Never commit `data/`, `inputs/`, `reports/`, private configuration, owner snapshots, or credentials. The Blueprint disables automatic deploys.

Set these environment variables on both services:

| Variable | Value |
| --- | --- |
| `PYTHON_VERSION` | `3.14.10` |
| `PYTHONUNBUFFERED` | `1` |
| `JOBFLOW_BACKEND` | `supabase` |
| `JOBFLOW_CLOUD_ROOT` | `/tmp/jobflow` |
| `SUPABASE_URL` | The project's HTTPS URL |
| `SUPABASE_SECRET_KEY` | A backend-only secret key |

The web service also needs `SUPABASE_PUBLISHABLE_KEY`. Its origin defaults to Render's `RENDER_EXTERNAL_URL`; set `JOBFLOW_PUBLIC_ORIGIN` only for a different exact HTTPS origin. Render injects `PORT`.

- Web start: `python3 deploy/start.py`
- Cron start: `python3 deploy/worker.py`
- Web health: `/healthz` (no account data)
- Build: `JOBFLOW_BACKEND=local python3 -m unittest discover -s tests -q`

No paid model credentials are supplied. Hosted workers use public feeds and deterministic preparation. **Open in Codex** prepares a scoped desktop request from a hosted batch; the user presses Send to begin. See [the hosted runner workflow](HOSTED_CODEX_APPLICATIONS.md).

Hosted Codex access uses the existing batch records, so this feature requires no
new tables, migrations, environment keys, or public Storage policies. The backend
issues a random 12-hour capability after session and CSRF validation, stores only
its hash, and binds it to the account, workspace, current run, selected roles, and
reserved resume hashes. It is checked again inside the normal CLI lock/remote
lease for every operation. Revocation and renewal retain the original worker and
all application history. Completed and uncertain applications are never retried.

`POST /api/codex/runner` and `GET /api/codex/resumes/<id>` accept the capability in
an Authorization header, without cookies or query parameters. The normal browser
API retains its session/CSRF checks. Only public source and workflow instructions
are served at `/codex/runner.py` and `/codex/workflow.md`. The desktop client uses
private temporary files, verifies downloaded resume size and SHA-256, and never
follows redirects or automatically retries mutations. The website polls the
selected active batch every ten seconds without discarding unsaved edits.

## Import the existing owner

Run administrative commands from a trusted machine with the backend environment loaded. Keep keys out of shell history and command arguments. Choose a private disposable cache path:

```sh
python3 -m jobflow cloud --cloud-root /private/cache/jobflow invite --email you@example.com
python3 -m jobflow cloud --cloud-root /private/cache/jobflow adopt --email you@example.com --source /absolute/existing/workspace
```

`invite` sends no email. `adopt` takes a consistent allowlisted snapshot under the local workflow lock, preserves records and timestamps, and verifies every database record, run and file. Sign-in is blocked while adoption is pending. The source workspace is unchanged. A private frozen checkpoint is retained under `<CLOUD_ROOT>/data/owner-transfers/<account-id>/workspace`. Retry the same command after interruption: it reuses the frozen bytes even if the local source has since changed. A different source path or populated signed-in account is rejected. The checkpoint remains as a recovery copy after completion, and later retries return the receipt without overwriting hosted changes. Local edits made after the snapshot are not silently merged.

The imported profile goes directly into the existing account, skipping onboarding. Verify representative saved applications, completed tasks, resume versions and counts before switching regular use to the cloud. Keep the original local workspace as a backup. Avoid continuing independent edits in both copies after cutover.

## Verification and operations

Verify that an anonymous request cannot execute a private RPC or download a stored object. Run the rollback-only SQL checks in `tests/sql/`, inspect Supabase advisors, and check the bucket remains private. Test two accounts for isolation and verify that an empty local cache reconstructs the same account state.

Monitor the queue, source health and run history after actual scheduled searches. Source failures must remain visible and must never imply jobs closed. A queued refresh updates reports after interactive edits; the free website never performs full ingestion or full exports inline.

Create application-consistent off-host backups of PostgreSQL records and Storage manifests/objects before relying solely on the hosted copy. Supabase Free does not include automatic database backups. The verified original owner snapshot provides the initial recovery copy; it does not cover future hosted edits. Keep credentials out of backups and exercise a restore before opening the product to additional users.

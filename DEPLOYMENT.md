# Invite-only hosted pilot

This deployment runs Jobflow independently of your Mac. Supabase verifies invited email addresses; one Render Python web service runs the application and its background scheduler. Each account has a separate workspace on a persistent disk. Existing owner data is imported before the first sign-in, so the owner skips onboarding and retains the recorded profile, preferences, applications, contacts, research, and preparation history.

The first release deliberately retains SQLite and private files to preserve the existing workflows. Supabase is used for **authentication only**. PostgreSQL, object storage, Google sign-in, and connections to external user services are later work. Uploaded TXT, Markdown, and DOCX files offer text for explicit review; PDFs are stored privately and require pasted text. No messages or applications are sent by the job-search workflow.

## 1. Set up email sign-in

Create a Supabase project, then:

1. Enable the Email authentication provider and keep email confirmation enabled. Allow new email users: an invited person is created at the provider on their first sign-in. Jobflow's separate invite list controls access to the app.
2. Keep the default **Confirm sign up** and **Magic link or OTP** email templates. Jobflow requests a PKCE email link and exchanges the provider authorization code on the server. Default templates work on the free plan; no email-template customization is required.
3. For the owner pilot, use the same email address that belongs to your Supabase organization. Supabase's built-in email service only delivers to organization/team addresses, limits sending to two messages per hour, and is intended for testing. Configure custom SMTP before inviting other users; use a verified sender configuration. Free projects may pause after inactivity.
4. Obtain the project HTTPS URL and **publishable key** (a legacy `anon` key also works). Do not provide the `service_role` or secret key to Jobflow.
5. Once Render gives the app an HTTPS URL, set that root origin as the Supabase **Site URL**, and add exactly `https://YOUR-RENDER-HOST/auth/callback` to **Redirect URLs**. Do not use a wildcard redirect.

Open the sign-in email link in the **same browser** that requested it, within ten minutes. The browser carries an opaque HttpOnly, SameSite=Lax cookie; the corresponding PKCE verifier stays only in server memory. A callback is accepted once, checks the confirmed provider email against the invitation and pending browser request, and issues Jobflow's HttpOnly, SameSite=Strict session. A server restart safely invalidates pending sign-in links; request a new email if that happens. Provider access and refresh tokens are discarded, and authorization codes are not logged or reflected into the callback page. Signing in does not authorize Gmail, LinkedIn, or other service access.

Optional numeric codes remain supported. When custom SMTP (or a plan that permits template editing) is configured, include `<strong>{{ .Token }}</strong>` in both confirmation and magic-link templates. Users can expand **My email contains a numeric code** on the sign-in page.

References: [email sign-in](https://supabase.com/docs/guides/auth/auth-email-passwordless), [PKCE flow](https://supabase.com/docs/guides/auth/sessions/pkce-flow), [email templates](https://supabase.com/docs/guides/auth/auth-email-templates), [custom SMTP](https://supabase.com/docs/guides/auth/auth-smtp).

## 2. Prepare and deploy the source

The repository includes `render.yaml` and `deploy/start.py`. The Blueprint selects a paid **1 CPU / 2 GB** web service in Oregon and a **5 GB** persistent disk. Review current costs before creating it; the free compute plan cannot provide this disk. The initial size is a pilot starting point, not a measured capacity guarantee.

Run the offline checks from the repository:

```sh
python3 -m unittest discover -s tests -q
```

When the Render CLI is installed and configured, validate the Blueprint:

```sh
render blueprints validate render.yaml
```

Push the reviewed application source and deployment files to the desired Git branch. Review the staged files individually; do not commit `data/`, `inputs/`, `reports/`, private configuration, owner snapshots, credentials, or unrelated local work. A Git checkout does not contain the existing private workspace.

In Render, create a Blueprint from that branch, supply the two prompted Supabase values, review the resources, and deploy. Automatic deploys on subsequent commits are disabled in this Blueprint. Use the service's normal manual deploy action for later changes.

| Variable | Purpose |
| --- | --- |
| `SUPABASE_URL` | HTTPS project URL; required |
| `SUPABASE_PUBLISHABLE_KEY` | Publishable or legacy anon key; required |
| `JOBFLOW_CLOUD_ROOT` | `/var/data/jobflow`; all account data stays beneath this disk mount |
| `JOBFLOW_PUBLIC_ORIGIN` | Optional exact HTTPS origin for a custom domain; otherwise the start script uses Render's `RENDER_EXTERNAL_URL` |
| `PORT` | Render's injected HTTP port, normally `10000` |

Store environment values in Render settings, not source files. The process refuses to start without valid authentication configuration and an HTTPS origin. `/healthz` returns process readiness without account data. The web process binds `0.0.0.0`; Render supplies public HTTPS.

References: [Blueprint specification](https://render.com/docs/blueprint-spec), [compute plans](https://render.com/docs/compute-plans), [web services](https://render.com/docs/web-services), [default environment variables](https://render.com/docs/environment-variables).

## 3. Import your existing workspace before signing in

Choose the email you will use for the owner account. The app does **not** give the first visitor ownership of your data.

On your Mac, from the current repository, make a consistent private snapshot outside its inputs/reports directories. Replace the source path if your checkout moved; the output directory must be absent or empty:

```sh
python3 -m jobflow --root /absolute/path/to/agentic-job-search cloud export-owner --output "$HOME/jobflow-owner-transfer"
```

This uses the SQLite backup API under the workflow lock and copies the exact profile/settings plus allowlisted private input/report files. Review the reported record counts and skipped files. It does not rescore jobs, recreate completed tasks, or overwrite the source. It excludes the account registry, session data, and credential files.

Transfer this directory through authenticated SSH/SFTP to the **running Render service**, for example `/var/data/jobflow/import/jobflow-owner-transfer`. Obtain the exact SSH destination from Render's Connect menu; the placeholders below are not literal host credentials. First create the destination directory in the service shell with restrictive permissions:

```sh
umask 077
mkdir -p /var/data/jobflow/import
```

Then transfer from your Mac, substituting the SSH destination shown by Render:

```sh
scp -r "$HOME/jobflow-owner-transfer" YOUR_SERVICE@ssh.YOUR_REGION.render.com:/var/data/jobflow/import/
```

In the running service's shell, from the deployed repository directory, replace `you@example.com` with your chosen email:

```sh
python3 -m jobflow cloud --cloud-root /var/data/jobflow invite --email you@example.com
python3 -m jobflow cloud --cloud-root /var/data/jobflow adopt --email you@example.com --source /var/data/jobflow/import/jobflow-owner-transfer
python3 -m jobflow cloud --cloud-root /var/data/jobflow accounts
```

`invite` creates an allowlist entry and sends no email. `adopt` attaches a copy to that account and records a migration receipt. Retrying the same migration does not overwrite later changes; a different source or a populated destination is rejected. Run these commands in the live service shell, **not** a build/pre-deploy command or one-off job: those cannot access the persistent disk.

Now open the app, request a sign-in email using that address, and open its link in the same browser. A valid imported profile goes directly to the existing workspace. Use **Account** to review the preserved data; there is no need to upload the resume or redo preferences. Compare representative saved applications, completed tasks, and record counts before switching regular use to the cloud. Keep the original as a backup and move any local scheduled runs to the cloud workspace so two independently edited copies do not diverge.

For additional users, run `invite --email ...` in the same live service shell. They receive an empty private profile and follow onboarding at first sign-in. They do not inherit the owner's resume, contacts, applications, or preferences. New workspaces use the public source template at `config/cloud_sources.example.json`; an operator can override it with an array of public source descriptors at `/var/data/jobflow/config/sources.json` before inviting new users. An imported owner retains the original source configuration.

Reference: [persistent disk access and secure transfer](https://render.com/docs/disks#transferring-files).

## 4. Background runs, backups, and limits

The normal start command includes the worker in the same process and disk. It checks each active account's configured daily schedule hourly and calls the existing due-aware workflow. Initial onboarding queues a first search; later profile edits queue matching updates. Inspect source health and run history after actual searches. A failed source remains a visible error rather than proof that its jobs closed.

To manually process queued/due work from the live service shell:

```sh
python3 -m jobflow cloud --cloud-root /var/data/jobflow worker --once
```

Keep **one Render instance**. A separate Render worker or cron service cannot read this disk. Attached disks also mean brief downtime during deploys; this pilot does not offer horizontal scaling or uninterrupted deploys. Moving account state to a managed database and files to object storage is the next step before broader scale. [Render disk constraints](https://render.com/docs/disks#disk-limitations-and-considerations)

Use application-consistent backups: for each workspace ID shown by `accounts`, use `--root /var/data/jobflow/workspaces/WORKSPACE_ID cloud export-owner --output /var/data/jobflow/backups/UNIQUE_NAME`, then transfer the snapshot to a private backup destination. Back up the account registry and queue with SQLite's backup API as part of host administration; do not copy a live SQLite file alone or rely only on disk snapshots. Verify restore procedures before depending on the service.

This release does not migrate desktop Codex automations, browser sessions, or service credentials. Hosted workers use public feeds and deterministic preparation; optional paid model research and external account connections need their own per-user design. Account deletion, a connector token vault, shared public ingestion, and billing are not included in this pilot.

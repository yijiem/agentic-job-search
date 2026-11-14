# Jobflow web application

Run from this checkout with Python 3.9 or later:

```sh
python3 -m jobflow web --open
```

Open `http://127.0.0.1:8765`. Keep the process running while using the application; Ctrl+C stops it. No JavaScript build, package installation, API key, or external font service is required. The interactive street map loads public OpenStreetMap tiles over the internet; the chart and city-role lists work without that service. A fresh checkout first needs `python3 -m jobflow init` and its own profile and source configuration. An empty workspace is shown as empty, without fabricated opportunities or activity.

## Working with roles

The dashboard reads the same database as scheduled searches and CLI commands. Refresh the app after a search run or external tracker edit. Search and filter by company, match score, availability, and location; use the daily inbox to review roles first observed on the selected local date. An import's first-seen date is discovery evidence, not the employer's original publication date.

Select a company and a minimum score of 80 to create a focused batch, or select the incoming roles for a day. Selected roles can be saved and reviewed together. Open each official employer application, review its requested materials, and submit there. Recording an application in Jobflow updates your tracker; it does not submit an employer form. Generated resume and outreach materials remain review drafts grounded in the recorded evidence.

Use application stages, notes, response details, next actions, and follow-up dates to track progress. Historical submissions with unknown dates remain undated. Follow-up reminders reflect recorded dates and actions, not inbox synchronization. Email and calendar integrations are separate future work.

## Reading the evidence

- Match scores are keyword-based triage hints. Posted base pay, verified total compensation, and Levels.fyi benchmarks are separate evidence.
- The discovery chart starts with the top 10 companies, with top 20, all-company, and searchable custom selections. Choose a date range, toggle company lines, and inspect exact counts with the pointer or keyboard. Counts use first-seen records from the matching catalog, including closed history. They are not a reconstruction of historical open positions. Current availability and source health are shown separately.
- The map supports zoom, pan, city search, company and score filters, and city-level role drilldown. Markers approximate cities mentioned in location text; they are not employer office addresses. Remote, broad Bay Area, and unrecognized locations need separate review. Multi-location postings can appear in several city counts. Leaflet is bundled locally; OpenStreetMap tiles and attribution remain visible. Tile requests contain map coordinates and the app origin, not job records, application notes, or profile details. If tiles are unavailable, the city list remains usable. This is an OpenStreetMap street map, not the Google Maps service.
- The network view uses recorded direct and mutual contacts, with saved-role and company filters. A public profile is a prospect, not a personal connection. A management title does not establish ownership of a particular opening. Import contacts through `python3 -m jobflow import FILE --kind contacts`; mutual contacts require a named `via` introducer.
- With the user's authorization, observations from their signed-in LinkedIn account can be imported with `relationship_source: "linkedin_signed_in_observation"`, `observed_at`, `evidence_url`, and `connection_degree`. `mutual_connections` stores named intermediaries with `name` and `profile_url`; `related_job_ids` and `role_relevance` record relevance to individual jobs. The app renders each supported one- or two-degree path and its provenance. An unnamed second- or third-degree label does not establish a complete path. These are dated observations, not a live LinkedIn integration or an exhaustive network search; no LinkedIn credentials or cookies are stored by the app.
- Preparation is available before an interview is scheduled. Open any role and choose **Interview prep** for a role/company guide built from the recorded role, dated official process references where available, and matching research. The preparation selector also includes saved/applied roles and top current matches. Suggested exercises are original practice; they are not reported employer questions or additional scheduled commitments.
- A selected role shows its own tasks, shared company preparation, and the general daily routine. Shared practice uses one workspace budget, not a separate allowance per role; daily totals include other roles' commitments. **Rebuild schedule** creates or refreshes practice without requiring an interview. Confirmed interviews shape deadline-specific preparation, while completed work and notes remain intact.

Each guide explains why an exercise or project fits the role and includes expandable evidence from the recorded title, description, or skill overlap. Its LeetCode section links to a small verified set of public problems with difficulty, practice topics, and role relevance. These are skill-based recommendations, not employer question-frequency claims. Choose one within an existing coding block; recommendations add no extra scheduled time. Account-level totals do not establish ability or individual problem completion.

Selected-role URLs use `#prep?job=JOB_ID`, so an unsaved role remains selected after reloading or reopening the link. The app checks its UI build version and offers **Reload app** when an update is available; it does not reload while you are editing. The first upgrade from an older page requires a normal browser reload.

## Account context and service access

The preparation page's **Account context** section shows LeetCode, Blind, 1Point3Acres, and LinkedIn alongside dated browser-access observations and imported context. **Open** uses the provider's own website and sign-in session. It does not establish background synchronization or a persistent Jobflow API connection. Jobflow does not collect provider passwords or copy browser cookies.

Google Sign-In would identify a user to Jobflow; it does not authorize access to other websites. LinkedIn's self-service sign-in exposes limited profile information, while its Connections API requires separate approval. No supported account-data connector has been verified for the other three services. Where an official API is unavailable, use accessible browser research or official exports, retaining the source and observation date. Restricted content still requires the user's existing access.

Account snapshots use the existing research importer, for example:

```json
{
  "id": "leetcode-context-example",
  "kind": "service_context",
  "service": "leetcode",
  "account_handle": "example-user",
  "browser_access": "signed_in",
  "observed_at": "2026-11-14T09:00:00-08:00",
  "profile_url": "https://leetcode.com/u/example-user/",
  "text": "Summary of what was actually observed in this account.",
  "sources": [{"url": "https://leetcode.com/u/example-user/", "title": "Observed profile"}]
}
```

Import with `python3 -m jobflow import FILE --kind research`. Valid service IDs are `leetcode`, `teamblind`, `1point3acres`, and `linkedin`; browser states are `signed_in`, `sign_in_required`, `public_only`, or `unknown`. Optional LeetCode metrics are `solved_total`, `solved_easy`, `solved_medium`, and `solved_hard`; missing values stay unknown. LinkedIn snapshots can include a sourced `profile_summary` up to 2,000 characters. The API projects only supported metadata and provider-owned source URLs. A later observation changes the displayed browser status without deleting earlier evidence. Individual coding results belong in sourced `coding_progress` research records; missing results never mean unsolved.

Access references: [Google authentication and authorization](https://developers.google.com/identity/gsi/web/guides/integrate), [LinkedIn sign-in scopes](https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/sign-in-with-linkedin-v2), [LinkedIn Connections API](https://learn.microsoft.com/en-us/linkedin/shared/integrations/people/connections-api), and [LinkedIn data export](https://www.linkedin.com/help/linkedin/answer/a1339364/down-your-account-data).

## Code and private data

```text
Browser: jobflow/web_assets/  ->  HTTP adapter: jobflow/web.py
                                      |
                                      +-> read-only SQLite snapshots
                                      +-> python3 -m jobflow commands
                                                  |
                              existing workflow, matching, tracking,
                              relationships, preparation, and storage
                                                  |
                                      private data/jobflow.sqlite3
```

The browser has no database credentials, embedded private dataset, external analytics, or direct SQL access. Static assets contain presentation code. The HTTP adapter validates requests and delegates state changes to the existing command interface so validation, workflow locking, preservation rules, and report regeneration stay in one place. The existing core modules remain independent of HTML and HTTP.

Private configuration, database records, resumes, reports, and import files stay in the workspace's ignored paths. Neither `config/profile.json` nor `config/settings.json` is part of the distributable UI. The server serves an explicit static-asset allowlist rather than the repository directory. Source-code publication can therefore be prepared independently of personal data. Before any future open-source release, review the staged file list, repository history, examples, and any personal files outside ignored directories; this change does not publish or commit the repository.

The API exposes the user's recorded content only to the local browser. `GET /api/state` returns a compact snapshot; `GET /api/jobs/<id>` returns complete role details and related evidence. `POST /api/action` accepts a small allowlist of application, task, interview, personalization, and preparation actions. The page obtains a per-process request token from the snapshot and sends it in `X-CSRF-Token` for writes. External content is rendered as text; links are limited to HTTP(S).

## Mobile access and future hosting

An invite-only hosted transport is now available separately through
`python3 -m jobflow cloud`. See [DEPLOYMENT.md](DEPLOYMENT.md) for email-code
authentication, account isolation, owner-data migration, and the single-instance
Render deployment. The restrictions below continue to apply to the original
local `web` command. Cloud deployment is not performed by either command alone.

The UI is responsive, with desktop as the primary layout. The server intentionally binds to loopback. A phone cannot reach the laptop's `127.0.0.1`: it refers to the phone itself.

For access on the go, deploy the Python application and its private workspace on an always-on host behind authenticated HTTPS, or use an authenticated private tunnel that forwards to a loopback address. The current server validates its local Host/Origin, so an ordinary public reverse-proxy URL is not a configured deployment. Do not expose the development server directly to the public internet. A remote deployment needs an explicit authentication and trusted-origin layer, private persistent storage, backups, and a running scheduled search worker. No cloud deployment or public tunnel is created by `web`.

One portable private option for devices with an SSH client is local port forwarding to the host running Jobflow:

```sh
ssh -N -L 8765:127.0.0.1:8765 your-private-host
```

Open `http://127.0.0.1:8765` on the device that owns that SSH tunnel. This requires a reachable SSH host, an authenticated session, and a mobile SSH client that supports local forwarding if used on a phone. The host must remain awake. A future managed deployment can keep the same core and replace the loopback-only transport with authenticated hosting.

## Verification

```sh
python3 -m unittest discover -s tests -q
```

Web tests use temporary workspaces, not personal application records. Source polling remains separate from these offline tests.

When Node.js is available, `node tests/test_preparation_ui.js` checks preparation filtering, shared capacity, research scoping, safe rendering, and guide loading/retry behavior without browser dependencies.

`node tests/test_trends_ui.js` checks company selections, sparse discovery history, date ranges, integer axes, and safe chart rendering. `node tests/test_insights_ui.js` checks map filters, precise city drilldowns, role navigation, and chart/map lifecycle cleanup. These use Node's standard library; live street-tile loading is verified separately in the browser.

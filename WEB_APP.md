# Jobflow web application

Run from this checkout with Python 3.9 or later:

```sh
python3 -m jobflow web --open
```

Open `http://127.0.0.1:8765`. Keep the process running while using the application; Ctrl+C stops it. No JavaScript build, package installation, API key, or external font service is required. The interactive vector map loads public OpenFreeMap styles and tiles over the internet, with an explicitly labelled OpenStreetMap fallback; the chart and company-role lists work without those services. A fresh checkout first needs `python3 -m jobflow init` and its own profile and source configuration. An empty workspace is shown as empty, without fabricated opportunities or activity.

## Working with roles

The dashboard reads the same database as scheduled searches and CLI commands. Refresh the app after a search run or external tracker edit. Search and filter by company, match score, availability, and location; use the daily inbox to review roles first observed on the selected local date. An import's first-seen date is discovery evidence, not the employer's original publication date.

Select a company and a minimum score of 80 to create a focused batch, or select the incoming roles for a day. **Application batches** adds saved groups, top roles at a company, daily discoveries, private resume uploads, and role-specific materials and answers. Choose **Open in Codex**, review the selected roles and materials, and prepare the request. The desktop link opens this project with the batch request in the composer; press Send once to start. Codex inspects and fills the actual employer pages, asks for missing answers or required consent, and records visible confirmation after a single submission. See [the Codex application guide](CODEX_APPLICATIONS.md) for setup and recovery. The extension install/download/reload mechanism is retired. Prepared requests wait for Codex to connect and are never displayed as running prematurely. You can also apply manually at the employer site. The ordinary application-stage controls update the tracker only. Generated resume and outreach materials remain review drafts grounded in the recorded evidence.

Use application stages, notes, response details, next actions, and follow-up dates to track progress. Historical submissions with unknown dates remain undated. Follow-up reminders reflect recorded dates and actions, not inbox synchronization. Email and calendar integrations are separate future work.

## Resumes and application details

**Resumes** is the shared private library for onboarding, individual roles, and application batches. Upload several PDF, DOCX, TXT, or Markdown versions, choose a default, and save your applicant details once. New batches start with that default and those details; each role can still use a different saved version. Uploading or preparing materials does not submit an application.

Onboarding uploads are available in this same library. Existing onboarding files are imported without replacing originals. Configured resume files inside the workspace's private `inputs/` directory can be synchronized; external resume URLs are references, not automatic downloads. Existing PDFs remain downloadable in their original format. **Review PDF** renders and extracts selectable text in the browser using bundled PDF.js, without a Python PDF dependency or an external document service. DOCX, TXT, and Markdown text is extracted locally.

Start an application from a role or a selected batch, then choose **Tailor resume** in its application materials. The selected roles carry into tailoring automatically; there is no separate role search or target-role dropdown. Choose a base resume and review each selected role in turn. Saving a version selects it for that role when you return to application setup. In an existing batch, save the updated role materials to queue a fresh inspection. The general **Resumes** page remains a library for uploads, manual edits, and saved review drafts. The local tailoring assistant matches recorded skills to the role and reorders exact bullet statements within their existing sections. It shows changes, skill matches, and evidence gaps without inventing experience. Edit the draft and explicitly save it as a separate DOCX version; the original remains unchanged. Pending drafts can be reopened, and saved versions retain their source version and role. This local assistant does not call a language model or perform fresh web research.

In **Review PDF**, select text to propose a replacement or attach a comment. Page notes also work on scanned PDFs; changing scanned text requires a text-based PDF because OCR is not included. Annotations and accept/reject decisions persist in a private review for that source version and target role. The review stays tied to the application role that opened it. For a batch, move between its selected roles in the resume workspace; each role has a separate review. Opening **Review PDF** directly from the library gives a general review without role-tailoring controls. **Suggest from role evidence** proposes local, extractive changes; **Generate AI suggestions** can propose wording changes when configured. Suggestions appear as anchored overlays and before/after cards, and can be edited before acceptance.

To save a PDF revision, accept or reject every pending replacement, preview all pages, name the version, and confirm. At least one accepted text change is required; comments remain saved with the review. The browser typesets accepted text into a new PDF with embedded fonts, rather than covering old text in the source file. The layout may differ, and this is not an arbitrary PDF layout editor. The new library version retains its parent and target role and can be selected for an application or batch, optionally as the default. Original bytes remain unchanged. PDF.js, pdf-lib, fontkit, and fonts are bundled locally with licenses; no CDN scripts are used.

Optional AI rewriting requires `OPENAI_API_KEY` and `OPENAI_MODEL` in the local server process environment and a model that supports Responses structured outputs. The button explains that resume text and the selected job description will be sent to OpenAI and requires a separate confirmation. Calls use `store: false`, bounded input/output, a timeout, and at most three attempts per review. Failed provider attempts count toward this limit. Claims and source excerpts are checked, but the user must still review every suggestion. Set `resume_ai.enabled` to `false` in private settings to disable it. Hosted accounts do not use the server owner's API credentials; AI rewriting remains unavailable there until per-account access is configured. Local role suggestions and manual editing remain available.

**Plan my next steps** in Interview prep creates a reviewable proposal from saved roles, applications, upcoming interviews, research, and task history. It shows role guidance and the proposed schedule within the shared daily budget. Applying the reviewed proposal retains completed work and user notes. If the underlying records change, regenerate the proposal before applying. Use the existing task editor to adjust individual dates or work blocks. Planning is local and does not synchronize an external calendar.

The HTTP library routes are `GET /api/resumes`, `GET /api/resumes/<id>`, and `GET /api/resume-drafts/<id>`. PDF reviews use `GET /api/resume-reviews?resume_id=<id>` and `GET /api/resume-reviews/<id>`. Original and generated files use `GET /api/application-resumes/<id>`. Library writes and `pdf_review_create`, `pdf_review_update`, `pdf_review_suggest`, and `pdf_review_save` use the existing batch CLI transport; plan preview/apply uses `python3 -m jobflow agent --request FILE` under the workspace lock. Hosted routes always select the signed-in account's workspace.

## Reading the evidence

- **Explore roles** opens in **Companies**, a grid of company logos in their original colors, centered at their original size within each tile, scaled down proportionally only when needed to fit. Company names, filtered role counts, and match scores appear at the bottom. Companies default to **Most matching roles**; **Rank companies** also offers highest match score, average match score, and most strong matches (score 80+). These rankings use the current filters. No roles open automatically: choose a company to see its full filtered role list, ranked by score with newer discoveries breaking ties, in pages of 50. **Back to companies** returns to the tile grid. Search, location, score, and listing-status filters apply across companies; a filter that removes the selected company returns to the grid without choosing another company. **All roles** retains the combined list and its highest-match, newest-first, and company A–Z sorts. **Select all company roles** selects the full filtered company list across pages; selected roles remain in the queue when switching views or companies. Tiles are keyboard-accessible buttons. Logos are bundled locally from employer sites, with initials for unavailable images. Browsing does not contact a logo service or alter recorded jobs.
- Match scores are keyword-based triage hints. Posted base pay, verified total compensation, and Levels.fyi benchmarks are separate evidence.
- The discovery chart starts with the top 10 companies, with top 20, all-company, and searchable custom selections. Choose a date range, toggle company lines, and inspect exact counts with the pointer or keyboard. Counts use first-seen records from the matching catalog, including closed history. They are not a reconstruction of historical open positions. Current availability and source health are shown separately.
- The map uses a clean vector basemap, with light/dark styling, pan, zoom, city search, and company/score filters. The overview map stretches to align with the chart. Selecting a city opens searchable company groups containing individual positions, full employer-stated locations, match scores, recheck notices, and employer links. All companies are available; expand additional roles within each group. Multi-location strings are preserved. City markers remain approximate grouping points. Separately imported address evidence can add office-reference or posting-confirmed worksite pins; the original employer location text is retained. Remote, broad Bay Area, and unrecognized locations remain available in the all-locations company list. Multi-location postings can appear in several city counts. Leaflet and MapLibre scripts are bundled locally; OpenFreeMap supplies geographic styles/tiles with visible attribution. If vector rendering fails, the map identifies its OpenStreetMap fallback. Requests contain geographic tile information and the app origin, not job records, application notes, or profile details. If both providers are unavailable, city and company-role lists remain usable.
- The network view uses recorded direct and mutual contacts, with saved-role and company filters. A public profile is a prospect, not a personal connection. A management title does not establish ownership of a particular opening. Import contacts through `python3 -m jobflow import FILE --kind contacts`; mutual contacts require a named `via` introducer.
- With the user's authorization, observations from their signed-in LinkedIn account can be imported with `relationship_source: "linkedin_signed_in_observation"`, `observed_at`, `evidence_url`, and `connection_degree`. `mutual_connections` stores named intermediaries with `name` and `profile_url`; `related_job_ids` and `role_relevance` record relevance to individual jobs. The app renders each supported one- or two-degree path and its provenance. An unnamed second- or third-degree label does not establish a complete path. These are dated observations, not a live LinkedIn integration or an exhaustive network search; no LinkedIn credentials or cookies are stored by the app.
- Preparation is available before an interview is scheduled. Open any role and choose **Interview prep** for a role/company guide built from the recorded role, dated official process references where available, and matching research. The preparation selector also includes saved/applied roles and top current matches. Suggested exercises are original practice; they are not reported employer questions or additional scheduled commitments.
- A selected role shows its own tasks, shared company preparation, and the general daily routine. Shared practice uses one workspace budget, not a separate allowance per role; daily totals include other roles' commitments. **Rebuild schedule** creates or refreshes practice without requiring an interview. Confirmed interviews shape deadline-specific preparation, while completed work and notes remain intact.

Each guide explains why an exercise or project fits the role and includes expandable evidence from the recorded title, description, or skill overlap. Its LeetCode section links to a small verified set of public problems with difficulty, practice topics, and role relevance. These are skill-based recommendations, not employer question-frequency claims. Choose one within an existing coding block; recommendations add no extra scheduled time. Account-level totals do not establish ability or individual problem completion.

Selected-role URLs use `#prep?job=JOB_ID`, so an unsaved role remains selected after reloading or reopening the link. The app checks its UI build version and offers **Reload app** when an update is available; it does not reload while you are editing. The first upgrade from an older page requires a normal browser reload.

## Employer address evidence

Import verified employer addresses through `python3 -m jobflow import FILE --kind research` using `kind: "company_location"`. Required fields are `id`, `company`, `city`, full `address`, `relationship`, `precision`, `source_url`, `observed_at` (YYYY-MM-DD), `text`, and a nonempty `sources` list. The address's official employer evidence URL must also appear in `sources`. Imports are persistent research records; job records, first-seen dates, discovery history, and existing application state are unchanged.

Use `relationship: "company_office"` for an employer-listed office and omit `job_ids`. The API derives `office_locations` only for same-company, city-compatible, nonremote postings, labeling them **Company office reference · worksite unconfirmed**. A shared city or a headquarters address does not confirm a role's worksite. Use `relationship: "job_site"` only when the posting explicitly confirms the address; supply nonempty `job_ids` and every linked employer posting URL in `sources`. Imports reject unknown job IDs and links to a different company or city. These attachments are labeled **Posting-confirmed worksite** and retain their observation date. Each read checks the current company, posting URL and city again; a changed posting stops receiving incompatible evidence while the dated research record is preserved.

Coordinates are optional. If present, both `latitude` and `longitude` must be finite numeric values in range, with `coordinate_source_url` also listed in `sources`. `precision` is `address` or `building`. Optional `coordinate_method` is `employer_map_link`, `census_address_range`, `openstreetmap_address_point`, or `published_address_point`. All methods except an employer map link require `precision: "address"` and a `coordinate_note` explaining the approximation; address-range interpolation and an address or building centroid are not surveyed entrance points. The importer does not geocode addresses or infer offices. Missing coordinates leave the address usable as sourced text without inventing a pin.

Record a completed company-wide address investigation as `kind: "company_location_review"`, with `id`, `company`, `status` (`verified`, `remote_only`, or `unverified`), `observed_at`, `text`, and nonempty HTTP(S) `sources`. `verified` requires `location_ids` referencing already imported, valid same-company address records; other statuses must omit or empty that list. Remote-only reviews require actual company-wide remote evidence, not merely a remote job. Unverified reviews retain the sources checked and the unresolved reason.

`GET /api/state` exposes an allowlisted `locations` catalog and derived `jobs[].office_locations`; role details expose the same linked evidence. `location_reviews` covers every current matching company, including companies without pins. Its `status` derives current catalog coverage: `verified` has an address with coordinates, `address_only` has an address awaiting coordinates, `remote_only` has a sourced remote review and no office record, and `unverified` has neither. `review_status` retains the separate research outcome, and `reviewed` distinguishes unresolved research from a company not yet investigated. Unknown imported metadata is retained locally but omitted from these API projections. Location research and reviews are kept out of ordinary interview-research cards so address evidence is not presented as process or question evidence.

Report regeneration adds the same derived `office_locations` to `reports/jobs.json`, exports the validated public catalog to `reports/locations.json`, the company directory to `reports/location_reviews.json`, and writes `reports/location_enrichment.json` and `.md`. Coverage uses current eligible roles at the configured minimum score, excluding confirmed closed jobs. Company status counts include every matching company and remain separate from compatible role references, sourced coordinates, posting-confirmed worksites, and unresolved roles. Catalog counts deduplicate physical addresses separately from evidence records; an office reference and a posting can support the same address. These are read-only projections and do not rewrite stored jobs or their history.

## My account

Open **My account** in the top bar (`#account`) to view your professional profile, skills, recorded experience, contact details, resume library, default resume, and search preferences. It is available in both the local and hosted workspace. **Manage resumes** and **Edit contact details** open the existing resume library; hosted users can also open **Edit background & preferences** to update their reviewed profile.

The application history defaults to submitted roles, with **Saved** and **All tracked** views, search by company/role/stage, and pagination. It includes tracked jobs even when they no longer match the current search. Applied, recruiter, technical, onsite, and offer stages establish recorded submission history, as does a recorded application date; rejected or withdrawn stages alone do not establish submission. Missing dates remain **Date not recorded**, and saved roles are counted separately. **Update** uses the existing application tracker.

Opening this page only reads data. `GET /api/profile` returns selected profile and evidence fields, excluding private source paths and other configuration metadata. Resume/contact information comes from `/api/resumes`, and application history from `/api/state`. Hosted routes use the signed-in account's workspace. A failed section can be retried without hiding the other available records.

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

The API exposes the user's recorded content only to the local browser. `GET /api/state` returns a compact snapshot; `GET /api/jobs/<id>` returns complete role details and related evidence. `GET /api/batches` returns durable application batches, resume metadata, Codex run status, and the request token; `GET /api/application-resumes/<id>` lets the same authenticated user review an uploaded resume. `POST /api/action` accepts an allowlist of application, task, interview, personalization, preparation, and batch actions. Batch mutations invoke `python3 -m jobflow batch --request FILE` under the existing workflow lock. Normal JSON bodies are limited to 256 KiB; PDF review creation and updates accept up to 1 MiB; resume uploads and PDF revision saves accept up to 7 MiB of JSON (5 MiB of decoded file content). The page sends the request token in `X-CSRF-Token` for writes. The website can prepare a Codex handoff through that same-origin API. Run claims, private material paths, authorization, and submission checkpoints are available only through the local CLI. Hosted workspaces do not emit local desktop launch links. External content is rendered as text; ordinary links are limited to HTTP(S), and the dedicated launch control accepts only the documented codex://new link shape.

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

## Appearance

The workspace starts in light mode from 7 a.m. to 7 p.m. in your browser’s local time, and dark mode overnight. Use the sun/moon button in the top-right corner to switch at any time. Your choice is remembered in this browser and takes priority over the time of day.

## Verification

```sh
python3 -m unittest discover -s tests -q
```

Web tests use temporary workspaces, not personal application records. Source polling remains separate from these offline tests.

`node tests/test_my_account_ui.js` checks the account dashboard's application counts, safe rendering, filters, pagination, read-only loading, failure recovery, and navigation/refresh behavior.

When Node.js is available, `node tests/test_preparation_ui.js` checks preparation filtering, shared capacity, research scoping, safe rendering, and guide loading/retry behavior without browser dependencies.

`node tests/test_trends_ui.js` checks company selections, sparse discovery history, date ranges, integer axes, and safe chart rendering. `node tests/test_insights_ui.js` checks map filters, precise city drilldowns, company-grouped role locations, incremental role expansion, navigation, and lifecycle cleanup. `node tests/test_maps_ui.js` checks vector styles, the local worker, provider fallback, attribution, and cleanup. These use Node's standard library; live street-tile loading is verified separately in the browser.

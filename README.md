# Agentic job search

A local, three-agent workflow for finding jobs, preparing outreach and resume changes, and planning interview practice. SQLite is the shared memory. Markdown reports are the review surface; JSON and CSV exports are editable trackers.

## Start here

Python 3.9+ on macOS/Linux is sufficient; there are no runtime package dependencies.

```sh
python3 -m jobflow init
python3 -m jobflow run
python3 -m jobflow jobs --limit 20
python3 -m jobflow export
```

Open `reports/review.md` for opportunities and personalized packets, `reports/preparation.md` for the daily plan, and `inputs/tracker/` for editable tracking files. The CLI runs from anywhere using the package's repository directory; `--root /absolute/path` can select another initialized workspace.

Private configuration is in `config/settings.json` and `config/profile.json`. They, database files, inputs, and reports are ignored by Git. A new checkout can copy the examples with `init`; replace the example evidence before use.

## What each agent does

| Agent | Available now | External inputs or limitations |
|---|---|---|
| Job search | Reads Greenhouse, Ashby, and Lever public feeds; normalizes jobs; deduplicates; applies level/location filters; scores evidence-backed skills; records source failures and job lifecycle | Meta, LinkedIn and Levels.fyi are manual/public-search sources. Import verified postings from those sources; no private scraping adapters are included. |
| Relationships and personalization | Ranks imported contacts; generates position-specific introduction/email drafts; selects and reorders exact resume achievements into a review draft | True direct/mutual connections require your contact records. Optional model web research finds public prospects but does not verify a personal connection or exact hiring-manager responsibility. |
| Interview preparation | Maintains applications/interviews/tasks; builds an editable daily plan; uses deadlines, progress, and a daily capacity; includes official process references and systems-focused practice | Calendar sync is not connected. Company-specific recent research needs the optional research provider or Codex web research. Paid/private interview reports must be supplied through access you already have. |

Configure public employer feeds with `source-add`; the example configuration contains no subscriptions. A failed source is visibly unhealthy; it never counts as a successful zero-result search. Feed coverage changes over time. After two successful complete scans omit a previously seen job, its status becomes `closed`; the first omission is `missing`, requiring rechecking before applying.

Scores are keyword-based triage hints, not an assessment of hiring probability. The baseline intentionally includes unknown compensation: **base salary is never silently treated as TC**. Verified TC, base-only ranges, currency, and uncertainty are separate fields.

## Track applications, interviews, and preparation

Copy a real job ID from `jobs`, then use:

```sh
python3 -m jobflow application JOB_ID --stage applied --notes "Applied through company site"
python3 -m jobflow interview --job JOB_ID --at 2026-11-05T10:00:00-08:00 --round "Technical screen"
python3 -m jobflow task TASK_ID --status done --notes "Solved in 35 minutes; revisit cancellation"
python3 -m jobflow prep
```

Application stages: `saved`, `applied`, `recruiter`, `technical`, `onsite`, `offer`, `rejected`, `withdrawn`. Update an interview with `interview --id EXISTING_ID ...`. Date/time offsets are mandatory; daylight saving time matters. New tasks can be created with a unique ID, `--title`, `--date`, and `--minutes`. `task --status skipped` removes planned effort; infeasible future work is retained as `unscheduled` for review.

For bulk editing:

```sh
python3 -m jobflow export --directory inputs/tracker
# Edit one of applications.csv, interviews.csv, tasks.csv or contacts.csv.
python3 -m jobflow import inputs/tracker/tasks.csv --kind tasks
```

JSON is also supported and retains all metadata. CSV exports include an `_extra` JSON column; leave it intact so stable task identities and interview links survive a round trip. Export before editing; importing upserts records by ID. Removing a row from an import does not delete the stored record. Use statuses to retain history. Reports are regenerated, so edit through CLI/import, not the report files.

Contact rows require `name`, `company`, `profile_url` or `evidence_url`, and `relationship` (`direct`, `mutual`, `public`). A mutual connection additionally requires `via`, the named introducer. Optional `team`, `title`, and `notes` improve ranking. A shared employer, school, or public profile never establishes a referral path.

To import a posting from an aggregate site, save JSON with `company`, `title`, `url`, `description`, `location`, optional `source_id`, and explicit compensation fields, then run `import FILE --kind jobs`. Use the canonical employer URL when possible. Imported jobs are not automatically revalidated or closed; inspect their `last_seen` date. A duplicated aggregator record does not take ownership of an existing authoritative feed record.

## Optional model-backed research

Public job ingestion, deterministic packets, and scheduling work without credentials. Enable web research by setting `OPENAI_API_KEY` and `OPENAI_MODEL` in your shell environment, then:

```sh
python3 -m jobflow personalize --job JOB_ID --research
python3 -m jobflow prep --research
python3 -m jobflow discover --research
```

Use a model available to your API account that supports Responses web search. No model or API key is assumed, and credentials are never written to this repository. `config/settings.json` caps calls per invocation and output tokens. Calls can incur API charges. The provider uses [Responses web search](https://developers.openai.com/api/docs/guides/tools-web-search), retains sources and usage in SQLite, caches identical prompts for the day, and sends `store: false`. It sends limited public role context; candidate experience is used locally for resume drafts. Web research remains a set of sourced notes for review and cannot inject new resume claims.

Alternatively, Codex can perform web research with connected tools and import sourced notes using `import FILE --kind research`. Each note requires `id`, `text`, and `sources: [{"url": "https://...", "title": "..."}]`; optional `job_id` associates it with a packet. Full research is exported to `reports/research.json` and a readable research report. No key is needed for this route.

## Morning schedule

`config/settings.json` defaults to **8:00 a.m., America/Los_Angeles, every day**. Weekdays use Python numbering (`0` Monday through `6` Sunday). This command checks the configured local day/time and runs at most once per day:

```sh
python3 -m jobflow run --due
```

An external scheduler can check hourly and run this command when due. Change `hour`, `minute`, `days`, and `timezone` in configuration; an hourly heartbeat runs at the first check after that time. Desktop execution requires the host/app to be available, so a sleeping/offline host may run late. For reliable always-on execution, deploy this same command and persistent data directory to an always-on host. The repository itself does not install a system daemon or cron job.

One process holds the workflow lock; concurrent runs skip safely. All completed runs, including partial source failures, are recorded. Run `search` manually to retry failed sources without waiting for the next morning.

## Discovery roadmap

`discover` creates a candidate research brief; `discover --research` finds additional companies with evidence. Subscribe a verified public ATS board using:

```sh
python3 -m jobflow source-add --id example --company Example --kind greenhouse --board example --url https://example.com/careers
```

Automatic subscription is deliberately a later feature, in the roadmap. The proposed policy is in [ARCHITECTURE.md](ARCHITECTURE.md): discover from public job links and technical communities, validate official employer ownership, test the connector, evaluate matched yield, then auto-subscribe within a bounded exploration budget. Feedback on saved/dismissed jobs should improve discovery and ranking over time.

## Verification

```sh
python3 -m unittest discover -s tests -v
```

Tests cover ATS parsing and failure semantics, salary/TC separation, geography and level filters, deduplication and lifecycle, evidence-grounded drafts, contact uncertainty, daily budget, deadlines, state preservation, and tracker round trips. Live job feeds can change independently of these tests. The optional paid API path is tested with mock responses and is not live-verified without credentials.

Public adapter references: [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html), [Ashby public job API](https://developers.ashbyhq.com/docs/public-job-posting-api), [Lever Postings API](https://github.com/lever/postings-api).

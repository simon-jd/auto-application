# auto-application

Latest review fixes and Linux/Dot verification: [2026-10-14 deployment record](docs/updates/2026-10-14-review-deployment.md).

A personal pipeline that finds job postings, picks the ones worth applying to, and prepares applications. Design: [`docs/README.md`](docs/README.md) (overview) and one doc per step. Progress log: [`docs/updates/`](docs/updates/README.md).

Source research: [2026-10-10 job and interview source catalog](docs/research/2026-10-10-source-catalog.md) and [machine-readable registry](docs/research/source-catalog.json). The registry records verified entry points, freshness, provenance and access gaps; it is research data and does not enable feeds.

Review follow-up: [decision/expiry safety, parsing, independent filters, archive queries and ingestion recovery](docs/updates/2026-10-12-review-followup.md). These local fixes preserve saved evidence and manual-only applications; the record distinguishes tests from deployment.

The intended primary runtime is Linux: repository/employer-board collection, deterministic screening,
public-API JD enrichment, Discord delivery and a shared SQLite library. It calls no model or agent.
Manual application decisions remain supported; the historical automated appliers stay off. Optional
model filters and external agents are arranged by the owner. Mac TUI, browser/model scheduling and
launchd are development tools. As of 2026-10-14, the current source and schema are deployed on Linux;
ingestion, screening/enrichment, private Dot MCP and fictional preparation workers are running.
Discord and Grok dependencies/service units are installed but disabled pending owner configuration;
handoff and mailbox access remain opt-in. See the [Linux configuration checklist](docs/linux-connectors-setup.md)
and [deployment evidence](docs/updates/2026-10-14-review-deployment.md) for exact verification boundaries.
The [roadmap](docs/superpowers/plans/2026-10-12-roadmap-p0-p3.md) defines their scope.

---

## Agent interface and Linux foundation

The next direction is a shared job library for several chat/skill/tool clients, with repository, email and website sources. The [design](docs/reviews/2026-10-09-job-agent-proposal.md) and [implementation plan](docs/superpowers/plans/2026-10-09-agent-foundation.md) distinguish the current first batch from later work.

**Available now:** a lightweight, strictly read-only query entry point. It reads the existing database without loading `.env`, starting a browser/model, running migrations, or changing decisions. Run `uv sync` after updating to install the new console command.

```bash
uv run auto-app-query context
uv run auto-app-query jobs --limit 50 --company NVIDIA
uv run auto-app-query job 41
# Same queries with only Python's standard library:
python3 scripts/job_view.py --db data/auto_app.db context --json
python3 scripts/job_view.py --db data/auto_app.db jobs --source linkedin --limit 50
# Optional local HTTP interface:
uv run auto-app-query serve --host 127.0.0.1 --port 8766
```

All query commands return JSON; failures go to stderr with a nonzero exit code. The database defaults to `$AUTO_APP_HOME/data/auto_app.db`, or this checkout's `data/auto_app.db`. An explicit `--db` goes **before** the command. Missing or incompatible databases produce an error and are never created or migrated by this interface.

`jobs` returns `items`, `total`, `returned`, `next_cursor`, `as_of` and `snapshot_max_id`. Follow `next_cursor` with the same filters until it is null to retrieve every result. New insertions are held out of that traversal; existing fields reflect the time each page was read. Use `--query`, `--company`, `--source`, `--state`, `--archived` (only archived), or `--all` (both); the default is active jobs. The limit is 1–200. These are **existing recommendation/decision states**, not the planned `possible/unknown/excluded` qualification model.

The read API exposes GET `/api/context`, `/api/jobs`, and `/api/jobs/{job_id}`. List query parameters match the service; use `archive=active|archived|all` instead of the CLI flags. For another computer, keep it on localhost and use an SSH tunnel: `ssh -N -L 8766:127.0.0.1:8766 user@linux-host`. It currently has no authentication or write endpoints.

An explicit `--state archived` (or `state="archived"` in MCP/API calls) selects archived jobs even when the archive flag has its default active-only value.

`uv run auto-app serve systemd --project /srv/auto-application --home /srv/auto-app-home --uv /home/jobs/.local/bin/uv` prints a Linux service template. It does not install or start it. See [Linux configuration](docs/step4-run.md#14-agent-query-and-linux-foundation-2026-10-09) for deployment details.

**Local MCP tools:** `auto-app-agent` exposes `get_job_context`, `search_jobs`,
`get_job`, `record_application_status`, and `record_application_event` over stdio (default) or loopback
Streamable HTTP. It uses the same job library and preferences, with no browser,
model, mailbox credentials or automatic submission. Manual states are
`want_to_apply`, `applied`, and `not_applied`; an identical status/reason retry is
a no-op. `applied` records the user's report of applying.

The Dot backend has 18 base, preparation and control tools; since 2026-11-17 its private listener also enables the 12 handoff tools (30 in all). Cloud context,
search and detail calls passed after deployment. A fresh Dot-managed task also demonstrated that
screening proposals can persist after a chat-side timeout. See the [verification record](docs/updates/2026-11-17-dot-loopback-handoff.md).

```bash
uv run auto-app-agent serve --db /srv/job-search/runtime/data/auto_app.db \
  --preferences /srv/job-search/runtime/config/preferences.json \
  --transport streamable-http --host 127.0.0.1 --port 8767
# Read-only protocol check against that server:
uv run python scripts/check_agent_mcp.py
```

The [Dot setup guide](docs/dot-setup.md) covers Ubuntu user services, OpenAI's
private outbound tunnel, and a cloud Skill plugin bound to the registered
ChatGPT app. The source Skill is in `plugins/job-search/skills/job-search/`;
`scripts/build_dot_plugin.py --app-id asdk_app_... --output /tmp/job-search-dot-0.4.1.zip`
builds its archive (version 0.4.1; the script never overwrites an existing file, so name each build after its
version). A local Skill alone does not establish Dot cloud access.

**Fictional Ashby preparation (opt-in):** add `--preparation-dir PATH` to the
MCP server and run the separate `auto-app-agent prepare-worker
--preparation-dir PATH` process. This adds `prepare_application(job_id=... or
application_url=...)` and `get_application_run(run_id=...)`. The worker uses
only the packaged `fictional-sales-v1` test identity, fills one Ashby form,
reads back the field values and returns mismatches/missing questions. It stops
before submit and closes the browser; the result includes a saved screenshot
reference, not a live browser handoff. No job is marked applied. The default
driver is `rules`; `--driver claude` enables the constrained agent and requires
the worker user's Claude Code login. Simplify remains a reserved interface.
Readiness checks include native/ARIA validity, committed location selection,
and both resume-storage and form-attachment acknowledgements. Only exact,
target-bound fictional draft/autosave and upload requests are permitted;
final submission, account actions and redirects are blocked.
See [setup and result states](docs/dot-setup.md#fictional-ashby-preparation-v1)
and the [verification record](docs/updates/2026-10-11-ashby-preparation.md).
On 2026-10-11 the Ubuntu worker was deployed with Claude, Chrome and a 240-second
task limit. A live Ramp Sales test matched all six required fields and stopped
before submit. That 2026-10-11 MCP check exposed six tools; plugin `0.2.0` was uploaded,
and the refreshed cloud app lists four read/two write tools. Both new tools were
then called through the connected cloud backend: `prepare_application` reused
the existing completed run and `get_application_run` returned the same result.
This verified cloud callability and idempotent readback without another browser
execution. The production MCP queue run also matched
all six required fields, and its result survived worker/MCP restart; repeating
the request reused the same run. Ubuntu regression recorded 951 passed, two
failures and one skip; after fixing the two failures, all 132 preparation tests
passed again. The dated record preserves both results.

**Following batches:** website verification and JD enrichment, broad opportunity qualification, cited company research, resume generators and persistent tasks. LinkedIn has an existing adapter; Jobright website access remains unverified while its public repository feeds are enabled; Simplify's repository feed, website and application hand-off are separate integrations. Automated applying remains off.

Review fixes dated 2026-10-12 cover collection isolation, identity matching, ingestion performance, selection retries, retention, preparation readiness and query Host/Origin validation. See the [repair record](docs/updates/2026-10-12-review-remediation.md) for behavior, verification and existing-data limits.

## Shared screening preferences

`config/preferences.json` holds deterministic preferences shared by repository, email and website jobs. With `AUTO_APP_HOME`, it lives in `$AUTO_APP_HOME/config/`; on home-ubuntu the active file is `/home/postedism/Desktop/job-search/runtime/config/preferences.json`.

```bash
cp config/preferences.example.json config/preferences.json
```

```json
{"exclude_countries": ["Canada"]}
```

Canada-only jobs are excluded; a job with a US option remains visible even when it also lists Canada. Matching checks merged locations and every posting's original locations, so `Remote in Canada` still works after normalization to `remote`. Canadian province names/codes are recognized; `San Jose, CA`, `Ontario, CA` and `Vancouver, WA` remain visible. Unknown locations remain visible. This is a country preference, not full opportunity qualification.

The agent query CLI/API, ordinary `job list`, the TUI/web job lists, and model filter planning apply these preferences. Sources still collect the original evidence; exclusions do not delete records, alter existing recommendations or overwrite manual decisions. Audit/evaluation and direct job details retain the original library. Setting `exclude_countries` to `[]` removes the exclusion on the next query/filter run; no ingestion restart is needed. A job that also lists a US location is never excluded ("Seattle, WA / Vancouver, BC" stays). Excluded collected jobs archive at TTL expiry even when never judged; use the archived/all view after removing the exclusion to see expired records.

`auto-app-query` automatically reads `<database runtime home>/config/preferences.json` (`data/auto_app.db` uses its parent's parent; other database paths use their parent). Override with `--preferences PATH` **before** the command. Only this non-secret JSON file is loaded; `python3 -S scripts/job_view.py` still works. An explicit missing/invalid file fails visibly. `jobs --include-excluded` (API: `include_excluded=true`) provides an audit view; `--all` only controls archived records and does not bypass preferences. Results include `screening.excluded`, `reasons` and `matched_locations`. `context.preferences` reports the effective settings and excluded/included active-job counts; `context.counts` remains the raw library count. Changing preferences invalidates an old pagination cursor.

Other configuration remains separate: `config/searches.yaml` controls website searches (locations, title/company exclusions); `config/feeds.yaml` controls sources and polling; `filters/<name>/command.md` and `filter.yaml` define model judgment instructions and execution. Country names other than Canada currently match explicit names in locations; only Canada's province aliases are implemented.

## Repository feeds and email recommendations

The ingestion commands add repository listings and exported `.eml` recommendations to the same job library. Any feed may set `every_hours` (above 0, at most 168) to be read at most that often; such feeds share a budget of 40 reads per round, most overdue first, which is how long-tail employer boards are added ([2026-11-18](docs/updates/2026-11-18-workday-pacing.md)). A `simplify` feed may set `incremental: true` with a local file pattern (Grok's daily files): each new or changed file is read once and never marks missing what it leaves out ([2026-11-18](docs/updates/2026-11-18-board-proposals-and-grok-daily.md)). The Discord bot also proposes up to 10 unconnected employer boards a week, added on the owner's click. Supported repository formats are `simplify` (default), `speedyapply`, `applyguy`, `ricsign` and `jobright`, plus the employer boards `greenhouse-board`, `lever-board`, `ashby-board`, `workday-board`, `eightfold-board`, `icims-jibe-board`, `oracle-board` and `smartrecruiters-board` ([Employer boards](#employer-boards)). The four after `simplify` use explicit provider adapters, not a generic GitHub README parser. The example config includes two Simplify feeds plus eight feeds for SpeedyApply's US SWE/AI internship/new-grad tables, ApplyGuy, Ricsign and Jobright's SWE new-grad/internship tables.

```bash
cp config/feeds.example.yaml config/feeds.yaml
uv run auto-app ingest run --config config/feeds.yaml --db data/auto_app.db
# Or import one saved input:
uv run auto-app ingest repository listings.json --source saved-repo --db data/auto_app.db
uv run auto-app ingest repository NEW_GRAD_USA.md --format speedyapply --source speedyapply-swe-newgrad --db data/auto_app.db
uv run auto-app ingest email first.eml second.eml --source recommendations --db data/auto_app.db
uv run auto-app-query jobs --source recommendations
```

These commands **write** to the chosen database and may initialize/migrate it. They return JSON reports; each source in a config runs independently, with partial/failed imports reported and a nonzero exit code. Use a scratch `--db` to try them. Relative paths and email globs in the feed config are relative to that config file.

Each observation keeps its source ID, origin, content hash/version, source fields, status and change history (a description is stored on the posting; the import record keeps its SHA-256 and length). Reimporting the same content retains the job ID and your decision; changed content refreshes current fields and flags `needs_reassessment` while keeping past judgments. HTTP feeds use conditional requests. Malformed or failed snapshots do not mark previous records missing; absence in a complete snapshot means **missing from that source**, not confirmed closed by the employer. Initially inactive/hidden rows are recorded without creating a new opportunity.

Set `format` alongside `kind: repository` and `location` in feeds YAML. Changing format or URL forces a complete fetch before reusing HTTP validators. ApplyGuy uses `listingUrl`, Ricsign uses `apply_url`, SpeedyApply uses its apply link, and Jobright retains its public detail URL until employer-link enrichment. Exact known identities merge across sources; matching titles alone do not. Relative ages, yearless dates and refresh clocks stay in `metadata.source_observation` without triggering content changes. Date-only JSON values retain day precision. Original locations remain available to the shared Canada exclusion. [Provider details and deployment verification](docs/updates/2026-10-10-priority-repository-feeds.md).

A `simplify` file may resolve a listing site's job to its employer: `url` is the employer link and the optional `listing_url` the site's page (for example `https://jobright.ai/jobs/info/<id>`). The posting then joins either the Jobright/LinkedIn listing's job or the employer link's job already in the library, and enrichment reads the employer's JD. `employment_type` accepts `FULL_TIME`, `Full-time` or `full_time`. Grok Bot's LinkedIn, Handshake and Jobright files use this format ([2026-11-01 record](docs/updates/2026-11-01-grok-feeds-dedup.md)).

`skip_internships: true` at the top of `config/feeds.yaml` leaves out rows whose own title or employment type
says internship: intern, co-op, apprentice, or a `Summer 2027`-style title without a new-grad word. A source can
set its own `skip_internships`. Skipped rows create no jobs, screening would have excluded them anyway, and
reports count them as `skipped_internships`. The default is `false`
([2026-11-02 record](docs/updates/2026-11-02-skip-internships.md)).

Email parsing extracts job links and any observed titles, cleans known tracking links and deduplicates repeated links within a message. It does not connect to your inbox or fetch the linked website. Missing title/JD is explicitly **pending enrichment**, visible in query output, and skipped by automatic model filtering. Pending imports without a manual decision do not expire; other imported jobs archive on expiry to retain their history. Exact official job identities can still merge across a repository and email; unrelated sparse listings are not merged by similar titles. The query detail includes saved provenance and changes. Archived imported identities stay attached to the same archived job on reimport.

Gmail/Outlook connections and scheduled ingestion are available below; IMAP is implemented locally but remains unconnected to a real mailbox. The Linux worker below fills missing JDs from supported public employer APIs. Verifying authenticated LinkedIn/Jobright/Simplify website access remains subsequent work. See [ingestion behavior](docs/step1-collect.md#14-repository-and-email-ingestion) and the [implementation record](docs/superpowers/plans/2026-10-09-multisource-ingestion.md).

## Gmail, Outlook and recurring ingestion

`kind: mailbox` sources read scoped Gmail/Outlook mail through their official APIs, using the same parser and job library as `.eml` imports. OAuth refresh, successful-message receipts, bounded pagination and source failure reports persist between runs. Mail reads do not send, delete, move or mark messages read. Configure scopes and authorize before enabling the disabled mailbox examples in `config/feeds.example.yaml`.

```bash
uv run auto-app ingest auth gmail --client credentials/google-client.json --credentials credentials/gmail.json
uv run auto-app ingest auth outlook --client-id YOUR_CLIENT_ID --credentials credentials/outlook.json
uv run auto-app ingest watch --config config/feeds.yaml --db data/auto_app.db --interval 1800
uv run auto-app ingest status --db data/auto_app.db
# Print a Linux user service; install nothing:
uv run auto-app ingest systemd --project /srv/auto-application --home /srv/auto-app-home --uv /home/jobs/.local/bin/uv
```

The worker runs once immediately, then waits 30 minutes after each completed round by default. It resumes the saved next-run time after a restart, reloads feeds each round, and shares an import lock with manual commands. `watch --once` forces one immediate round. It runs independently of browser searches and model filters. Query context exposes `sources[].ingest_run` and allowlisted mailbox progress; `ingest status` reads the scheduler sidecar without opening the database.

Mailbox sources require a credential file plus an explicit Gmail `query`/`label_ids` or Outlook `folder`/`senders`. Defaults: `lookback_days: 14`, `page_size: 50`, `max_pages: 4`. Relative credential paths are relative to feeds YAML. Read [authorization and Linux deployment instructions](docs/mailbox-linux-setup.md), including the Google Testing-mode token lifetime, sender/folder setup, timestamps and user-service installation. The repository worker is now deployed on `home-ubuntu` under its desktop `job-search` directory; [deployment checks and operating commands](docs/updates/2026-10-10-linux-deployment.md) record the observed state. Real Gmail/Outlook authorization remains pending.

## Linux worker: screening, JD enrichment and Discord

`auto-app worker` runs next to the feed worker and calls no model or agent. Its default round screens active
jobs with deterministic rules, fills missing descriptions from the employer behind each imported link, screens again, then sends new kept jobs and reported handoff outcomes to Discord, checks a rolling seven-day summary
and disk health. Default steps are `screen,enrich,notify,summary,health`. `watch` starts rounds 15 minutes apart;
long rounds finish before another starts. Sponsorship offers sort first, and recommendations from your
own filters are marked ⭐. Exclusions are a reversible view: jobs and manual decisions are preserved.
`auto-app filter run`, which you run when desired, skips rule-excluded jobs and those awaiting enrichment.

Enrichment reads public APIs for Greenhouse (EU boards included), Lever, Ashby (board names with dots or spaces
included), Workday, Microsoft careers, SmartRecruiters, Workable and Amazon (its search index), plus the public
job pages of TikTok/ByteDance (lifeattiktok.com, joinbytedance.com, jobs.bytedance.com) and Jobright. For Jobright
the description is Jobright's logged-out summary of the JD, not the employer's text. Each round makes at most 200
requests, at least one second apart per host. A link cached as unsupported before its adapter existed is read in
the next round; other unsupported and gone links are not retried. Accepted link shapes, what each site gives and
how a closed job is recognized: [Linux worker §1](docs/linux-worker.md#1-description-enrichment).

The optional `$AUTO_APP_HOME/config/screening.json` controls internship, senior-title, non-CS,
citizenship / US-person (ITAR) / clearance and outside-the-US exclusions (`us_only`: a job whose explicit places are all abroad
is excluded; US, US-remote and unknown places stay). A clearance or US-person requirement stated in the
description or the title excludes; a hedged one ("may require", "if needed"), a licence route, or one listed
only under a preferred heading does not. A clear requirement of at least 3 years' experience excludes by default,
including new-grad titles ("1~3 years" counts as 1; "a degree or 3+ years" as 0); lack of sponsorship alone does not.
The opt-in `exclude_unknown_without_software_evidence` (off by default) also excludes a job whose title
names no role and whose description names no software skill. With `graduation` set (`"YYYY-MM"`, off by default), a job
whose explicit start date or graduation class ends before it is excluded (`start_before_graduation`): "2026 New Grad",
"start date: January 2027", "must graduate by August 2026"; earliest dates ("as early as"), open ends ("or later"),
hedged or preferred mentions are kept. Rule details: [Linux worker §2](docs/linux-worker.md#2-deterministic-screening).

```bash
# Optional project-local rules file (use $AUTO_APP_HOME/config on Linux):
cp config/screening.example.json config/screening.json
uv run auto-app worker run --steps screen
uv run auto-app worker run --steps screen,enrich --enrich-limit 20
uv run auto-app worker status
uv run auto-app-query jobs --sponsorship offers
```

The query CLI, HTTP API and MCP `search_jobs` hide rule-excluded jobs by default; `--include-excluded` /
`include_excluded=true` exposes audit evidence including `screening.reasons`. The first worker round saves
a cutoff to avoid announcing the backlog, and a feed added later (such as an employer board) gets its
own cutoff, so its existing jobs are not pushed either. Title-only arrivals wait two hours. Discord is optional until
`AUTO_APP_DISCORD_WEBHOOK_URL` is configured; no applications are submitted.

**Implemented locally; deployment and live acceptance pending.** Setup, state files, delivery limitations,
deployment backup/restart behavior and the 24-hour acceptance criteria: [Linux worker](docs/linux-worker.md).

### Employer boards

Find Greenhouse, Lever and Ashby boards from already saved jobs, then review the generated feed entries
(Workday, Eightfold, iCIMS Jibe, Oracle and SmartRecruiters boards are added by hand; see below).
Enabled entries in `runtime/config/feeds.yaml` are pulled by the ingest worker every 30 minutes; this
feature has not yet been activated on Linux. [Board setup and limits](docs/linux-worker.md#6-employer-boards).

```bash
uv run auto-app worker boards --yaml --top 30
```

**Workday career sites** (`*.myworkdayjobs.com`: GM, NVIDIA, Intel, Aptiv, Cisco …) use `format: workday-board`
with the careers URL as `location` and a required `company`. Optional `search` (a text or a list, one Workday
search each), `facets` (a facet's parameter or label → values as the site shows them) and `max_pages` (pages of
20, default 25, at most 100) narrow it. The reader pauses three seconds between requests. Workday sites take turns
(2026-11-18): a feed round reads one, the most overdue, each at most every two hours, and a site that answered
HTTP 429 waits out `Retry-After` or a 1–12 hour backoff ([pacing](docs/updates/2026-11-18-workday-pacing.md)). The list has no JD:
the worker's enrichment reads it for the postings screening keeps. A search that ends before Workday's total,
or at the page cap, imports what it read as a partial snapshot and closes nothing. Workday lists at most
2000 results, so narrow large employers with facets.
[Workday board feeds](docs/updates/2026-11-06-workday-board.md).

```yaml
  board-workday-aptiv:
    kind: repository
    format: workday-board
    company: Aptiv
    location: https://aptiv.wd5.myworkdayjobs.com/aptiv_careers
    facets:
      Country: [United States of America]
    max_pages: 10
```

**Eightfold, iCIMS Jibe, Oracle Recruiting Cloud and SmartRecruiters** read the same way, page by page through
the site's public search, with the pipeline's User-Agent and a pause between requests (two seconds for
Eightfold, which answers bursts with HTTP 429). Each takes an optional `country`, `search` (a text or a list)
and `max_pages`. A search that falls short of the site's total is read once more (a posting added or closed
mid-read shifts the pages); if it still falls short, the run imports a partial snapshot and closes nothing.

| Format | `location` | `country` | Notes |
|---|---|---|---|
| `eightfold-board` (Qualcomm) | `https://<tenant>.eightfold.ai/careers?domain=<domain>` | Eightfold's location text: `United States` | `company` required; `filters` such as `job_family: [software engineering]`; 10 a page; JDs through enrichment |
| `icims-jibe-board` (Garmin, AMD, Lutron, Rivian) | the careers site, `https://careers.garmin.com` | the site's country name: `United States` | 100 a page, **with full JDs** (no enrichment) |
| `oracle-board` (Ford, TI, Honeywell) | `https://<pod>.fa.<region>.oraclecloud.com/hcmUI/CandidateExperience/en/sites/<site>` | a location facet's name: `United States` | `company` required; 100 a page plus one facet request; JDs through enrichment |
| `smartrecruiters-board` (Bosch, Intuitive, Western Digital) | `https://careers.smartrecruiters.com/<CompanyIdentifier>` | ISO code: `us` | 100 a page; JDs through enrichment |

[Paged board feeds](docs/updates/2026-11-06-paged-boards.md) has the live counts and request costs.

```yaml
  board-oracle-ford:
    kind: repository
    format: oracle-board
    company: Ford
    location: https://efds.fa.em5.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1
    country: United States
```

### IMAP

The local IMAP provider can read scoped Gmail recommendation mail with EXAMINE and BODY.PEEK; real mailbox
credentials and unread-state acceptance remain pending. Its disabled feed example keeps the existing
repository sources intact. [Gmail IMAP setup](docs/mailbox-linux-setup.md).

```bash
uv run auto-app ingest status --db data/auto_app.db
```

### Public MCP and control tools

A separate token-protected MCP listener can be placed behind a public HTTPS tunnel; the existing Dot
listener stays on its private path. Pause, status, rule preview and digest tools are optional. A Grok
connection has not been tested; its Bearer-token compatibility must be checked live. [Remote MCP guide](docs/remote-mcp.md).

```bash
uv run auto-app-agent serve --db data/auto_app.db --transport streamable-http --port 8769 --auth-token-file PATH --public-url https://YOUR-TUNNEL-HOST --control-tools
```

### Grok Bot (local execution)

The Grok Bot desktop app runs on home-ubuntu as a separate low-privilege `grokbot` account and uses its local
execution, so no tunnel, public listener or MCP is involved. It and Claude Code (as `postedism`, on the server or
over ssh) run one fixed-path command, `deploy/jobq`, which wraps the existing digest, query, decision and handoff
services as small JSON commands.
The script pins `AUTO_APP_HOME` to the sibling `runtime/` directory, changes into it and starts Python with `-I` (no
`PYTHON*` variables, no current or user directory on `sys.path`), so a caller cannot change the runtime, the working
directory or what Python imports. Through sudo the environment is also reset, apart from sudo's short default list
(locale and time-zone variables such as `LANG`, `LC_*` and `TZ`, and a few terminal and display ones);
`"timezone"` in `runtime/config/handoff.json` keeps `TZ` from moving the day of the daily quota. `jobq` takes no
`--db`, `--preferences` or path argument and never creates or migrates a database. It calls no model or agent and
submits nothing: the owner confirms every decision and every submission. **Deployed; installed and checked on
home-ubuntu on 2026-10-21 (`check.sh`: 0 FAIL), and run end to end on a real job up to the Submit button the same day;
nothing has been submitted yet.** The `grokbot` account's install and
check scripts are in `deploy/grokbot/`. [Guide, in Chinese](docs/grok-bot.md).

| `jobq` command | What it does |
|---|---|
| `new [--limit N]` | List jobs the user has not been told about yet, visa sponsorship first (N is 1–50, default 20; read-only) |
| `seen ID [ID ...]` | Mark jobs as told to the user, so `new` stops listing them (at most 200 job numbers) |
| `search [--query TEXT] [--company TEXT] [--source TEXT] [--state STATE] [--sponsorship SIGNAL] [--limit N] [--cursor CURSOR]` | Search the job library; jobs the user's rules exclude are left out. `STATE` is `not_evaluated`, `recommended`, `un_recommended`, `want_to_apply`, `applied` or `not_applied`; `SIGNAL` is `offers`, `no` or `unknown`; N is 1–200, default 50; `CURSOR` is the `next_cursor` of the previous page |
| `show ID` | One job in full, without its import records: description, apply link, screening, visa signal, history (no `ingestion`: a mailbox import's raw links can carry tracking or sign-in tokens) |
| `want ID --reason TEXT` | Record that the user confirmed they want to apply (the job joins the queue) |
| `skip ID --reason TEXT` | Record that the user decided not to apply |
| `queue [--limit N]` | List jobs that may be applied to now, after quotas, claims and cooldowns (N is 1–100, default 20) |
| `claim ID` | Claim one queued job, always as agent `grok-bot`; returns a token and when the claim ends |
| `packet [TOKEN]` | Everything needed to apply to the claimed job (`profile`, `answers` and `resumes` are empty: the server stores none). The token is read from **stdin** when the argument is left out or is `-`, which keeps it out of the sudo log and the process list; the argument still works |
| `record [TOKEN] OUTCOME [--note TEXT]` | Record the outcome of an attempt, once per claim: `submitted`; `needs_user` or `blocked` (the job waits until the owner requeues it); `failed` (it can be claimed again); `skipped` (counts as not applying). No evidence files. Token on stdin as for `packet` |
| `release [TOKEN]` | Give a claim back without an outcome. Token on stdin as for `packet` |
| `hold [TOKEN]` | The form is filled and waits for the user's go: keep the claim for `hold_hours` (default 24) instead of the lease. `claim ID` on that job then returns a fresh token (`"resumed": true`). Token on stdin as for `packet` |
| `held` | Filled forms waiting for the user's go (held, claim still live), oldest first: `job_id`, `title`, `company`, `apply_url`, `agent`, `lease_until`, `held_since`. The user approves them together ("提交 #a #b #c") |
| `questions [TOKEN]` | Hand back the form's open questions that the package's `answers.md` does not cover, or the user's change requests from the pre-submit check; this uses up the claim and the job waits (`needs_answers`) until the revised package is published. stdin: the token line, then a JSON list of `{"text", "required", "limit", "page", "kind"}` (only `text` is needed; `kind` is `question`, the default, or `change` for the user's words verbatim; at most 60, 64 KiB); with a TOKEN argument, stdin is only the JSON |
| `request ID --text TEXT` | Pass on the user's own words asking for a change to a wanted job's materials while no form is open (one line, 1–1000 characters): the job waits (`needs_answers`) until the revised package is published. Refused while a form is claimed or held (use `questions` with kind `change`), for a blocked job (`blocked`/`action_needed`: the owner requeues it) and for a job that is not `want_to_apply`. A job parked by `needs_user` accepts it: that is how the user's answers to items only they can confirm reach Claude, and the job returns to the queue once the revised package is published |
| `cheatsheet --text TEXT` | Pass on the user's own words with an answer for every application (one line, 1–1000 characters) for Claude to add to the cheat sheet (`apply-kit/answers.md`); the agent never edits it. One job's answer goes through `request` |
| `cheatsheet-requests` | Cheat-sheet requests: `pending`, and the last 20 done ones (`recent`) with `outcome` (`applied` / `declined`) and Claude's `note` |
| `cheatsheet-done ID --outcome applied\|declined --note TEXT` | The owner's step after editing the cheat sheet or declining. Refused through sudo |
| `status ID` | Where one job stands: `decision`, `application`, live `claim` (agent, until, held), `pending` questions and changes, `package` (published, published_at, has_message, `submitted_copy`: when the package was frozen at submission, or null), `runner` (the package runner's `apply-kit/runner.json`), `queue_block` (what keeps a wanted job out of the queue, e.g. `screened_out`, `company_cooldown`, `link_gone`; `daily_limit`; or null) and a one-line `summary`; never a token |
| `answers-needed` | Jobs waiting for answers, each with the latest questions handed back |
| `answers-ready ID [--asked-at SUBMITTED_AT]` | The owner's step once the answers are published: the job returns to the queue. With `--asked-at` (the round's `submitted_at` from `answers-needed`, which the skill's `apply_kit.py answers-ready` always passes), refused when the round changed since, e.g. a change request arrived while the package was being made. Refused through sudo (grokbot); Claude Code runs it as `postedism`, without sudo |
| `kit [ID [FILE]]` | The published application packages in the server's `apply-kit/` folder (the runtime's sibling), with **no claim token**: with no ID, `jobs/index.json`; with ID, that job's `job.json`; with ID FILE, the file itself on stdout, not JSON (`jobq kit 14143 resume.pdf > ~/kit/14143/resume.pdf`). `FILE` is `job.json`, `fields.json`, `jd.txt`, `resume.pdf`, `cover_letter.pdf`, `answers.md` or `message.txt`; a file not published yet is an error |
| `doc [NAME]` | The user's own documents a form may ask for, in the server's `apply-kit/documents/` (e.g. an unofficial transcript), with **no claim token**: with no NAME, the list (`name`, `bytes`, `sha256`); with NAME, the file itself on stdout, not JSON. Names are letters, digits, `.`, `_` and `-`; regular files only, no symlinks, at most 10 MiB (2026-11-19) |
| `guide [cheatsheet\|readme]` | The apply-kit's guide as plain text, not JSON: `cheatsheet` (`apply-kit/answers.md`, the default) or `readme` (`apply-kit/README.md`) |
| `account SITE EMAIL [--existing] [--note TEXT]` | Note that an account exists on a hiring site (never its password); `--existing` means the user made it, not the agent |

`--reason`, `--note`, `SITE` and `EMAIL` are one line of 1–500 characters after trimming, with no control characters
(Unicode categories Cc, Zl and Zp); the `search` text filters allow 200 characters and `--cursor` 4096. `ID` is a whole
number from 1 to 9223372036854775807. `TOKEN` is the value `claim` returned; claim tokens never begin with "-", so they
work as plain arguments. Output is one JSON object, except that `kit ID FILE` and `guide` print the file itself (an
error is still JSON). Exit status: 0 success; 1 the request failed
(`{"status": "error", "error": "…"}`, including database and file errors); 2 bad arguments or configuration. After
`needs_user` or `blocked`, only the owner's `auto-app handoff requeue` puts the job back; after `questions`, the
owner's `answers-ready` does.

```bash
# On home-ubuntu (as postedism); from the Mac, run the same command through ssh home-ubuntu
/home/postedism/Desktop/job-search/auto-application/deploy/jobq new --limit 5
/home/postedism/Desktop/job-search/auto-application/deploy/jobq search --sponsorship offers --limit 10
# ssh joins its arguments into one string: quote the whole remote command when a value has spaces
ssh home-ubuntu "/home/postedism/Desktop/job-search/auto-application/deploy/jobq want 123 --reason 'confirmed in chat'"
# As grokbot (/usr/local/bin/jobq): a published package's file, byte for byte (no token), and the cheat sheet
mkdir -p ~/kit/14143 && jobq kit 14143 resume.pdf > ~/kit/14143/resume.pdf
jobq guide
# Development checkout, without the wrapper (the runtime must already hold a database):
AUTO_APP_HOME=/path/to/runtime uv run auto-app-jobq queue --limit 20
```

The `grokbot` account is set up by the owner on home-ubuntu, with sudo (never by Claude). `setup.sh base` creates the
account (locked password, no extra groups or subordinate IDs, home 750), the sudo rule that allows only the wrapper as
`postedism`, `/usr/local/bin/jobq` and the egress firewall (`grokbot-egress.service`, nftables table
`inet grokbot_egress`: DNS through `127.0.0.53`/`127.0.0.54` and public IPv4 only); `setup.sh app`, after the Grok Bot
`.deb` is installed, runs the app on a hidden Xvfb display with VNC on `127.0.0.1:5990` (reached through
`ssh -N -L 5990:127.0.0.1:5990 home-ubuntu`), started with `--no-sandbox` and without an AppArmor profile that would
allow user namespaces. Both can be re-run. `check.sh` probes every boundary as `grokbot`, each target only after root
has reached it (root itself only reads): each reject rule of the egress table has a probe that only it stops (loopback
listeners and the LAN address; the default gateway, Tailscale's resolver and the Mac on the tailnet; IPv6 to a public
site), and it fails
if the runtime holds a `profile/`, `resumes/` or answer bank. It prints `PASS` / `FAIL` / `REPORT` lines, exiting 1 on
any FAIL; its output holds no secrets. The server's sudo is sudo-rs (classic sudo is also installed); the sudoers
file is written for both.

```bash
sudo bash ~/Desktop/job-search/auto-application/deploy/grokbot/setup.sh base
sudo apt install ./<the Grok Bot .deb>
sudo bash ~/Desktop/job-search/auto-application/deploy/grokbot/setup.sh app   # prints a one-time VNC password: never paste it into a chat
sudo bash ~/Desktop/job-search/auto-application/deploy/grokbot/check.sh
```

### Discord bot

The owner-only bot adds `/jobs`, `/job`, `/status`, `/pause`, `/resume` and decision buttons. Each job notification is
a separate Discord message with that job's buttons directly below its card. In bot mode, the screening worker runs
`--steps screen,enrich,summary,health` and the bot owns digest delivery. [Discord bot setup](docs/discord-bot.md).

```bash
uv run auto-app bot run --db data/auto_app.db --credentials PATH/TO/bot.env
```

Each pushed job also has **Shouldn't recommend** (2026-11-18): a box asks for the reason (1–300 characters), the job
becomes `not_applied`, and the reason is saved in `screening_feedback`. On home-ubuntu, `deploy/screening-feedback.timer`
runs daily at 08:30. With 3 pending reasons, or one 3 days old, it starts `claude -p`, which saves one screening
proposal (`auto-app screen-feedback preview` to try words, then one `propose`, with the usual preview) and closes the reasons no rule can express
(`close`). The bot posts the proposal with **Apply** / **Discard**. Only the owner's Apply changes the rules.
At 24 hours, a proposal expires and its unresolved reasons return to the next summary, even if the owner clicks
Discard before cleanup. A busy screening configuration leaves the buttons available for retry. If the rules revision
changes, Apply requires a fresh proposal and Discard remains available for the current one. Every `screen-feedback`
command prints one JSON object (an error is `{"status": "error", …}`, exit 1).

```bash
uv run auto-app screen-feedback due     # {"due": false, "pending": 1}
uv run auto-app screen-feedback list    # pending reasons with each job's title, company and verdict, and the rules
uv run auto-app screen-feedback preview --changes '{"extra_non_cs_terms": ["...", "field service"]}'   # saves nothing
uv run auto-app screen-feedback propose --changes '{"extra_non_cs_terms": ["...", "field service"]}' --ids 3,5 --note "..."
uv run auto-app screen-feedback close --ids 8 --note "needs a judgment of the track, not a keyword"
```

### Phone control (2026-11-18)

- **Faster pickup.** A changed `want_to_apply`, `jobq questions` and `jobq request` touch
  `$AUTO_APP_HOME/data/apply-queue.kick`. `deploy/jd-apply-queue.path` then starts the package runner at once; the
  hourly timer stays as the fallback. The runner does one more round when a kick arrived during its run (at most 3),
  and writes `apply-kit/runner.json`, which `jobq status` reads.
- **Remote Control.** `deploy/claude-remote.service` runs
  `claude remote-control --spawn same-dir --permission-mode auto --name job-search` in tmux, in `auto-application`
  with `AUTO_APP_HOME` set. The owner talks to it from the Claude app (Code). The first start's two prompts are
  answered in `tmux -L claude-remote attach -t claude-remote`.

```bash
systemctl --user enable --now jd-apply-queue.path screening-feedback.timer claude-remote.service
```


## Application handoff

`handoff` provides an eligible queue, quota-reserving leases, claim-gated JD/profile packets, external
outcome recording, account metadata and resume registration. It does not call an agent/model or submit
forms. Only allowed profile fields are exported; passwords are never accepted. Defaults are 20 per local
calendar day (the host's zone, or `timezone` in `handoff.json`), 3 per company in 30 days, a 30-minute lease and a
24-hour hold for a filled form that waits for the user's go (`hold_hours`); the server's runtime has used
`"daily_submissions": 200` since 2026-11-17. `held` lists the forms waiting for that go, oldest first, so the user can
approve them together; `release` on a held form puts it back to `queued` and clears the hold note (local fix,
2026-11-17, not deployed yet). An agent can hand open questions back (`questions`), and the job waits until the owner's
`answers-ready`. The published packages in `apply-kit/` (the runtime's sibling folder, written by jd-resume-tailor's
`apply_kit.py`) are read with no claim token: `jobq kit` / `jobq guide`, or MCP `get_application_file` /
`get_apply_guide`.
`company_groups` in `handoff.json` makes an employer's other names (e.g. "amazon web services" for "amazon") share
its company limit. Runtime `config/handoff.json` overrides the
[example](config/handoff.example.json). Streamable HTTP handoff requires `--handoff-tools`
and either the Bearer configuration or `--allow-unauthenticated-loopback-handoff` on
the exact `127.0.0.1` bind for Dot's private tunnel. The latter accepts the local Host and
Origin and permits callers on Ubuntu that can reach the port. Dot's private 8767 listener has run it since
2026-11-17 through the override `deploy/auto-app-agent.service.d/50-dot-loopback-handoff.conf`. The owner uses CLI `answers-ready`
after publishing revised answers. [Handoff contract](docs/agent-handoff.md) ·
[Dot rollout and acceptance](docs/dot-setup.md#option-c-private-handoff).

```bash
uv run auto-app handoff queue --limit 20
# Fictional example: reserve a wanted job and use the returned token:
uv run auto-app handoff claim 123 --agent example-agent
uv run auto-app handoff packet CLAIM_TOKEN
uv run auto-app handoff release CLAIM_TOKEN
# Open questions the package does not answer, then the owner's step once the answers are published:
uv run auto-app handoff questions CLAIM_TOKEN questions.json
uv run auto-app handoff answers-needed
uv run auto-app handoff answers-ready 123
# Filled forms waiting for the user's go:
uv run auto-app handoff held
```

## Tracking after you apply

`events` records applied/assessment/interview/rejected/offer/withdrawn timelines. Manual applied reports
and agent outcomes retain distinct sources. Disabled `purpose: updates` mailbox examples recognize
concrete status evidence; ambiguous job matches remain for review and body text is not persisted.
Base MCP includes `record_application_event`; `--control-tools` adds unmatched update review.
[Tracking and mail matching](docs/application-tracking.md).

```bash
uv run auto-app events list 123
uv run auto-app events unmatched
# Writes a reported event; never submits an application:
uv run auto-app events add 123 interview --note "phone screen"
```

## Operations

`ops backup` makes private online SQLite copies of the main DB and an existing optional preparation
ledger, verifies every new archive, then keeps 14 recent daily and 8 older weekly representatives per
DB. Since 2026-11-18 it also archives the apply-kit folder next to the runtime (published packages, frozen submitted
copies, the cheat sheet) as `apply_kit-<UTC>.tar.zst`, verified and pruned the same way; screenshots are not kept. Templates add nightly
backups and service-failure alerts; worker health checks free disk below 2 GiB. Private SSH backup pull
and optional clean-checkout Git deployment are available; rsync remains default. No deployment or real
backup/alert/pull was performed in P2/P3. [Operator procedures](docs/operations.md).

```bash
uv run auto-app ops backup
uv run auto-app ops verify /srv/job-search/runtime/backups/auto_app-20261013T033000Z.db.zst
# Future operation, when authorized:
scripts/pull_backups.sh home-ubuntu
```

---

## Contents
- [Setup](#setup)
- [Key terms](#key-terms)
- [Configuration](#configuration)
- [Common workflows](#common-workflows)
- [Command reference](#command-reference)
  - [Global options](#global-options)
  - [`source`](#source--step-1-setup-where-jobs-come-from) — `list`, `show`, `add`, `login`, `health`, `plan`
  - [`collect`](#collect--step-1a-run-the-searches) — `run`, `probe`, `stats`
  - [`process`](#process--step-1b-postings--jobs) — `parse`, `normalize`, `dedup`
  - [`worker`](#linux-worker-screening-jd-enrichment-and-discord) — `run`, `watch`, `status`, `boards`
  - [`bot`](#discord-bot) — `run`
  - [`screen-feedback`](#discord-bot) — `due`, `list`, `preview`, `propose`, `close`
  - [`jobq`](#grok-bot-local-execution) — `new`, `seen`, `search`, `show`, `want`, `skip`, `queue`, `claim`, `packet`, `record`, `release`, `hold`, `held`, `questions`, `request`, `status`, `cheatsheet`, `cheatsheet-requests`, `cheatsheet-done`, `answers-needed`, `answers-ready`, `kit`, `doc`, `guide`, `account`
  - [`handoff`](#application-handoff) — `queue`, `claim`, `packet`, `record`, `release`, `hold`, `held`, `questions`, `answers-needed`, `answers-ready`, `requeue`, `account`, `resume`
  - [`events`](#tracking-after-you-apply) — `add`, `list`, `unmatched`, `assign`, `dismiss`
  - [`ops`](#operations) — `backup`, `verify`, `prune`, `alert`
  - [`web`](#web--the-app-in-your-browser) — the app in your browser (Jobs; Apply, Audit, Status to come)
  - [`ui`](#ui--every-interactive-part-in-one-app) — Jobs (F1), Audit (F2), Apply (F3)
- [The manager: Claude Code](#the-manager-claude-code) — talk to the app; project skills
  - [`run`, `serve` and `schedule`](#run-and-schedule--all-of-step-1-automatically)
  - [`job`](#job--the-job-pool) — `list`, `show`, `browse`, `mark`
  - [`filter`](#filter--step-2-judge-jobs-with-your-filters) — `list`, `show`, `run`, `why`, `audit`, `eval`, `reparse`
  - [`apply`](#apply--step-3-apply-to-the-jobs-you-queued) — `profile`, `policy`, `queue`, `run`, `inbox`, `list`
  - [`data`](#data--storage) — `inspect`, `export`, `import`, `purge`
- [Where the data goes](#where-the-data-goes)
- [Safety](#safety)
- [Adding a source plugin](#adding-a-source-plugin)
- [Tests](#tests)

---

## Setup

The instructions below initialize a development checkout. Linux runtime configuration and services
are described in [Linux worker](docs/linux-worker.md) and [operations](docs/operations.md).

```bash
uv sync
cp config/searches.example.yaml config/searches.yaml   # what to search — then edit (git-ignored)
cp .env.example .env                                   # global settings + your API key — then edit (git-ignored)
cp -r profile.example profile                          # Step 3: your profile, answers, CV (git-ignored) — then edit
uv run auto-app --help
```

Job sites use your installed **Google Chrome** (fallback: `uv run playwright install chromium`). Company sources on API platforms need no browser. Everything — config, browser logins, the database — stays inside the project folder and is git-ignored.

## Key terms

| Term | Meaning |
|---|---|
| **Source** | Where postings come from. **Each source is one plugin**, named by its plugin name (`linkedin`, `nvidia`, …) — the name used in the config, in storage and with `-s` |
| **Job site** | A source with many employers: LinkedIn (verified), Handshake (unverified), Jobright (disabled — bot check). Built in |
| **Company source** | A source with one employer: a small plugin file in `plugins/companies/` (e.g. `nvidia.py`) that names the company and its board on a **platform** (Ashby, Greenhouse, Lever, Workday — the platform code is shared). `auto-app source add` writes it |
| **Batch** | One `collect run` (or `run`) invocation |
| **Search** | A named search under a source in the config (`sources.<source>.searches`); can copy another source's search with `from: <source>/<search>` |
| **Search run** | One search on one source within a batch |
| **Sighting** | One card / list entry a search run saw, with its outcome: `fetched`, `known`, `filtered`, `budget_skipped`, `fetch_failed`, `cards_only` |
| **Posting** | One listing on one source, saved once. States: `fetched` → `parsed` (or `parse_failed`) → `normalized` → `linked` |
| **Job** | The real opening; one or more postings (from any sources, any locations) linked by dedup |
| **Job state** | Step 2's verdict (`recommended` / `un_recommended`, set by `filter run`) and your decision — `want_to_apply` (queued for Step 3) or `not_applied`, set when you review in `job browse`; `applied` will be set by Step 3. A new job is *not evaluated*. Any state can also be set by hand with `auto-app job mark` |
| **Label** | in `job browse` / `filter audit`, your answer for one job and one filter: should that filter **recommend** this job or **not** (`g` / `b`) — about the job, not a grade of what the filter said. Compared with its verdict, it says whether the filter was right; kept per filter as the training data for its preference model (later) |
| **Audit** | random samples of a filter's "no" jobs that you label, to measure its **noise** — how often it wrongly rejects (`filter audit`, `filter eval`) |
| **Applier** | Step 3: something that applies to one kind of application form. Each platform has an ordered **chain** of appliers (`config/apply.yaml`); when one can't handle a job, the next takes over. A **hand-off** applier (Simplify, manual) gives the job to you to submit |
| **Job TTL** | A job expires `AUTO_APP_JOBS__TTL_DAYS__*` days (in `.env`) after its postings were last seen: jobs a filter judged (recommended or un-recommended) or you decided on are **archived** — kept as training data, never merged with active jobs; jobs never judged are **deleted** (with their postings) |

## Configuration

`config/searches.yaml` (layout: [`docs/step1-collect.md` §2.4](docs/step1-collect.md#24-configuration); template: `config/searches.example.yaml`) says **what to search on each source**. Which board a company source reads and its display name are in its plugin file, not here.

```yaml
defaults:                       # shared by every search
  posted_within_days: 1         # days back; "all" = no date limit (a search can override either way)
  card_filters:
    exclude_title_words: [senior, staff, principal, manager, director]   # skipped before any page is opened

sources:                        # one entry per source (plugin name); a source with no searches doesn't run
  linkedin:
    searches:
      - name: ml-engineer-us    # unique within the source
        keywords: ["machine learning engineer", "applied scientist"]
        locations: ["United States"]
        workplace_types: [remote, hybrid, onsite]
        employment_types: [full_time]
        experience_levels: [entry]          # internship | entry | associate | mid_senior | director | executive
        # site_url: "https://www.linkedin.com/jobs/search-results/?…"   # optional: a search URL copied from the browser
    limits: { max_details_per_hour: 40, max_details_per_day: 300 }

  uipath:                       # plugins/companies/uipath.py
    searches:
      - from: linkedin/ml-engineer-us     # copy that search's fields; the name defaults to the copied one's

  nvidia:                       # plugins/companies/nvidia.py
    searches:
      - name: new-grad
        locations: ["United States"]
        employment_types: [full_time]
        posted_within_days: all
        site_filters:           # this source's own options: `auto-app source show nvidia`
          facets: {"Job Type": ["New College Graduate"], "Job Category": ["Engineering", "Research"]}
```

**Global settings and the API key: `.env`** at the project root (git-ignored; template `.env.example`, which lists every setting with its default). One `KEY=value` per line; nested settings use `__`, maps are JSON on one line. Variables set in your shell win over `.env`; global settings still written in `searches.yaml` work too, with a notice to move them (`.env` wins).

```bash
ANTHROPIC_API_KEY=sk-ant-…                       # Step 2 (`filter run`): console.anthropic.com → API Keys
AUTO_APP_JOBS__TTL_DAYS__UNFILTERED=14           # job TTL in days unseen: never judged → deleted
AUTO_APP_JOBS__TTL_DAYS__UN_RECOMMENDED=7        # … judged → archived (training data)
AUTO_APP_JOBS__TTL_DAYS__RECOMMENDED=30          # … archived
AUTO_APP_SELECT__NOISE_LIMIT=0.05                # evaluation: unlabelled "no" jobs may train a model below this noise
AUTO_APP_RETENTION__RAW_DAYS=7                   # also SIGHTINGS_DAYS, LOGS_DAYS
AUTO_APP_SELECT__PARALLEL=3
AUTO_APP_SELECT__PRICES={"claude-haiku-4-5-20251001": {"input_per_mtok": 1.0, "output_per_mtok": 5.0}}
AUTO_APP_RUN_WINDOW__START=08:00                 # also RUN_WINDOW__END, MIN_HOURS_BETWEEN_RUNS, RETRY__…
AUTO_APP_DEBUG_FILES=false                       # true = files under data/raw/ instead of the database
AUTO_APP_UI__THEME=auto                          # full-screen apps: follow the system's light / dark mode; or a theme name
```

- **Inheritance:** a field left out comes from the search named in `from` (if any), else from `defaults`. `from` copies only the generic fields (keywords, locations, dates, types, levels, pages, card filters) — never `site_url` or `site_filters`. Setting a field overrides the copy; an empty list (`experience_levels: []`) clears it.
- **Empty vs. all:** no `keywords` means no keyword filter (every job matching the other filters), while no `posted_within_days` means the default, not "no limit" — write `posted_within_days: all` for that. On LinkedIn, `all` drops the "date posted" filter; on company boards, every listed job passes the date check.
- **Checked against the plugin:** a field the source can't apply is an error when written in the search itself (remove it — and state it in your Step 2 command if it matters) and a notice when copied with `from`; unknown `site_filters` are errors.
- **LinkedIn searches** run on LinkedIn's newer search page (the one the search bar on linkedin.com/jobs opens), using its **own filters**, never extra keywords:
  - generic fields map onto them when LinkedIn has an exact equivalent: `employment_types` (full_time, part_time, contract, internship), `experience_levels` (entry, director, executive), `workplace_types: [remote]`, `posted_within_days` (rounded up to LinkedIn's 24 hours / week / month), `locations: ["United States"]`;
  - anything else (associate, mid_senior, temporary, hybrid, on-site …) is **not filtered, with a notice in `source plan`** — the whole field, so LinkedIn never filters out values you asked for; state it in your Step 2 command if it matters;
  - `site_filters` sets LinkedIn's filter buttons directly and replaces the generic mapping: `employment_type` (full_time | part_time | contract | internship | volunteer), `experience_level` (LinkedIn's own levels: entry_level | senior | manager | director | executive), `remote`, `easy_apply`, `under_10_applicants`, `in_my_network`, `geo_id` (a location id copied from LinkedIn's URL — needed for places other than the United States);
  - after the exact matches LinkedIn lists "related" jobs that ignore the filters ("…may not be exact matches"); the tool stops reading at that note. Results are ordered by relevance, so there's no early stop: `max_pages` sets the depth.
- **`site_url`** (browser sources) replaces the generated search with a URL copied from your browser; its own filters then apply, not the search's generic fields. For LinkedIn, both the newer `/jobs/search-results/` page and the classic `/jobs/search/` page work (the classic one honours strict filter parameters like `f_E=2` and `sortBy=DD`).
- **Older layouts** (`job_sites:` + `companies:`) still load; a company without a plugin file gets an error with the `source add` command that creates it.

`auto-app source plan` shows what this config will run and where each field is applied (by the site, locally, or not at all — deferred); `auto-app source show <source>` lists what a source supports.

## Common workflows

**First time**
```bash
uv run auto-app source login linkedin                         # log in once in a normal Chrome window; quit it with Cmd+Q
uv run auto-app source add https://jobs.ashbyhq.com/uipath    # writes plugins/companies/uipath.py + a config stub
uv run auto-app source plan                                   # check what will run
uv run auto-app run --max-pages 1                             # a small first run: collect → parse → normalize → dedup
uv run auto-app job list                                      # the result
uv run auto-app job browse --state all                        # … or browse it in a full-screen app
```

**Every day**
```bash
uv run auto-app run --purge                                   # or let the schedule do it:
uv run auto-app schedule install --at 09:00 --at 17:00
```

**Judging jobs with your filters (Step 2)**
```bash
mkdir -p filters && cp -r filters.example/ml-engineer filters/   # then edit filters/ml-engineer/command.md
uv run auto-app filter list                                   # problems / warnings
uv run auto-app filter show ml-engineer --job 28              # exactly what the agent gets for job 28
uv run auto-app filter run --limit 10 --max-cost 1            # a first trial
uv run auto-app filter why 28                                 # how job 28 was judged
uv run auto-app job browse                                    # review (opens on Recommended): decide; per filter: should it recommend the job?
uv run auto-app filter audit                                  # label random "no" jobs: how often each filter misses
uv run auto-app filter eval                                   # precision, noise, recall per filter
uv run auto-app job list --state want_to_apply               # your queue for Step 3
```

**Applying (Step 3)**
```bash
cp -r profile.example profile && cp ~/Documents/cv.pdf profile/cv.pdf   # then fill in profile/*.yaml
uv run auto-app apply profile                                 # what's missing
uv run auto-app ui                                            # Jobs (F1): a on jobs to apply to → Apply (F3): R starts them,
                                                              # answer what's asked, then s submit / f fill only (or hand-offs: o, a)
uv run auto-app apply queue                                   # (or from the command line: the queue …
uv run auto-app apply run                                     #  … and start it)
```

**Checking a new site or company**
```bash
uv run auto-app collect run -s linkedin --cards-only --max-pages 1      # are cards read correctly? (no job pages opened)
uv run auto-app collect probe linkedin "https://www.linkedin.com/jobs/view/<id>/"   # one job page, what was captured
uv run auto-app collect run -s nvidia && uv run auto-app process parse -s nvidia    # coverage per field
uv run auto-app data inspect nvidia <id>                                # raw → parsed → normalized
```

**Debugging a source with plain files (no database)**
```bash
uv run auto-app --debug-files collect run -s nvidia           # writes data/raw/postings/nvidia/<id>/… instead of the database
uv run auto-app --debug-files process parse && uv run auto-app --debug-files process normalize
uv run auto-app --debug-files process dedup --preview         # jobs need the database, so only a preview
uv run auto-app data import                                   # later: load the files into the database
```

**Step by step instead of `run`**
```bash
uv run auto-app collect run
uv run auto-app process parse
uv run auto-app process normalize
uv run auto-app process dedup --preview && uv run auto-app process dedup
```

---

## Command reference

Commands are grouped: `uv run auto-app <group> <command> [options]` — `source`, `collect`, `process`, `job`, `filter`, `data`, `schedule`, `worker`, `bot`, `handoff`, `events`, `ops` — plus `run` on its own. A group by itself (`uv run auto-app job`) shows its commands; every command has `--help`.

```
auto-app run                     all of Step 1          auto-app job      list · show · mark
auto-app schedule                install · show · remove auto-app filter   list · show · run · why · reparse
auto-app source                  list · show · add · login · health · plan
auto-app collect                 run · probe · stats    auto-app data     inspect · export · import · purge
auto-app process                 parse · normalize · dedup
auto-app worker                  run · watch · status · boards   deterministic screening, enrichment and board suggestions
auto-app bot                     run   owner-only Discord digest and decisions
auto-app screen-feedback         due · list · preview · propose · close   Discord reasons → screening proposals
auto-app handoff                 queue · claim · packet · record · release · hold · held · questions · answers-needed
                                 answers-ready · requeue · account · resume
auto-app events                  add · list · unmatched · assign · dismiss
auto-app ops                     backup · verify · prune · alert
auto-app ui                      the TUI                auto-app web      the app in your browser
```

### Global options

Given before the command, e.g. `uv run auto-app --debug-files collect run`.

| Option | Effect |
|---|---|
| `-v`, `--verbose` | debug logging |
| `--debug-files` | **file-only mode**: postings, batches, parse and normalize results are written as browsable files under `data/raw/` **instead of** the database (same as `AUTO_APP_DEBUG_FILES=true` in `.env`). Nothing touches the database. Jobs need the database, so in this mode `process dedup` can only `--preview` (`run` previews its dedup step), and `job`, `data purge`, `data export` and `data import` stop with a message. Load the files into the database later with `auto-app data import` |

Most commands also accept `--config PATH` (default `config/searches.yaml`) and `--db PATH` (default `data/auto_app.db`).

`handoff`, `events`, and `ops` select their database through `$AUTO_APP_HOME`, rather than `--db`;
their operations emit JSON without `--json` and skip the legacy model environment-file loader.

---

### `source` — Step 1 setup: where jobs come from

#### `source list` · `source show NAME`
Lists the sources — one plugin each: built-in job sites and the files in `plugins/` (company sources in `plugins/companies/`) — with their kind, company and platform, plus the platforms available for new companies. Plugin files that failed to load (import error, bad board, duplicate name) are listed with the reason. With a name (a source, or a platform such as `workday`): its company and board, which search fields it supports (native / local / deferred) and its `site_filters`.

```bash
uv run auto-app source list
uv run auto-app source show nvidia
```

#### `source add URL`
Detects a company's job platform from a careers page or job-board URL and test-calls it (prints the number of open jobs). Then it writes the company's plugin, `plugins/companies/<name>.py` (a few lines: name, company, board on the platform base; commit it with your code and edit it for anything company-specific), and adds a commented search stub under `sources: <name>:` in your config. The source runs once you add a search there.

| Option | |
|---|---|
| `--name NAME` (alias `--key`) | source name (default: from the board) |
| `--company TEXT` | the employer's display name (default: from the name) |

```bash
uv run auto-app source add https://jobs.lever.co/palantir --company Palantir
uv run auto-app source add https://adobe.wd5.myworkdayjobs.com/external_experienced --name adobe --company Adobe
```
Marketing careers pages that load jobs with JavaScript (e.g. careers.adobe.com) can't be detected — open any job's *Apply* page and pass that URL.

#### `source login NAME`
Opens a **normal, non-automated Chrome** on the source's saved profile (`browser-profiles/<source>/`) so you can sign in — Google / school SSO work here. Log in, then **quit that Chrome with Cmd+Q** (that's what saves the session); the session is then checked and the page it landed on is printed.

| Option | |
|---|---|
| `--no-verify` | skip the session check afterwards |

```bash
uv run auto-app source login linkedin
```
Output: `linkedin session: ok (checked https://www.linkedin.com/feed/)`. Re-run when a site logs you out (collect stops that site with `needs_login` and notifies you).

#### `source health`
Health of each source: `ok`, `needs_login`, or `cooldown` (after a block, until a time), with the last reason and last successful run.

| Option | |
|---|---|
| `--reset SOURCE` | clear a cooldown / login status by hand |

```bash
uv run auto-app source health
uv run auto-app source health --reset linkedin
```

#### `source plan`
Shows what `collect run` would run: every source, its searches, and for each search field whether it is applied **natively** (by the site), **locally** (on what the site returns) or **deferred** (not filtered — state it in your Step 2 command if it matters); warns about fields a source can't apply. Config errors (unknown fields or sources, bad `site_filters`, bad `from`, a selected source with no searches) are reported here.

| Option | |
|---|---|
| `-s/--source NAME`, `--job-sites`, `--companies`, `--search NAME` | same selection as `collect run` |

```bash
uv run auto-app source plan
uv run auto-app source plan -s nvidia
```

---

### `collect` — Step 1a: run the searches

#### `collect run`
Runs the searches and saves new postings **raw**. For each card: card filters → already saved? (`known`, skipped without opening) → daily ceiling → hourly limit (rate pause) → open the job page / fetch the record → save. Searches stop early after a run of known cards. Starts with a retry pass for previously failed / skipped jobs. Prints the plan first and a batch summary at the end.

| Option | Effect |
|---|---|
| `-s/--source NAME` (repeatable; alias `--site`) | only these sources (any plugin name: `linkedin`, `nvidia` …) |
| `-c/--company NAME` | same as `-s` (kept for old scripts) |
| `--job-sites` / `--companies` | only job sites / only company sources |
| `--search NAME` (repeatable) | only searches with this name |
| `--max-pages N` | cap result pages per job-site search |
| `--cards-only` | read cards and record sightings, open no job pages |
| `--ignore-known` | re-fetch jobs already saved |
| `--interactive` | pause after opening each search so you can set filters in the browser, then collect from that page |
| `--manual-checks` | if a site shows a security check, bring the window forward and wait for you to complete it (headed only) |
| `--capture-all-json` | keep every JSON response a site loads (for investigating a site) |
| `--screenshots`, `--card-html` | keep a screenshot per job page / each card's HTML (off by default) |
| `--no-retry` | skip the retry pass |
| `--headless` | no browser windows |
| `--force` | ignore cooldowns |
| `--scheduled` | enforce the run window and minimum hours between runs (`AUTO_APP_RUN_WINDOW__*`, `AUTO_APP_MIN_HOURS_BETWEEN_RUNS` in `.env`; for automation) |

```bash
uv run auto-app collect run                                # everything in the config
uv run auto-app collect run -s linkedin --max-pages 2
uv run auto-app collect run --companies
uv run auto-app collect run -s linkedin --search nvidia-check
```
How much is fetched: depth is `max_pages`; pacing is `max_details_per_hour` (pause, then continue) and `max_details_per_day` (stop; the rest become `budget_skipped`, fetched first next run). Stop any time with Ctrl+C — everything saved is kept.

#### `collect probe SOURCE JOB`
Fetches **one** job page (browser sources) by id or URL, overwriting any saved copy, and prints what was captured. Not rate-limited; takes a screenshot by default.

| Option | |
|---|---|
| `--no-screenshots`, `--capture-all-json` | |

```bash
uv run auto-app collect probe linkedin 4471589084
uv run auto-app collect probe linkedin "https://www.linkedin.com/jobs/view/4471589084/"
```

#### `collect stats [BATCH]`
Summary of a batch (default: the latest): per source — status, pages loaded, job pages fetched, outcomes, stop reasons, rate pauses, errors; per search run — stop reason, pages, outcomes (`listed` / `not_matching` for companies).

```bash
uv run auto-app collect stats
uv run auto-app collect stats 20261001-105917
```

---

### `process` — Step 1b: postings → jobs

#### `process parse`
Turns raw postings into common fields (title, company, locations, workplace / employment type, salary, posted date, reposted, applicants, apply URL, req id, description …) with each source's plugin parser. Only new postings and those whose parser version changed are parsed; failures are recorded (`parse_failed`) and retried. Prints field coverage per source — the quickest check of what a parser gets right.

| Option | |
|---|---|
| `-s/--source NAME` (repeatable) | only these sources |
| `--reparse` | parse everything again |

```bash
uv run auto-app process parse
uv run auto-app process parse -s linkedin --reparse
```

#### `process normalize`
Computes each posting's dedup keys: normalized company (via the company source, employer board, or aliases) and title, **employer job key** (the same job on Workday / Ashby / Greenhouse / Lever, from LinkedIn's apply link or the company posting's URL), req id, normalized locations, a description hash and MinHash signature. Works on each posting independently. Prints, per source, the share with an employer key and how companies were resolved.

| Option | |
|---|---|
| `-s/--source NAME` (repeatable) | only these sources |
| `--again` | normalize everything again |

```bash
uv run auto-app process normalize
```

#### `process dedup`
Links new normalized postings to **jobs**, incrementally:
1. **Level 1b** — exact same-source copies (reposts, recruiter copies, per-location copies) become duplicates of the earlier posting.
2. **Level 2** — the rest are grouped across sources (same employer key → same company + req id → same company + description overlap ≥ 0.95 → identical description → probable), with vetoes (**different title** — the poster treats differently titled postings as different jobs; different employment type; descriptions overlapping < 0.5; a shared employer key overrides them); each group joins the best-matching existing job, unless a veto with any of its postings blocks it, or becomes a new job.

Existing jobs keep their ids and state; every active job's last-seen and expiry dates are refreshed. Archived jobs take no part (a listing seen again after its job was archived becomes a new job); a new job that shares a matching key with an archived job gets a **repost warning** (⚠ in `job list`: "looks like job #12, which you applied to / declined / Step 2 left out"). Prints new jobs with several postings, postings joining existing jobs, and **near misses** (pairs that looked related but weren't merged — for calibration).

| Option | Effect |
|---|---|
| `--preview` | dry run: show what dedup would do, write nothing |
| `--rebuild` | unlink everything and regroup from scratch — refused once any job is evaluated, decided or archived |
| `--no-near-misses`, `--limit N` | output control |

```bash
uv run auto-app process dedup --preview
uv run auto-app process dedup
```

---

### `web` — the app in your browser

```bash
uv run auto-app web --open                  # http://127.0.0.1:8765 — built from web/ on first run (needs Node)
uv run auto-app web --db data/try.db        # on the test database
```
A web app next to the TUI ([docs/web-frontend.md](docs/web-frontend.md)), on **this Mac only** (127.0.0.1; requests from other hosts or sites are refused). It reads the same database as `serve`, the TUI and Claude Code, and refreshes every 30 s.

- **Jobs:**
  - the TUI's tabs with counts (All, Not evaluated, New recommended, All recommended, Un-recommended, Applied, Not applied, Archived);
  - narrowing: search, source, company, one filter's verdict (recommended it / didn't / couldn't be read), date posted;
  - a table with the job's sources, state, expiry, ⚠ repost warnings and your 👍 / 👎 labels;
  - a detail pane: the job's facts and link, every filter's verdict with its reason and lookups and your label, the postings, the description.
- **Answers** (as in the TUI, through the same code; every one saves at once):
  - **Buttons:** Want to apply / Not applying / Set state… / Reason… on the job; 👍 should recommend / 👎 shouldn't on each filter's verdict; "no filter should".
  - **After each answer:** a toast with "add a reason" and "undo". After want to apply / not applying the next job opens. The answered job stays in the tab (dimmed) until you leave it.
  - **Archived jobs:** labels only.
- **Keys:**
  - **moving:** `↑`/`↓` or `j`/`k` move · `[` / `]` previous / next tab · `/` search · `o` open the job's link · `esc` closes a box;
  - **answers:** `a` want to apply · `n` not apply · `←`/`→` or `1`–`9` pick a filter · `g` / `b` it should / shouldn't recommend · `B` no filter should · `m` set state · `r` reason · `u` undo.
- **The URL keeps the view** (tab, narrowing, the open job), so reloads and bookmarks come back to it.
- **Coming next:** Apply and Status (W3), Audit (W4). Until then use `auto-app ui` or Claude Code for those.

| Option | |
|---|---|
| `--port N` | default 8765 |
| `--open` | open it in your browser |
| `--build` | rebuild the web app first (after changing `web/`); it's built automatically when `web/dist` is missing |
| `--dev` | also run Vite's dev server (hot reload) on port 5173, proxying `/api` to this server; open http://127.0.0.1:5173 |
| `--db PATH` | another database |

### `ui` — every interactive part in one app

The Mac TUI is development tooling; Linux is the intended primary runtime.

```bash
uv run auto-app ui
```
One full-screen app (in your terminal) with a section for each interactive part — switch by **clicking** a section in the top bar, with the **function keys**, or with **`[` / `]`** (previous / next — use these where F-keys are taken, e.g. F3 in VS Code's terminal, or press fn+F1… on a Mac):

| key | section | what it is | also opened by |
|---|---|---|---|
| **F1** (or click, `[` `]`) | **Jobs** | the job pool and your review — [`job browse`](#job-browse) | `auto-app job browse` (with its options) |
| **F2** | **Audit** | random samples of each filter's "no" jobs, with the evaluation panel — [`filter audit`](#filter-audit) | `auto-app filter audit` (which draws samples first) |
| **F3** | **Apply** | Step 3: applying to the jobs you queued — Queued, Needs input, Ready to submit, Hand-offs, … — [`apply inbox`](#apply-inbox) | `auto-app apply inbox` |

- The **bar at the top** shows the sections and what's waiting in each — `Audit (8 left)`, `Apply (5 queued, 2 ready)` — with the current one highlighted.
- Each section **keeps its state** (tab, cursor, search, narrowing) while you're in another, and **reloads when you come back** — a job you queued in Jobs shows up in Apply → Queued right away.
- **The mouse works:** click a section in the top bar, a tab in a section's tab bar, or a row in the list to select it.
- **`?` lists every key** of the current section (in its current tab), with what each does — including the ones kept off the key bar (`tab`, `←` `→` / `1`–`9` pick a filter, `m` set state, `u` undo, `r` reason, `o` open link, `s` / `c` narrow). `Esc` closes it.
- The **key bar** at the bottom shows only the **main keys of the current tab**, then `? all keys · q quit` (each is clickable); `ctrl+p` (the command palette: themes, "Go to Jobs / Audit / Apply") still works. Switching is ignored while a pop-up (e.g. the state menu) is open; in a search or reason box, `[` and `]` are typed as usual.
- Audit stays isolated: only the randomly drawn jobs and audit keys (Jobs' narrowing doesn't affect it).
- If a section can't open (e.g. `config/apply.yaml` doesn't load), it says why instead.

| Option | |
|---|---|
| `--dir PATH` | another filters folder |

---

### `run` and `schedule` — all of Step 1, automatically

Mac launchd scheduling and the browser/model loop are development tooling. The Linux deterministic
worker is documented [above](#linux-worker-screening-jd-enrichment-and-discord).

#### `run`
The whole of Step 1 in one command: **collect → parse → normalize → dedup** (→ purge). Prints a section per stage and any problems (blocked sources, parse failures). Only one run at a time (`data/run.lock`).

| Option | Effect |
|---|---|
| `-s`, `--job-sites`, `--companies`, `--search`, `--max-pages`, `--headless`, `--force`, `--manual-checks` | as for `collect run` |
| `--no-collect` | skip collecting; only process what's saved |
| `--purge` | apply retention at the end |
| `--scheduled` | respect `run_window` / `min_hours_between_runs` and send a notification when done (used by the schedule) |
| `--time-limit 30m` | the round's **time cap per source**, spread across its searches (each gets the time left ÷ the searches left). Ends early once every search is **caught up**, i.e. it reached postings already collected or the end of its results |
| `--max-new N` · `--round-pages N` | the round's caps per source: new postings, result pages |

With a goal, each source's summary says **caught up**, or **not caught up — stopped by time_limit / goal**. The next round finds the rest.

```bash
uv run auto-app run
uv run auto-app run --companies --purge
uv run auto-app run --no-collect
```

#### `serve` — the app running by itself

The Mac launchd `serve` instructions here are for development use.
One long-running loop (design: [`docs/step4-run.md`](docs/step4-run.md)): **collect** (`auto-app run`, until every search is caught up, or the time cap), then **judge the round's new jobs as one batch** (`filter run`), then a **pause counted from the end of collecting**. The next round also waits until the batch is judged, so collecting never outpaces filtering. Rounds only run inside your daily window.

**When filtering can't keep up:**
- **No usage left** (Claude Code, API or DeepSeek limits): judging waits until the reset the error names (otherwise it retries every 15 min), and so does collecting. You get one notice, and it resumes by itself.
- **A job failing 3 times** for a filter that otherwise works becomes **unjudgeable** and stops blocking (`filter unjudgeable` lists them). `filter run --job N` gives it a fresh start.
- **A broken filter** (a config that won't load, a login or API key rejected, an unknown model, or 3 different jobs failing with none working): **you're notified at once and collecting stops** until you fix it and run `serve resume`, which judges the backlog first.

A round with new recommendations sends a notification ("ask Claude Code"). Each round's report goes into `serve status` and `data/logs/serve-rounds.jsonl`, for example `round 09:00–09:10 · linkedin: caught up, 37 new / 6 pages · 41 judged (5 recommended) · next round ≈ 09:40`. State lives in `data/serve.json`; the settings in `config/serve.yaml` (template `config/serve.example.yaml`: window, round cap, pause, jitter, per-source `time_limit` / `every` / `enabled: false`, filter tries, `retention`).

**Cleanup:** expired jobs are archived (or, never judged, deleted) in the round where their date passes. Once a day (`retention: 24h`; `null` = never) a round also drops old raw pages, sightings and logs, as `data purge` would; the report adds e.g. `retention: 120 postings' raw data, 300 sightings, 2 log files dropped`.

| command | |
|---|---|
| `serve run` | the loop in the foreground (`--once`: one round now, or why not). `Ctrl-C` / SIGTERM stops it within 15 s (what's collected is saved). Holds `data/serve.lock`; a manual `auto-app run` during collecting waits for the next round |
| `serve install` · `serve remove` | keep it running in the background (launchd KeepAlive: starts at log-in, restarts after a crash; `--dry-run` shows the plist); its `PATH` includes where `uv` and `claude` are (and `~/.local/bin`); log `data/logs/serve.log` |
| `serve status` | running / paused / stopped, the next round, the last round's report |
| `serve pause [--until 2h \| 09:00 \| 2026-10-07 09:00]` · `serve resume` | no new rounds (judging goes on); resume after a pause, or after fixing a broken filter (a running `serve` judges the backlog at once, then rounds go on) |

```bash
uv run auto-app serve run --once
uv run auto-app serve install
uv run auto-app serve status
uv run auto-app serve pause --until 2h
uv run auto-app serve resume
```

#### `schedule install` · `schedule show` · `schedule remove`
A macOS launchd agent that runs `auto-app run --scheduled --purge` daily at the given times, logging to `data/logs/scheduled.log` (`~/Library/LaunchAgents/com.auto-app.run.plist`).

| `install` option | |
|---|---|
| `--at HH:MM` (repeatable, default 09:00) | run times |
| `--headless` | no browser windows (default: visible — safest for LinkedIn) |
| `--dry-run` | print the plist, install nothing |

```bash
uv run auto-app schedule install --at 09:00 --at 17:00 --dry-run
uv run auto-app schedule install --at 09:00 --at 17:00
uv run auto-app schedule show
uv run auto-app schedule remove
```

---

### `job` — the job pool

#### `job list` · `job show JOB_ID`
**For Claude Code / scripts** (the manager, below): `--json` (both) gives machine-readable output, and `job show --json -d` includes the description, each filter's verdict and your labels. `job list` also narrows with:
- `--since 2h | 1d | 2026-10-06` (with `--state recommended`: recommended since; otherwise first collected since);
- `--posted-within N` (days);
- `--search <text>` (title, company, places, sources);
- `--not-notified` (not yet told about; see `job notified`).

Lists active jobs, newest posting date first, sized to your terminal:

- one row per job: number, posted date, company (as the posting shows it), title, where (`Austin, TX +2`), the sources it was found on (`linkedin, nvidia`), and when it **expires** (`in 12d`; `due` = past its date, archived or deleted at the next collect / judging / decision; `!` = recommended and undecided, archived within 3 days; `queued` = want to apply, never expires while waiting for Step 3);
- a state column appears once jobs are in different states; ⚠ marks a repost warning;
- the summary line counts the jobs found on more than one source.

With a job number, it shows that job:

- company, every location, posted date, first and last seen, apply link, state (with Step 2's and your dates), when it expires and what happens then, and any repost warning;
- each posting (the same opening on each source) with how it joined the job, in words: "first posting of this job", "same job id on the employer's system (1.00)", "repeat of linkedin/… on the same source" …; a posting titled differently from the job shows its title;
- its matching keys (how later postings find this job);
- for an archived job: the description kept with it (judged jobs keep what a filter reads).

| Option | |
|---|---|
| `-s/--source NAME` (repeatable) | only jobs found on this source |
| `-c/--company TEXT` | only jobs whose company contains this text |
| `--state S` | `not_evaluated`, `un_recommended`, `recommended` (undecided), `want_to_apply`, `applied` or `not_applied` |
| `--archived` | archived job records instead of active jobs |
| `--limit N` | only the newest N jobs |
| `-d/--description` (`job show`) | also print the job's **full description** (its primary posting's text, as saved — archived judged jobs keep it) |

```bash
uv run auto-app job list
uv run auto-app job list -s nvidia --limit 20
uv run auto-app job list --state applied
uv run auto-app job list --state want_to_apply
uv run auto-app job list --archived
uv run auto-app job show 28
uv run auto-app job show 28 --description
```

#### `job browse`
The job pool in a **full-screen app** (in your terminal) — and where you **review** what your filters found: decide on each job, and say for each filter whether it should recommend it. Every key saves at once; nothing is submitted anywhere. It's the **Jobs** section (F1) of [`auto-app ui`](#ui--every-interactive-part-in-one-app) — `job browse` opens the app there; F2 / F3 switch to Audit and Apply.

```
 All (64)  Not evaluated (12)  [New recommended (13)]  All recommended (23)  Un-recommended (41)  Applied (2)  Not applied (7)
┌───────────────────────────────────────────────────────────┬─────────────────────────────────────────────────┐
│ ★ 17  RemoteHunter  Data Scientist, …  want to apply  …   │ job #28  Research Scientist, AI Accelerator     │
│   28  NVIDIA        Research Scientist recommended in 14d │   company  NVIDIA · where Austin, TX · …        │
│ ✗ 15  Amazon        Applied Scientist  not applied    👎  │   postings (2) … matching keys …                │
│                                                           │ Filters ──────────                              │
│                                                           │ ▸[1] ml-engineer   recommend · you: 👍 should …  │
│                                                           │     strong ML systems fit; sponsors H-1B        │
│                                                           │     looked up: web_search: NVIDIA H-1B …        │
│                                                           │  [2] research      no · you: —                  │
│                                                           │ Description ──────────                          │
└───────────────────────────────────────────────────────────┴─────────────────────────────────────────────────┘
```

- **Tabs** (`tab` / `shift+tab`): All · Not evaluated · **New recommended** (recommended and not decided yet — the ones to decide on; opens here) · **All recommended** (every job a filter recommended, whatever you decided) · **Un-recommended** (undecided, every filter said no — to catch wrong rejections) · **Applied** · **Not applied**. A job you mark `a` (want to apply) **leaves New recommended at once** and goes to [Apply → Queued](#apply-inbox) — there's no Want to apply tab; `n` moves it to Not applied. `u` undoes; `r` right after `a` / `n` adds a reason to that decision. Archived jobs and audit samples are in [`filter audit`](#filter-audit) / `job list`. Newest first; columns: mark (★ want to apply, ✗ not applied, ✓ applied), number, company, title, posted, state, expires, 👍 / 👎 your labels.
- **Detail pane** (right; scrolls): everything `job show` prints — company, places, dates, apply link, state, expiry, repost warnings, each posting and how it joined the job, matching keys — then **every filter that judged the job**: its verdict, reason, what it looked up, your label (`you: 👍 should recommend → it was right` / `it missed it` / `a bad pick`) (`older version` = judged before you last edited that filter); then the description.
- **Archived jobs** can be labelled (they're training data) but not decided on or changed.
- `e` shows the **evaluation panel** (as in `filter audit`).

| Key | |
|---|---|
| `↑` `↓` | move through the jobs |
| `a` | **want to apply** — queues the job for Step 3 (it never expires while queued). On a job some filter said no to, also labels it **should recommend** for those filters (implied — unless you've labelled it for them yourself) |
| `n` | **not apply** |
| `←` `→` or `1`–`9` | pick a filter (marked ▸) when several judged the job |
| `g` / `b` | that filter **should** / **shouldn't recommend** this job — your answer about the job, whatever the filter said (not "its verdict was good / bad"); a label for that filter's preference model only; labelling again replaces it |
| `B` | **no filter should recommend** it — `b` for every filter on the job, in one key (one undo) |
| `m` | **set any state** — a menu: want to apply, applied, not applied, recommended, un-recommended, not evaluated (same rules as `job mark`) |
| `r` | add a short reason to your last answer on this job |
| `u` | undo your last change (any job, `m` included) |
| `o` | open the job's link in your browser |
| `/` | search title / company / source / job number (`Esc` clears) |
| `s` / `c` | show only jobs from these sources (comma-separated) / whose company contains this text; clear the box for all |
| `e` | show / hide the evaluation panel (`d`, drawing more audit samples, works only in `filter audit`) |
| `q` | quit |

What's saved: your decision on the job (its state, as `job list --state` shows), a `reviews` row per `a` / `n`, and a `feedback` row per (job, filter) with the filter's version and verdict — both keep the job's title, company and description, so they outlive the job's TTL.

| Option | |
|---|---|
| `--state TAB` | open on this tab: `recommended` (New recommended, default — the jobs to review), `all`, `not_evaluated`, `all_recommended`, `un_recommended`, `applied`, `not_applied` |
| `-f/--filter NAME` (repeatable) | only jobs these filters judged, and only their verdicts |
| `-s/--source NAME` (repeatable) | start narrowed to these sources |
| `-c/--company TEXT` | start narrowed to this company |
| `--dir PATH` | another filters folder (for the "older version" marks) |

```bash
uv run auto-app job browse                              # review what filter run recommended (opens on Recommended)
uv run auto-app job browse -f ml-engineer
uv run auto-app job browse --state all
uv run auto-app job browse --state all_recommended
uv run auto-app job browse -s nvidia
```
`job list` / `job show` stay plain text (for scripts and pipes).

**Colors:** by default the app **follows your system's light / dark mode**, and switches while open when you change it (macOS, Linux/GNOME, Windows). Set in `.env`:

| Setting | |
|---|---|
| `AUTO_APP_UI__THEME` | `auto` (default) or a fixed Textual theme: `textual-light`, `textual-dark`, `ansi-light` / `ansi-dark` (your terminal's own colors), `solarized-light`, `catppuccin-latte`, `nord`, `dracula`, `tokyo-night`, … (an unknown name lists them all) |
| `AUTO_APP_UI__LIGHT_THEME` / `AUTO_APP_UI__DARK_THEME` | what `auto` uses in light / dark mode (default `textual-light` / `textual-dark`) |

`ctrl+p` → "theme" in the app tries a theme for that session.

#### `job mark JOB_ID... --as STATE`
Sets jobs' state by hand (in the app: `m` in `job browse`) — normally `filter run`, your review in `job browse` and (later) Step 3 do it: `recommended`, `un_recommended`, `want_to_apply`, `applied`, `not_applied` (the last three keep Step 2's verdict, or mark the job recommended if it has none) or `not_evaluated` (clears it). The state decides the job's TTL (`AUTO_APP_JOBS__TTL_DAYS__*` in `.env`), so e.g. marking a job `applied` keeps it 30 days after it was last seen and then archives it; a `want_to_apply` job never expires while it waits for Step 3. Archived jobs can't be changed.

```bash
uv run auto-app job mark 28 --as applied
uv run auto-app job mark 31 --as want_to_apply
uv run auto-app job mark 12 13 14 --as un_recommended
uv run auto-app job mark 41 --as not_applied --reason "it's in NYC"
```
Output: one line per job with its new state and expiry date. `want_to_apply` / `not_applied` are recorded as **your decision**, exactly like the app's `a` / `n`: with each filter's verdict, and `--reason` as the app's `r`. So the filters learn from it.

#### `job notified JOB_ID...`
Marks jobs as told about (the manager's digest): `job list --not-notified` leaves them out from then on.

---

### `filter` — Step 2: judge jobs with your filters

A **filter** is a folder in `filters/` (git-ignored) that says how you want jobs judged — design: [`docs/step2-select.md`](docs/step2-select.md). You can have several. Start from an example:

```bash
mkdir -p filters && cp -r filters.example/ml-engineer filters/
```

| File | |
|---|---|
| `command.md` (required) | the prompt run for **every** job, in your own words: background, what you want, deal-breakers, when to recommend. `$ARGUMENTS` marks where the job details go (appended at the end if missing). Optional front matter: `description` only. Unless the filter has a `parser.py`, it must make the agent end with **our output convention**: `{"fit": true \| false, "dealbreaker": true \| false, "reason": "…"}` — **fit**: does the job meet your requirements (if unsure, true); **dealbreaker**: does one of your deal-breakers clearly apply (if unsure, false). The filter recommends a job that fits with no dealbreaker. (The older `{"recommend": …, "reason": …}` still works; `filter list` suggests updating.) |
| `filter.yaml` (required) | `job_details` to send (`title, company, locations, workplace_type, employment_type, salary, posted_at, reposted, description`; default the first three + description), `max_description_chars`, the agent's `model`, `tools` (`web_search`, `web_fetch`), `max_turns`, `max_tool_calls`, and `applies_to` (sources or `source/search`; omit = every job) |
| `parser.py` (optional) | `parse(answer) -> {"fit": bool, "dealbreaker": bool, "reason": str}` (or the older `{"recommend": bool, "reason": str}`) for a command with its own output format (see `filters.example/verdict-format/`) |
| any other files | notes the command refers to (e.g. `sponsorship-notes.md`); the agent may read files in its own folder. Under 200 KB each |

A filter's **version** is a hash of its folder name, its command, its other files and the agent settings in `filter.yaml` (not `parser.py`, not `applies_to`); judgments are stored per version, so copied filters have independent results and editing a command marks old judgments out of date. Versions from before name scoping remain readable as historical judgments; `--rescore` refreshes them explicitly.

#### `filter list` · `filter show NAME`
Lists the filters: version, model, tools, how the answer is read (output convention or `parser.py`), which jobs each judges, its description, and any problem (✗ — e.g. an `applies_to` naming no configured source or search) or warning (! — e.g. a command that doesn't mention the output convention, still asks for the older `{"recommend": …}` answer, or has no `$ARGUMENTS`). Folders that don't load are listed with the reason (file and field). With a name: that filter's settings and files; with `--job`: **exactly the text the agent would get for that job** — the command with the job details in place of `$ARGUMENTS`, inside a block marked as data from the posting (legal boilerplate removed, original casing kept, long descriptions cut and marked).

| Option | |
|---|---|
| `--job N` | with a name: print what the agent gets for job N |
| `--dir PATH` | another filters folder (e.g. `filters.example`) |

```bash
uv run auto-app filter list
uv run auto-app filter show ml-engineer --job 28
uv run auto-app filter list --dir filters.example
```

#### `filter run`
Runs each filter's agent on **active, undecided** jobs it hasn't judged yet and sets each job's Step 2 state. Per job and filter:

1. the command is run with the job's details (what `filter show NAME --job N` shows), by an agent that may use the filter's `tools` — web search / web fetch (**linkedin.com blocked**) — and read files in its own folder, within `max_turns` / `max_tool_calls` (then it must answer with what it has);
2. its answer is read with the output convention (or `parser.py`): **recommend** (fits, no dealbreaker), **no — dealbreaker**, **no — doesn't fit**, or **couldn't read**;
3. the job becomes **recommended** if any filter that judged it recommends it or its answer couldn't be read (high recall — you see it), **un_recommended** if all said no. A failed run (API error) leaves the job undecided and is retried on the next `filter run`. Jobs you decided on (`want_to_apply` / `applied` / `not_applied`) are never changed.

Every answer is stored with the trace (each lookup), tokens, tool calls and cost. A filter's later edit (new version) doesn't re-judge anything unless you pass `--rescore`.

| Option | |
|---|---|
| `-f/--filter NAME` (repeatable) | only these filters |
| `-j/--job N` (repeatable) | only these jobs — judged again even if already judged; a job you already decided on is judged too (see `filter why`), but its state doesn't change; each filter's `applies_to` still applies |
| `--limit N` | at most N jobs, newest first |
| `--rescore` | also re-judge jobs judged by an older version of a filter |
| `--max-cost USD` | stop starting new runs once this much was spent (runs already going finish). With the `api` runner it needs prices for the filters' models in `AUTO_APP_SELECT__PRICES`; with `claude-cli` it uses Claude Code's reported cost |
| `--parallel N` | agent runs at once (default `AUTO_APP_SELECT__PARALLEL`, 3) |
| `--dry-run` | list what would be judged; run nothing |
| `--stream` | watch each agent work live: a header per job (`── [3/30] #28 title · filter`), its text as it's written, each lookup (`→ WebSearch: …`) and its result (`✓ …`), then the verdict line. Runs go one job at a time (so the output stays readable) |
| `--dir PATH` | another filters folder |

**Who runs the agent** (`AUTO_APP_SELECT__RUNNER` in `.env`, default `auto`):
- **`api`** — the Claude API with `ANTHROPIC_API_KEY` (from `.env` or your shell); billed per token; cost from `AUTO_APP_SELECT__PRICES`. Full trace of every lookup.
- **`claude-cli`** — your **Claude Code login** (`claude -p`, headless), no API key: usage counts against your Claude subscription. Each run is **a brand-new Claude Code session with no context from earlier runs** — a fresh session id, nothing saved, none of your Claude Code settings or memory, in a new temporary folder (deleted afterwards) holding only copies of the filter's own files, so no `CLAUDE.md` or other project file can be picked up — locked down to the same abilities: only WebSearch / WebFetch / Read exist, anything not allowed is denied (`dontAsk`), Read is limited to the folder (not `command.md`, `filter.yaml`, `parser.py`), WebFetch to linkedin.com is denied, none of your Claude Code settings, hooks, MCP servers or commands are loaded, and nothing is saved as a session. The cost shown is Claude Code's list-price equivalent. `filter why` shows each lookup, as for the API runner (read from Claude Code's event stream).
- **`auto`** — `api` when `ANTHROPIC_API_KEY` is set, otherwise `claude-cli`.
- **DeepSeek** — chosen per filter by its model: a filter with `model: deepseek-chat` (or any `deepseek-…`) runs on DeepSeek's API with `DEEPSEEK_API_KEY` (in `.env`), whatever the setting above. Same loop, budget and output; `read_file` as usual; `web_fetch` is done by the app (linkedin.com and local / private network addresses refused, every redirect checked, 2 MB / 20 s limits, pages as plain text); **`web_search` isn't available** with DeepSeek (`filter run` says so and runs without it). For cost tracking and `--max-cost`, add the model's price to `AUTO_APP_SELECT__PRICES`. Filters on different providers can run in the same `filter run`.

`filter run` prints which runner it uses. No runner (or key) is needed for `--dry-run` or `filter reparse`.

```bash
uv run auto-app filter run --dry-run --limit 30
uv run auto-app filter run --limit 30 --max-cost 2      # a first trial
uv run auto-app filter run -f ml-engineer --rescore      # after editing that filter's command
uv run auto-app filter run --job 28                      # just job 28 (again, if already judged)
uv run auto-app filter run --job 28 --stream             # … and watch the agent work
```
Output:
- **Progress while it runs.** First, what will run (`judging 30 runs on 30 jobs, 3 at a time`). Then one line per finished judgment, numbered: `[ 7/30] #28 ml-engineer recommend — reason` (or `no — dealbreaker — …` / `no — doesn't fit — …`). In a terminal, a **live bar** stays at the bottom: done / total, cost so far, elapsed and estimated time left, and which jobs are running now with how long each has taken (`running #28(41s) #31(12s)`). With `--stream`, or when the output is piped or logged, there's no bar; the `[n/N]` lines remain, and `--stream` puts them in each job's header too.
- **At the end:** how long it took, then totals — recommend / no (how many for a dealbreaker) / couldn't read / errors, how many jobs are now recommended or un_recommended, tokens and cost — and the command to review them (`job browse`).

#### `filter why JOB_ID`
Why a job was or wasn't recommended: for each filter that judged it — its version (current, out of date, or filter removed), model, verdict and reason (with its two parts: `fit: yes · dealbreaker: no`; "not given" for older answers), how the answer was read (and why not, if it couldn't be), tokens / tool calls / cost, every lookup it made (`web_search`, `web_fetch`, `read_file` with their inputs), and the full answer. `--trace` adds every model turn and tool result.

```bash
uv run auto-app filter why 28
uv run auto-app filter why 28 --trace
uv run auto-app filter why 28 --json      # for Claude Code / scripts
```

#### `filter label JOB_ID (--should | --shouldnt) [-f FILTER] [--reason …]`
Your label for a filter's verdict on a job, the app's 👍 `g` / 👎 `b` from the command line: should that filter recommend this job? Without `-f`, it labels every filter that judged it (`--shouldnt` = "no filter should", the app's `B`). Labels drive `filter eval` and the audit.

```bash
uv run auto-app filter label 41 -f ml-engineer --shouldnt --reason "sales, not engineering"
uv run auto-app filter label 57 --should
```

#### `filter audit`
Measures how often each filter **wrongly rejects** — its **noise** — by having you label **random samples** of its "no" jobs. Opens [`auto-app ui`](#ui--every-interactive-part-in-one-app) on its **Audit** section (F2): **only the Audit tab** — the randomly drawn jobs, nothing else (labelling other jobs here would bias the estimate) — with the **evaluation panel** at the top, recomputed after every label.

```
 [Audit (8 left of 30)]
 ml-engineer  (as used)  judged 89 · picks 20 (review load 22%) · no 69
   precision      78% (61–89%)     23 of its picks labelled, 18 should be recommended
   noise          5% (1–22%)       22 audited, 1 missed → ≈3 missed in all (1–15) of 69 said no
   recall         ≈84% (49–94%)
   training data  NO — noise could be 20% > 5% limit (~71 more clean audits); your labels only
┌──────────────────────────────────────────────┬──────────────────────────────────────────┐
│ ? 141  Acme      Account Executive, East     │ job #141  Account Executive, East        │
│ ✓ 122  Stripe    Data Engineer               │ … ▸[1] ml-engineer  no · you: —          │
│ ✗ 118  Datadog   ML Platform Engineer        │                                          │
```

- **Drawing:** for each filter (or `-f` ones) whose last sample is fully labelled, it draws `-n` jobs (default `AUTO_APP_SELECT__AUDIT_SIZE`, 30) uniformly at random from the filter's **pool**: jobs whose latest verdict from it is "no" — active and archived — that you haven't labelled for it and that aren't in one of its samples already. Each draw is stored with the pool's size, so a sample can't be cherry-picked. `d` in the app draws `AUTO_APP_SELECT__AUDIT_MORE` (10) more per filter.
- **Labelling:** for the picked filter (`←` `→` / `1`–`9`): `g` it **should** have recommended the job (a miss) / `b` it **shouldn't** (it rightly said no); `B` = no filter should — most audited jobs are obvious rejections. After a label the cursor moves on by itself: to the job's next unlabelled filter (when several filters drew it), else to the **next job still to label**. Marks: `?` still to label, `✓` no sampled filter should recommend it (they rightly said no), `✗` one should have (a **miss**). `u` undoes.
- **The panel** (`e` hides it): see [`filter eval`](#filter-eval).
- **Only audit keys:** the footer lists `g` · `b` · `B` · `←→` filter · `u` undo · `r` reason · `o` open link · `/` search · `d` draw more · `e` evaluation · `q` quit. Deciding on jobs (`a` / `n`), setting states (`m`) and narrowing (`s` / `c`) belong to `job browse` and do nothing here.
- Only labels on **sampled** jobs measure the noise: a "no" job you labelled elsewhere (e.g. in Un-recommended) wasn't picked at random.

| Option | |
|---|---|
| `-f/--filter NAME` (repeatable) | only these filters |
| `-n N` | jobs to draw per filter (default 30) |
| `--dir PATH` | another filters folder |

```bash
uv run auto-app filter audit
uv run auto-app filter audit -f ml-engineer -n 60
```
With no miss found, **59** audits show the noise is below 5% (one-sided 95%); each miss found raises that — 1 → 93, 2 → 124, 3 → 153 ([docs/study.md](docs/study.md) has the worked example).

#### `filter eval`
How well each filter judges, **against your labels** — the same numbers as the audit panel, printed. Per filter:

| | from | meaning |
|---|---|---|
| precision | your labels on its picks (recommend, or couldn't read) | the share of its picks it should have recommended |
| noise | your labels on its audited "no" jobs | the share of its rejections it should have recommended (misses) |
| missed in all | noise × the jobs it said no to | an estimate of the jobs it wrongly hid |
| recall | right picks ÷ (right picks + missed in all) | the share of good jobs it finds — the number that matters most |
| review load | picks ÷ judged | how much it asks you to read |
| by cause | its "no"s split into dealbreaker and "doesn't fit" rejections, with the audited misses of each | a wrong dealbreaker call hides a job however well it fits (shown once a filter reports dealbreakers) |
| training data | the noise's **one-sided 95% exact** upper bound vs `AUTO_APP_SELECT__NOISE_LIMIT` (5%) | YES: its unlabelled "no" jobs may train its preference model; NO: how many more clean audits would get there |

Ranges are 95% Wilson intervals. Nothing is cached — every label changes the numbers at once.

| Option | |
|---|---|
| `-f/--filter NAME` (repeatable) | only these filters |
| `--all-versions` | also each filter version on its own (its own verdicts and the audits drawn while it was current) |

```bash
uv run auto-app filter eval
uv run auto-app filter eval -f ml-engineer --all-versions
```

#### `filter unjudgeable`
Lists the jobs a filter gave up on (they failed 3 times; `serve` marks them), each with the filter, the tries and the last error. `--json` for Claude Code / scripts. `filter run --job N` gives one a fresh start.

```bash
uv run auto-app filter unjudgeable
```

#### `filter reparse`
Re-reads the stored answers with each filter's **current** parser — e.g. after adding or fixing a `parser.py` — and updates the verdicts and the jobs' Step 2 state. No agent runs, no cost (the raw answers are kept).

| Option | |
|---|---|
| `-f/--filter NAME` (repeatable) | only these filters |
| `--dry-run` | only list the verdicts that would change |
| `--dir PATH` | another filters folder |

```bash
uv run auto-app filter reparse --dry-run
uv run auto-app filter reparse
```

---

### `apply` — Step 3: apply to the jobs you queued

> **Automated applying is off for now (2026-10-05).** The pipeline collects, filters and recommends. Jobs you mark `a` (want to apply) go to the **Apply section's Queued tab** (F3), and **you apply by hand**:
> - `enter` opens the apply link in your browser and your CV in Finder;
> - `a` marks it **applied**;
> - `d` marks it **not applying**.
>
> Both then show in Jobs (F1) → Applied / Not applied. `apply run` only says so. Everything below (applier chains, the browser agent, trial runs, fill only / submit, site accounts) is kept, and comes back with `AUTO_APP_APPLY__AUTOMATED=true`.

Works through your **want to apply** queue (jobs you marked `a` in `job browse`) — design: [`docs/step3-apply.md`](docs/step3-apply.md). Each job's platform (from its apply link: `greenhouse`, `lever`, `ashby`, `workday`, `linkedin_easy_apply`, `other`) picks an **applier chain** from `config/apply.yaml`; appliers are tried in order, and when one can't handle the job (or isn't built yet), the next takes over:

| applier | status | how |
|---|---|---|
| `simplify_auto` | ✅ — not in the default chains since 2026-10-05 (the agent took its place); name it in `config/apply.yaml` to use it | **automated Simplify**: Playwright's Chromium with your Simplify Copilot extension (copied from your Chrome) and a dedicated profile you log in to once (`apply login simplify`). Starting a job opens the form **headless**, clicks "Start Application" on a description page (or opens a form embedded from an ATS directly), answers Simplify's one-time cookie notice ("Essentials only" — `AUTO_APP_APPLY__SIMPLIFY_CONSENT`), clicks **Autofill This Page**, fills what Simplify left empty from your profile / answers, and reads the form back (below). Submitting does the same in a browser, then submits (or fill only). When Simplify's panel doesn't appear (not logged in, no form, panel changed), the next applier takes over. Fills from **your Simplify profile** — Simplify's terms may not allow automated use: your choice, your account |
| `simplify` | ✅ | **hand-off**: opens the apply link in **your Chrome**, where the [Simplify](https://simplify.jobs) extension autofills the form; you check, submit, then press `a` in `apply inbox` |
| `manual` | ✅ | **hand-off**, always last: opens the apply link and shows your CV in Finder; you apply, then press `a` |
| `greenhouse` | ✅ | **automated**: reads the job's questions from Greenhouse's public API, answers them (below), asks you what's left (**Needs input**); once complete it's **Ready to submit** — you check every answer, then the app fills the form in a browser (CV first, then text and dropdowns) and submits it, or fills it for you to submit. A closed posting stops (no fallback); a form it can't fill falls back to Simplify |
| `agent` | ✅ — **first in every default chain** (after the Greenhouse API applier on Greenhouse; never on LinkedIn Easy Apply). Starting a job: a **trial run** through every step, stopping before the final submit. `f`: the same in a visible window, left on the last step for you. `s`: the same, then the final submit and the confirmation check (no confirmation → Action needed, never a second applier) — [`docs/step3-agent.md`](docs/step3-agent.md) | a **Claude browser agent** (Opus 5.5, your Claude Code login) that works out any form: generic browser tools as its hands, our code for the rules — never submits outside `s`, never clicks Next in a dry run, stays on the application, fills sensitive questions only from `answers.yaml`; creates / signs in to site accounts with your email and Keychain password (never shown to the model); a time limit per run |
| `lever`, `ashby`, `workday` | planned | automated, like greenhouse (A4–A6); until built, chains skip them (recorded as "not built yet") |

LinkedIn Easy Apply is never automated. The app submits only when you press `s` on a ready application (auto-submit comes with A5).

**Starting a job is a trial run** (`enter` / `R` / `r` in Apply, `apply run`): the automated applier fills the real form in a headless browser and **reads it back** — the browser agent goes through **every step** (signing in / creating the account if the site asks; each step is saved on the site as a draft) and **stops before the final submit**, so Ready to submit means it can be submitted; only `s` submits. On a **multi-step form** (Workday, Adobe's careers site, …) only the first step: its Next button sends the step to the site, so the later steps are filled when you submit (`s`, or `f` in a window) — a new question there brings the job back to Needs input with your answers kept — every field, its label, whether it's required, what's in it. Nothing is submitted. A required field still empty (one the API didn't list, or an answer that didn't take — e.g. not one of the dropdown's options) becomes a question for you → **Needs input**; when every required field is filled → **Ready to submit**. Each answer is recorded with where it came from (`simplify`, `form`, profile, `bank:<key>`, claude, you). Forms without `<label>`s (Rippling) are read by the question text around each field; a dropdown's own search box isn't a question.

**How questions are answered** (Greenhouse, and later the other automated appliers):

| kind | answered from |
|---|---|
| standard — name, email, phone, CV, country, LinkedIn / GitHub / website, city, address, state, postal code | `profile.yaml` (`location:` holds address1 / address2 / city / state — full name — / postal_code / country) |
| known — start date, relocation, "interviewed here before?" … | `answers.yaml`: the same question (ignoring case / punctuation, `{company}` / `{title}` filled in), else **Claude** picks the entry that means the same, with a confidence (below `AUTO_APP_APPLY__MATCH_THRESHOLD`, 0.9, it goes to you) |
| **sensitive** — work authorization, sponsorship, citizenship, demographics, legal agreements / consent | **only** an `answers.yaml` entry (exact, or picked by Claude above the threshold) — never drafted, never guessed |
| free text — text areas ("Why us?") | a saved free-text answer (`kind: free_text`), else **drafted by Claude** from your profile and the job — marked ✎ and shown to you in review |
| dropdowns | the answer mapped onto the listed options (exact, or yes / no) |

Optional questions with no answer are left blank, never asked. Claude runs through your **Claude Code CLI** login (`AUTO_APP_APPLY__MODEL`, default `claude-sonnet-5`): one call per application, no tools, a fresh session, nothing saved, `ANTHROPIC_API_KEY` not used. Without a working profile (`apply profile` reports problems), the automated appliers are skipped and jobs go to Simplify.

**Your files** (git-ignored; templates committed):

| File | |
|---|---|
| `profile/profile.yaml` | name, email, phone, location, links, education, experience, `cv:` path, `accounts:` (the email the agent signs in / up with on sites like Workday — default your email — and `accept_terms`, whether it may tick a site's terms box when creating an account) — template `profile.example/profile.yaml` |
| `profile/answers.yaml` | your **answer bank**: work authorization, sponsorship, start date, relocation, salary, demographics, saved free-text answers (`kind: free_text`, `{company}` / `{title}` filled in) — template `profile.example/answers.yaml`. Used by the automated appliers; answers you give in the inbox can be added to it (`S`) |
| `profile/cv.pdf` | your CV, uploaded as is |
| `config/apply.yaml` | the **apply policy**: `workers`, `pace` (per hour / day / company, hours window — for automated submission), `order` (`queued` / `newest_posting`), `appliers` (the chain per platform; `"*"` for any other), `submit` (`review` / `auto` per automated applier), `auto_drafts`, `notify` — template `config/apply.example.yaml`; without it, the template's defaults apply |

#### `apply profile` · `apply policy`
Check your files. `apply profile`: your name and email, the CV, how many answers are saved, problems (✗ — a missing file, invalid YAML, an answer without `question` / `answer`, the CV not found; exit code 1), and the common answers still missing (you'll be asked when an application needs them). `apply policy`: the effective policy and each platform's chain with ✓ (built) / … (not built yet), and problems: unknown appliers or platforms, a chain not ending in `manual`, LinkedIn Easy Apply with an automated applier.

#### `apply account set` · `apply accounts`
Site accounts for the browser agent (Workday and other sites that need one): it **signs in**, or **creates an account**, with the email and password **you** provide.
- **In `profile/profile.yaml`** (git-ignored):
  ```yaml
  accounts:
    email: you@mail.com          # default: your email
    password: "…"                # for signing up everywhere
    passwords: { nvidia.wd5.myworkdayjobs.com: "…" }   # a site where you already have an account: sign in there
    accept_terms: true           # may tick a site's terms box when creating an account
  ```
  The `accounts:` section is **never sent to Claude** (the question matcher leaves it out).
- **Or the macOS Keychain:** `apply account set` asks for the password twice and saves it (service `auto-app`). `--site HOST` sets one site's own. `profile.yaml` wins when both are set.
- The agent types it with its `fill_secret` tool, so Claude never sees it, and it isn't logged.
- **Sign in or sign up:** where a site needs an account, the agent **signs in only with account info for that site**: an account on record (`apply accounts`), or a password you set for it with `--site`. **Otherwise it signs up** with your email and password. If the site says the email is already registered, it signs in with the same email and password instead; a wrong password → Action needed. It does this whenever a site asks, including the trial run. The job's note says "created an account on …" or "signed in to …".
- **What it needs from you:** at least a valid **email** and a valid **password**. `apply account set` refuses a password a sign-up would reject (it needs 8+ characters, upper and lower case, a number and a symbol). Without either, the agent doesn't try, and the job waits in Action needed ("set …"). `apply profile` shows whether both are set.
- Email verification goes to **Action needed**: verify, then `r`.

`apply accounts` lists every account the agent created or signed in to (site, email, created / signed in, last used, e.g. "verification pending") and whether a password is set.

```bash
uv run auto-app apply account set
uv run auto-app apply account set --site nvidia.wd5.myworkdayjobs.com
uv run auto-app apply accounts
```

#### `apply login simplify`
Opens the browser `simplify_auto` uses — Playwright's Chromium with the Simplify Copilot extension copied from your Chrome (the newest installed version; recopied when Chrome updates it) and a dedicated profile in `browser-profiles/simplify/` (git-ignored). Log in to Simplify there once (the window hides the automation markers Google checks, so "Continue with Google" usually works — otherwise use your email), click the extension's icon to check you're logged in, then close the window. If a window on that profile is still open, the command says so (quit it first). Needs `uv run playwright install chromium` (branded Chrome no longer loads extensions under automation).

#### `apply queue`
The jobs you queued, in the policy's order: number, company, title, platform, chain, and the application's status (`not started`, `handoff (simplify)`, `failed`, …).

#### `apply run`
Starts an application for each queued job without one, and moves it through its chain to the first applier that can take it: the form is filled headless and read back (Simplify autofill / Greenhouse API answers; questions answered with at most one Claude call each), landing in **Needs input** or **Ready to submit**; jobs no automated applier can take become **hand-offs**. Refuses while `apply profile` reports problems. A failed application isn't restarted by `run`: retry it in the inbox.

| Option | |
|---|---|
| `-j/--job N` (repeatable) | only these jobs |

#### `apply inbox`
Step 3 — the **Apply** section (F3) of [`auto-app ui`](#ui--every-interactive-part-in-one-app); `apply inbox` opens the app there. **With automated applying off (the default) it's just the Queued tab: `enter` opens the link and your CV, `a` applied, `d` not applying.** With it on (`AUTO_APP_APPLY__AUTOMATED=true`), all of the following: **`R`** (any tab) **starts the jobs you queued** that haven't started — the same as `apply run`, in the background; the tab bar says how many are waiting ("2 queued, not started — R starts"). Starts run one at a time (about a minute each — one Simplify browser), and `s` / `f` wait for the job being started (and starts wait while a fill-only window is open): `enter` / `r` / `R` pressed while one is running **waits its turn** and starts automatically after it. **Progress**: the status line at the bottom says which job is being filled and how many are done ("starting job 22 — … 1 done, 4 more after it", then "job 22 → Needs input · 2 done, 3 to go"); in Queued, the job being filled says *starting…*, the rest *next* / *waiting*; each finished job appears in its tab right away (the tab counts go up). Quitting never waits for it (what didn't start stays queued); while a form is being submitted, `q` warns once — press it again to quit anyway. A tab each (`tab` switches; the key bar shows that tab's main keys, `?` all of them):

| tab | keys |
|---|---|
| **Queued** — jobs you marked `a` (want to apply), not started yet; they appear here right away | `enter` **start this one** · `R` start all · `d` take it out of the queue (not applied) · `o` open the link |
| **Needs input** — questions only you can answer | `enter` **answer** the question marked ▸ — a dropdown / radio / checkbox opens a **list to pick from** (↑↓ `enter`; a checkbox: *Yes — check it* / *No — leave it unchecked*; a multi-select: `space` ticks, `enter` saves), anything else a text box; after each answer the **next open question opens by itself** — this job's, then the next job's (Esc stops) · `A` answer it for **every** job waiting with the same question · `S` the same, and **save** it to `answers.yaml` · `n` next applier · `o` open the link · `d` drop |
| **Action needed** — a CAPTCHA, a sign-in or an email verification | `r` **start over** (e.g. after you verified your email, or set an account password) · `f` fill only / `s` submit in a visible browser (solve it there) · `n` next applier · `o` · `d` |
| **Ready to submit** — every question answered | the detail lists every answer with its source (profile / `bank:<key>` / claude / ✎ draft / you) — `s` **submit** (fills the form in a browser and submits; the job becomes `applied` on the confirmation page) · `f` **fill only** (a visible browser, filled; you check, submit, close it; then `a`) · `e` edit an answer (`3: new answer`; a choice question: just `3`, then pick) · `a` applied · `n` next applier · `o` · `d` |
| **Hand-offs** — oldest first | `o` open (Simplify: in your Chrome; manual: the link + your CV) · `a` **applied** (the job becomes `applied`) · `c` **couldn't** (the next applier in the chain takes over) · `l` later (to the back) · `r` **start over** (back to the queue and started again in the background — the automated appliers get another go, e.g. after a browser problem) · `d` drop (the job becomes `not applied`) |
| **Failed** — every applier tried | `r` start over (the chain from the top, in the background) · `m` apply by hand (manual) · `d` drop |
| **Done** — submitted or dropped, newest first (history: a job you set back to want to apply — `m` in Jobs — is queued again, and its old row here says "re-queued") | `o` open the apply link |

The detail pane shows the job, its apply link, the chain (▸ the current applier), its questions (✓ answered, ? open, ✎ draft) and every attempt with its outcome and reason. Submitting runs in the background ("submitting…"); a browser window opens unless `AUTO_APP_APPLY__HEADLESS=true`. If a job goes straight to Hand-offs with "the browser crashed at start", the attempt's reason and `data/logs/browser-launch.log` say why. (`DYLD_*` variables such as `DYLD_LIBRARY_PATH=/opt/homebrew/lib`, which crashed Playwright's Chromium in ImageIO, are already removed from every browser's environment.)

#### `apply list`
Application history: every application with its job, platform, status (and date, once submitted), applier and number of attempts.

| Option | |
|---|---|
| `--status S` | `queued`, `needs_input`, `action_needed`, `ready`, `handoff`, `failed`, `submitted` or `dropped` |

```bash
uv run auto-app apply queue
uv run auto-app apply run
uv run auto-app apply run --job 28
uv run auto-app apply inbox
uv run auto-app apply list --status submitted
```

---

### `data` — storage

#### `data inspect [SOURCE [JOB_ID]]`
Without arguments: a table of saved postings (state, readiness, size, structured data found, apply kind, title). With a source: only that source. With a source and job id: one posting in full — capture details, its job, the **parsed** fields, the **normalized** keys, and the description.

| Option | |
|---|---|
| `--chars N` | how much description / page text to print (default 1200) |

```bash
uv run auto-app data inspect
uv run auto-app data inspect linkedin
uv run auto-app data inspect nvidia JR2026576
```

#### `data export SOURCE JOB_ID`
Writes one posting from the database as files (raw page or record, `parsed.json`, `normalized.json`, screenshot) for a closer look.

| Option | |
|---|---|
| `--to DIR` | destination (default `data/export/`) |

```bash
uv run auto-app data export linkedin 4445417609
```

#### `data import`
Loads a file store (`data/raw/`, e.g. from `--debug-files` runs) into the database: batches with their search runs, sightings, logs and failure artifacts; postings with raw data, parse and normalize results; source health; learned company aliases. Safe to re-run — what's already in the database is skipped.

| Option | |
|---|---|
| `--from DIR` | file store to import (default `data/raw/`) |

```bash
uv run auto-app data import
```

#### `data purge`
Expired jobs don't wait for a purge: they're archived or deleted whenever job dates are refreshed (after collecting, judging, a decision), and `serve` applies the retention below once a day. `data purge` does both now, with a report. Two passes:

Country exclusions are checked only for expired jobs otherwise eligible for deletion. If `preferences.json` cannot be read, those deletions are deferred with a warning; saved decisions and undo remain usable. Judged/decided/imported jobs can still be archived safely. Excluded never-judged jobs are archived to preserve reversible visibility.

1. **Job TTL** (`AUTO_APP_JOBS__TTL_DAYS__*` in `.env`; the clock is the time since any of the job's postings was last seen — refreshed whenever collect sees them, including as `known`):
   - jobs a filter **judged** — recommended (30 days) or un-recommended (7 days) — and jobs you decided on are **archived**, never deleted: they're **training data** for evaluation and the preference model. Kept: a record (summary, Step 2 result, your decision, the original posting identities, matching keys) plus what a filter reads (the primary posting's parsed fields, description included); dropped: raw data, normalized data, the other postings' parsed data. Archived jobs are hidden from `job list` (see `--archived`), never judged again and **never merged with active jobs** — a listing seen again becomes a new job. Among themselves they're **deduplicated**: one with the same title and a shared matching key as an earlier archived job is linked to it (one training example);
   - jobs **never judged** (not evaluated, 14 days) are **deleted** together with every linked posting, its raw data and screenshot — a listing seen again later is collected as new.
2. **Posting-data retention** (`AUTO_APP_RETENTION__*` in `.env`): raw pages / records / screenshots 7 days after a successful parse; sightings, logs and block artifacts after 30 days. Live postings keep their identity, parsed fields and dedup keys.

`--dry-run` lists the jobs it would delete or archive (number, state, last seen, company, title); the summary counts archived jobs linked to an earlier archived copy.

| Option | |
|---|---|
| `--dry-run` | show what would be removed |
| `--vacuum` | compact the database file afterwards |

```bash
uv run auto-app data purge --dry-run
uv run auto-app data purge --vacuum
```

---

## The manager: Claude Code

Open **Claude Code** (the CLI, `claude` in this folder, or the desktop app) on this project and talk to it. **`CLAUDE.md`** gives every session the picture: what the app does, the job states, the skills, the rules. Type **`/auto-app`** to start a session: it checks `serve`, what's new and what's waiting, and briefs you. It knows the app through **project skills** in `.claude/skills/`, which teach it the commands, how to read them, and the rules:

| skill | for |
|---|---|
| `auto-app` (`/auto-app`) | start a session: the loop's state, new recommendations, your queue, problems, in a few lines |
| `job-briefing` | "what's new?": the digest of new recommendations (then `job notified`), a job's details, why a filter said yes / no, comparisons |
| `job-decisions` | "apply to the two ML ones, skip the NYC ones": `job mark` with your reasons; bulk rules are shown first and confirmed |
| `filter-feedback` | "this shouldn't have been recommended": `filter label`; `filter eval`; proposes filter edits (only with your OK) |
| `pipeline-control` | "is it running?", "pause until Monday": `serve status / pause / resume`, schedule changes (with your OK) |
| `troubleshooting` | "why no new jobs?": the loop, source health, the last collect, broken filters, usage |
| `application-tracking` | "what's in my queue?", "I applied to #41", history |
| `app-config` | "also search for applied scientist in Seattle", "add Stripe", "change the job TTL": searches, sources, logins, settings. Shows each change first, verifies it after (`source plan`); never edits `.env` |
| `filter-authoring` | "make a filter for research roles", "my filter is too strict": asks what you want, drafts `command.md` / `filter.yaml`, **tries it on jobs you've already decided** and compares, then lets it judge the pool and measures it (`filter eval`). Asks before every run that costs usage |

When `serve` finds new recommendations, it sends a notification: "ask Claude Code". Ask "what's new?" and it writes the digest. It never submits an application, confirms bulk changes and edits, and can't read `.env` or `profile/` (`.claude/settings.json`; that file also lets it run the read-only commands without asking).

## Where the data goes

Everything lives in `data/` in the project directory (git-ignored):

```
data/auto_app.db                      the database: postings (raw, parsed, normalized), jobs and their state,
                                      archived jobs (training data), Step 2 judgments, your decisions and labels, audit samples, Step 3 applications and their attempts,
                                      batches, search runs, sightings, source health, aliases
data/logs/<batch_id>.log              run logs (scheduled runs: data/logs/scheduled.log)
data/screenshots/<source>/<id>.png    only with --screenshots / probe
data/failures/<batch_id>/<source>/    screenshots + HTML when a site blocks or shows a login wall / CAPTCHA
data/raw/                             file-only mode — with --debug-files (or AUTO_APP_DEBUG_FILES=true), instead of the database
data/export/                          `auto-app data export` output
browser-profiles/<source>/            saved logins
.env                                  global settings and the API key (template: .env.example)
filters/<name>/                       your Step 2 filters (command.md, filter.yaml, …); examples in filters.example/
profile/                              Step 3: profile.yaml, answers.yaml, cv.pdf; templates in profile.example/
config/apply.yaml                     Step 3: the apply policy (template config/apply.example.yaml)
```

## Safety

- Browsers run as **your own logged-in Chrome**, with human-like pacing and hard hourly / daily limits per source.
- On a login wall, CAPTCHA / security check, or HTTP 403 / 429 / 999, that source **stops** (artifacts saved, notification sent) and goes into **cooldown**; other sources continue. Detection is never evaded — `--manual-checks` only hands a check to you.
- Workday is collected through its career site's own (undocumented) JSON endpoint at about one request per second; Ashby, Greenhouse and Lever through their official public job-board APIs.

## Adding a source plugin

**Each plugin is one source.** Any `*.py` under `plugins/` is discovered at startup; a class there that defines its own `name` is a source.

- **A company on a known platform** (Ashby, Greenhouse, Lever, Workday): `auto-app source add URL` writes the plugin — a subclass of the platform base (`src/auto_app/platforms/`) with `name`, `company` and `board`. Override methods in that file for anything specific to the company.
- **A company on its own career site, or a new job site:** write the plugin yourself — an `ApiSource` (JSON API) or `SourceAdapter` (browser) subclass in `plugins/` (companies: `plugins/companies/`, with `company` set). A new platform base fits in `src/auto_app/platforms/`.

Plugins are checked when loaded (unique name, a `company` and valid `board` for platform-based ones); a broken file is reported by `auto-app source list` and doesn't stop the other sources. A plugin supplies site knowledge only — how to list or search jobs, which search fields it supports (`filters`) and its `SiteFilters`, how to match a job, and how to parse its raw data (`parse`, with a `parser_version`). The engine handles known checks, pacing, limits, blocks and storage. See [`docs/step1-collect.md` §2](docs/step1-collect.md#2-sources-and-plugins).

## Tests

```bash
uv run pytest
```

Browser collection runs end to end against a local fake job site (`tests/fakesite/`); platform plugins and parsers against saved real responses and pages (`tests/fixtures/`, personal data removed); company collection over a fake HTTP transport; collection, parse and normalize run against both the database and the file store; Step 2 agents against a fake model; `job browse` through Textual's test driver. No test touches the network.

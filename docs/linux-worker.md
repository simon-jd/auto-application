# Linux worker: screening, descriptions and Discord

`auto-app worker` processes the saved job library next to the repository/mailbox ingest worker. Its default
round is **screen → enrich → rescreen → notify → summary → health**, sharing the same SQLite database. `watch` starts rounds
900 seconds apart; if a round overruns, the next starts after it finishes, without overlapping.

The worker calls no model or AI agent and submits no applications. Its CLI bypasses the legacy `.env`
settings loader and model keys. You decide when to run
`auto-app filter run`; that separate model-filter command skips rule-excluded jobs and jobs awaiting a JD.
The existing `serve` browser/model loop remains separate.

**Current delivery state:** local implementation and verification; deployment and live acceptance pending.
The default screening policy is accepted. No Discord webhook is configured in this implementation session,
and no production services, runtime configuration or live data have been changed.

## 1. Description enrichment

Only active imported postings awaiting enrichment are candidates. Rule-excluded jobs make no new employer
requests; cache-only projection recovery can repair an interrupted write even after exclusion. Supported
HTTPS application links resolve to the employer's public JSON API, or for two sites to the public job page:

| Platform | Accepted application link | Read from |
|---|---|---|
| Greenhouse | `boards.greenhouse.io/<board>/jobs/<numeric-id>`, `job-boards.greenhouse.io/<board>/jobs/<numeric-id>` and their EU hosts `boards.eu.greenhouse.io` / `job-boards.eu.greenhouse.io`; `/embed/job_app?for=<board>&token=<numeric-id>` on any of them | `boards-api.greenhouse.io/v1/boards/<board>/jobs/<id>` (it serves EU boards too; there is no `boards-api.eu` host) |
| Lever | `jobs.lever.co/<board>/<UUID>` or `jobs.eu.lever.co/<board>/<UUID>`, optionally ending in `/apply` | `api.lever.co` / `api.eu.lever.co` |
| Ashby | `jobs.ashbyhq.com/<board>/<UUID>`, optionally ending in `/application`; board names may contain dots and spaces (`jerry.ai`, `Spectral%20Labs`) | the board's posting API, once per round |
| Workday | `<tenant>.wd<number>.myworkdayjobs.com/[locale/]<site>/job/.../<title>_<requisition>`, optionally ending in `/apply`, `/application` or `/applyManually` | the site's `wday/cxs` job API |
| Microsoft | `apply.careers.microsoft.com/careers/job/<numeric-id>` | `apply.careers.microsoft.com/api/pcsx/position_details?position_id=<id>&domain=microsoft.com&hl=en` |
| Eightfold tenants (2026-11-06) | `<tenant>.eightfold.ai/careers/job/<numeric-id>?domain=<domain>`, the links `eightfold-board` feeds write (Qualcomm's own `careers.qualcomm.com` links stay unsupported) | `<tenant>.eightfold.ai/api/pcsx/position_details?position_id=<id>&domain=<domain>&hl=en`, Microsoft's position API |
| Oracle Recruiting Cloud (2026-11-06) | `<pod>.fa.<region>.oraclecloud.com/hcmUI/CandidateExperience/<language>/sites/<site>/job/<requisition>` | the pod's `hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails` (finder `ById;Id="<requisition>",siteNumber=<site>`) |
| TikTok / ByteDance | `lifeattiktok.com/search/<id>`, `joinbytedance.com/search/<id>`, `jobs.bytedance.com/[en/]position/<id>/detail` (it redirects to joinbytedance.com) | the job page on lifeattiktok.com or joinbytedance.com (its job API answers only POST requests) |
| Jobright | `jobright.ai/jobs/info/<24-hex id>` (also `www.`) | the logged-out job page's `__NEXT_DATA__` |
| SmartRecruiters | `jobs.smartrecruiters.com/<company>/<numeric-id>[-<title>]` or `/oneclick-ui/company/<company>/publication/<UUID>` | `api.smartrecruiters.com/v1/companies/<company>/postings/<id or UUID>` |
| Workable | `apply.workable.com/<account>/j/<shortcode>`, optionally ending in `/apply` | `apply.workable.com/api/v2/accounts/<account>/jobs/<shortcode>` |
| Amazon | `www.amazon.jobs` or `amazon.jobs`: `[<locale>/]jobs/<numeric-id>[/<title> or /apply]` | `www.amazon.jobs/en/search.json?base_query=<id>`, keeping the result whose `id_icims` is the id |

Every request URL is rebuilt from the board and id read from the link: query strings, other hosts and other
paths never reach the network. Pages are requested with `Accept: text/html`, the APIs with `application/json`.

The sites differ in what they give and in how a closed job looks:

- **Microsoft, other Eightfold tenants, SmartRecruiters, Workable** answer 404 for a closed job (`gone`).
  SmartRecruiters `active: false` is `gone` too. An Eightfold or Oracle record never names the employer; the
  feed's company stays on the posting.
- **Oracle** answers a closed or unknown requisition with an empty `items` list (`gone`). The description is the
  requisition's external description, responsibilities and qualifications (then the organization and corporate
  descriptions when present). Lookup values such as the location text and job schedule follow the
  requisition's own locale (Ford posts some in Brazilian Portuguese while the text is English), so the import's
  English locations are kept.
- **TikTok / ByteDance** pages give the title, `Location`, `Team`, `Employment Type` (`Regular` is full time),
  `Job Code` (the requisition id) and the description between "Share this listing:" and the second "Apply to this
  job"; the salary comes from its pay-transparency paragraph. A page that renders without the job's title and
  apply link is `gone`; a page that names another job, or is not the career site, fails.
- **Jobright** shows logged-out visitors its structured summary, not the employer's text: the description is
  the summary, responsibilities, qualifications, preferred qualifications and benefits. `isDeleted` is `gone`.
  About one listing in fifteen redirects logged-out visitors to another job board (ZipRecruiter); it is cached
  `unsupported` with platform `jobright` and not retried.
- **Amazon** has no per-job JSON: the job is the search hit with that `id_icims`. A job the index no longer lists
  is `gone` (its page answers 404).

The returned record must belong to the requested posting. For Workday, the request already names the
exact posting path, so the record's `jobReqId` may differ in punctuation (`JR-123456` / `JR123456`), omit
the letter prefix (`123456`), or lack the URL's copy suffix (`R123456-1` / `R123456`); a different prefix
(`R123456` / `JR123456`) or a different copy (`REQ-2026-01` / `REQ-2026-02`) is rejected.

Custom company domains and unrecognized links remain unsupported. Requests reject credentials, unusual
ports, traversal and off-platform targets. Redirects are not followed. At most **200 actual HTTP requests**
are made per round, with at least one second between requests to the same host (two for Eightfold tenants,
which answer bursts with HTTP 429). The User-Agent is
`auto-app/1 (personal job search; one request per job)`. An Ashby board response is reused during the round;
cache reuse consumes no request budget. `--enrich-limit` can reduce the budget to any integer from 0 to 200.

The `enrichments` table caches by canonical application URL:

| Status | Meaning and retry behavior |
|---|---|
| `ok` | A usable employer description is saved and merged into the posting. A later feed import that clears it restores it from cache without another request. |
| `failed` | A request or parse failed. Retry after 30 minutes × 2^(attempts − 1), capped at 24 hours. |
| `unsupported` | No supported public API is known (platform `NULL`), the employer record has no description, or Jobright sends the listing to another site. No automatic retry, except that a link cached with platform `NULL` is read again in the first round after an adapter for it exists. |
| `gone` | The employer API reports the opening absent/closed. No automatic retry; the next screening pass evaluates closure. |

Enrichment preserves imported source evidence, stable job IDs, manual decisions and model verdicts. It fills
missing fields conservatively and refreshes normalized projections. Unsupported and failed jobs remain
available for inspection; they are not deleted.

## 2. Deterministic screening

The optional rules file is `$AUTO_APP_HOME/config/screening.json`; without it, these defaults apply:

```json
{
  "exclude_internships": true,
  "exclude_senior": true,
  "exclude_non_cs": true,
  "exclude_citizenship_required": true,
  "max_years_required": 3,
  "extra_cs_terms": [],
  "extra_non_cs_terms": [],
  "us_only": true,
  "exclude_unknown_without_software_evidence": false
}
```

The `exclude_*` values and `us_only` are booleans. `max_years_required` is the inclusive exclusion threshold: a
clear requirement of 3 or more years excludes, **including a job titled new grad**; `null` disables this rule.
The two `extra_*_terms` values are lists of nonempty title phrases extending CS/non-CS recognition.
Unknown keys and invalid types fail visibly rather than silently reverting to defaults.

| Reason | Evidence |
|---|---|
| `internship` | Explicit intern/co-op/apprentice title or internship employment type; a seasonal title without a new-grad signal; or appearances exclusively in internship feeds. |
| `senior_title` | Explicit senior/staff/principal/lead/manager/director/architect or III-and-above level. New-grad/entry/junior title signals suppress this title rule. A comma- or dash-separated part made only of level words (“Sr. Staff”, “Senior Staff”, “Principal Lead”) is senior too. Ambiguous uses such as “Member of Technical Staff” and team suffixes (“- Staffing Platform”, “- Staff Tools”) remain kept. |
| `not_cs_role` | An explicit non-CS title, or a non-CS repository category when the title is otherwise unknown. A CS title wins over a category; a generic Software category does not override an explicit non-CS title. Embedded software, firmware and FPGA remain in scope. |
| `no_software_evidence` | Opt-in (`exclude_unknown_without_software_evidence`, off by default). The title names no role (`unknown`), the job has a description, and the description names no software skill: no language (Python, C++, C#, SQL …), no programming, computer science, APIs, Git, Linux, cloud, ML, embedded/firmware, software development/engineering, full-stack/back-end/front-end, CI/CD, DevOps or databases. A job without a description is never excluded by this rule. |
| `citizenship_required` | An explicit US citizenship, US-person (ITAR/EAR) or security-clearance requirement, in the description or the title. Counted: enumerated ITAR lists ("must be a (i) U.S. citizen or national, (ii) …", "must either be (A) a U.S. person"); quoted terms ("require “U.S. Person” status"); "only for / restricted to U.S. persons"; "U.S. citizen or green card holder required"; access to export-controlled data "without" an export license; a named clearance (Secret, TS/SCI, DOE Q, security/government/DoD/U.S. clearance) with a requirement cue such as eligible, obtain, active, must or required; a heading/value pair ("Security Clearance" over "Top Secret/SCI required"); titles with Cleared, Clearance, TS/SCI, Top Secret, Polygraph/Poly or U.S. Person/Citizenship. Not counted: negated or hedged wording (may, if needed, where required, certain positions, typically); a licence route ("may require an export license", "contingent on ability to obtain authorization"); pay statements; facility, TSA or badge clearances; Public Trust; definitions and EEO text; statements that also admit non-citizens or visa holders; questions; and items under a preferred heading ("Preferred qualifications", "Nice to Have", "Ways to stand out") unless the item itself says "required" or "must". The first line that is not a list item ends a preferred section. |
| `experience_required:N` | Independent mandatory experience constraints use the highest requirement; explicit OR/degree alternatives use the least demanding route. N reaches the configured threshold. A range counts from its lower bound (`1-3`, `1~3`). "A Bachelor's degree in CS … or 3+ years" is an alternative only when no experience route names a degree. Items of a "one of the following:" list of education routes are alternatives. Preferred/nice-to-have, negated, degree-substitution and employer/team-history text do not count. |
| `closed_at_source` | Every saved application URL for the job has a `gone` employer result. One closed link alone does not close a job with other links. |
| `outside_us` | `us_only` is on and every explicit place the job names is outside the US: another country, a region (EMEA, APAC, Europe …), a well-known foreign city, or Lever's structured country. Any US location (a state, `US`/`USA`, a common US city alias, "Remote - US") keeps the job, so "London or New York" stays; "Remote" alone, "Hybrid" and unrecognized places are unknown and stay. US evidence wins inside one location, so "Dublin, CA" is US. Country exclusions in `preferences.json` still apply on top. |

Unknown roles and uncertain evidence remain kept. The rules do not infer candidate qualifications or use a
model. Sponsorship is a sorting signal: `offers`, `unknown`, `no`, or `citizenship_required`. Lack of
sponsorship alone never excludes a job. This matches the owner's rule (confirmed 2026-10-19): jobs fall into
three classes — sponsors visas (e.g. Google), does not sponsor (e.g. Visa), requires a security clearance or
US citizenship (e.g. Amazon Leo) — and only the third is excluded. Signals also expose role, required years, new-grad title evidence,
and whether a description is present.

**Exclusion is a reversible view.** The worker stores `job_screenings`, never deletes jobs or changes
`jobs.step2`, `jobs.decision`, reviews or feedback. Changed facts or rules trigger reevaluation next round;
unchanged jobs keep their existing verdict. Archived/deleted jobs lose stale screening rows. Country
preferences, including the existing Canada exclusion, remain an independent visibility filter.

```bash
uv run auto-app worker run --steps screen
uv run auto-app-query jobs --include-excluded --limit 30
uv run auto-app-query jobs --sponsorship offers
uv run auto-app-query job 123
uv run auto-app-query context
```

The query CLI, HTTP API and MCP `search_jobs` hide rule-excluded jobs by default. Use
`--include-excluded` or `include_excluded=true` to audit `screening.reasons`, `screening.signals` and the
rules version; direct job details preserve evidence. `context.screening` contains kept/excluded/unscreened
counts, exclusions by reason and kept jobs by sponsorship. These query paths stay read-only, including
against a database predating this migration; sponsorship filtering requires screening tables.

## 3. Discord

In Discord, open your server's **Settings → Integrations → Webhooks → New Webhook**, select the destination
channel, and copy the URL. The owner enters it directly on Linux in
`~/Desktop/job-search/runtime/credentials/worker.env`:

```dotenv
AUTO_APP_DISCORD_WEBHOOK_URL=<your webhook URL>
```

Keep that file mode **600**, outside the checkout. The worker reads this value only from its environment;
do not paste it into chat, commit it, or print the credentials file. The service template loads the file
through `EnvironmentFile`. An empty file is sufficient when running without Discord: notification and
alerts are then reported as skipped, while screening/enrichment can run.

A digest includes jobs that are active, visible under country preferences, rule-kept, `not_evaluated` or
`recommended`, not yet notified, and first seen at or after `notify_since` and after its source was added (below). Ordering is sponsorship
offered → unknown → no sponsorship; within a group, your filter's recommendations (⭐) come first, then
newer posting dates. Each round sends at most 30 jobs, in messages containing at most 5 embeds. Messages
include title/company, location, sponsorship, posting date, a saved recommendation reason when present,
and an application link. No mentions are enabled.

A job without a description waits two hours from first seen, then becomes eligible as **title only**.
The first worker round durably saves `notify_since` before work begins, so the existing backlog does not
flood Discord—even if the process restarts or no webhook has been configured yet. An explicit ISO timestamp
with `--notify-since` replaces the cutoff and persists it; moving it backwards deliberately makes older
unnotified jobs eligible. Already notified jobs stay marked.

A feed added later is baselined the same way. Each round records, per source, when the worker first saw
it (`source_since`): sources that already had postings before `notify_since` follow `notify_since`
(stored as `null`, so `--notify-since` still applies to them); a new source — an employer board, a
repository or a mailbox enabled later — gets the time of that round. Its existing jobs were imported
before that moment, so they are browsed in chat, not pushed; only jobs that appear in it afterwards are
announced. A job found on several sources is announced if any of its registered sources allows it. Jobs
of a source the worker has not registered yet wait for the next round.

Only successfully delivered batches are marked notified. A later failed batch remains pending for the
next round; a failed notification does not undo enrichment. Discord 429 responses receive bounded retries.
This is acknowledgment-based tracking, not an exactly-once guarantee: if Discord accepts a message but
its response is lost, or the process/database fails before recording `notified_at`, a retry can duplicate it.
Operational alerts cover worker step errors and an ingest worker failing at least three consecutive
rounds. The same problem is alerted at most once per 12 hours, and a failed alert is not recorded as sent.

## 4. Commands, loop and state

Worker commands emit JSON by default; they do not need a `--json` flag.

```bash
uv run auto-app worker run --db data/auto_app.db --steps screen
uv run auto-app worker run --db data/auto_app.db --steps screen,enrich --enrich-limit 20
uv run auto-app worker run --db data/auto_app.db --enrich-limit 20
uv run auto-app worker watch --db data/auto_app.db --interval 900
uv run auto-app worker status --db data/auto_app.db
```

`run` requests one immediate round; while paused it returns the paused state without running or waiting.
`watch` resumes the saved next-run time after restart and reloads rules during screening.
`--steps` accepts subsets of `screen,enrich,notify,summary,health`. A second screening pass runs when enrichment changes facts
(including a `gone` result without a new description), or may have written facts before an exception.
No-change rounds skip that pass. `watch --interval` accepts at least 60
seconds. The database defaults to `$AUTO_APP_HOME/data/auto_app.db` (project-local when that variable is unset).

The separate control file beside the worker state records `pause_pipeline` / `resume_pipeline` requests.
The watch loop checks it before a round. A pause prevents screening, enrichment and notification rounds;
the [Discord bot](discord-bot.md) also stops its digest while paused. The independent repository/mailbox
ingest worker continues its 30-minute feed schedule. An invalid control file fails closed without
crashing: the worker stays alive but runs no rounds, records `status: control_invalid` with a
`control_error`, and sends one Discord alert when a webhook is configured. `/resume` (or the
`resume_pipeline` / `pause_pipeline` tools) rewrites the file, and rounds continue without a restart. The unit template runs this worker with `--steps screen,enrich,summary,health` (bot mode), because the bot sends the digest and handoff outcomes; the worker keeps its webhook summary/health path. A webhook-only setup without the bot adds `notify` back to `--steps`.

The `<db>.worker.lock` prevents overlapping worker processes; `<db>.worker-state.json` is written atomically:

| Key | Meaning |
|---|---|
| `status` | `not_started` before the first round; last outcome `ok`, `partial`, `error` or `interrupted`; `running` during work, `paused` while paused, and `waiting` after resuming before the saved deadline. |
| `last_run_at`, `last_finished_at` | UTC start/end timestamps of the latest round. |
| `next_run_at`, `interval_seconds` | Saved scheduling deadline and configured start-to-start interval. |
| `consecutive_failures` | Consecutive non-`ok` rounds, including partial step failures; reset after an `ok` round. |
| `last_report` | Per-step results, counts and bounded error details. |
| `notify_since` | Durable first-seen cutoff used by the digest. |
| `handoff_cursor` | Last acknowledged external-agent attempt in webhook notification mode; bot has its own state/cursor. |
| `summary_at` | Last acknowledged weekly summary; another is due after seven days. |
| `control_error` | Present only while the control file is unreadable (`status` is then `control_invalid`). |
| `source_since` | Per-source cutoffs: `null` = follows `notify_since`; a timestamp = the source was added then, and only later jobs from it are announced. |
| `alerts` | Problem fingerprints and the last successfully sent alert time. |
| `pid` | Last worker process ID; the separate lock determines live running status. |

`status` reads the sidecar and adds `running` without opening/migrating the database. A database lock or
step exception is reported; independent later work can continue and retry next round. A failed screen or
rescreen suppresses notification until a current verdict is available. A partial/error `run` exits nonzero;
`watch` records the failure and continues its schedule. A missing webhook is a notification skip, not proof
of a live Discord delivery.

Individual enrichment failures remain queued for retry. At least three failed requests with no successful,
cached or gone results produce an outage status; step exceptions also fail the round. The failed count stays
visible even when a mixed-success round is healthy. In bot mode, `screen,enrich,summary,health` retains webhook summaries, disk health and operational
alerts if configured, while the bot owns job digests. A screen-only run never initializes Discord.

## 5. Deployment and pending live acceptance

The commands below are an operator procedure, **not actions completed in this session**. The local script
updates an existing installation; it does not configure the webhook, install a new unit, or enable feeds.
Review the local checkout before syncing: deployment copies its current files, including uncommitted code.

```bash
# From the development checkout, only when ready to deploy:
scripts/deploy_linux.sh home-ubuntu
# Optional destination override: BASE=/srv/job-search scripts/deploy_linux.sh my-host
```

The default remote base is `~/Desktop/job-search`. `BASE` can be a relative home path or an absolute path,
without spaces or `.`/`..` segments. The script performs these steps in order:

1. Inspect ingest, MCP agent, preparation and worker units, failing if systemd cannot report their state.
   Record active or restart-pending services and explicitly stop every installed unit, including one
   currently activating/reloading/deactivating. Missing units are skipped. A unit already stopping retains
   that intent and is not restarted.
2. Back up the existing live SQLite database with SQLite's backup API, verify it with `quick_check`, and
   retain a mode-600 `runtime/data/auto_app.<UTC timestamp>.pre-deploy.db`. This happens **before code sync**.
3. Synchronize the checkout with checksum comparison. Private config, credentials, profiles, data,
   `.env` variants, editor/agent state and build caches are excluded; only configuration examples transfer.
   Excluded remote files are preserved. External symlinks are not followed.
4. Run `uv sync --frozen --python 3.12`, then explicitly apply Alembic migrations while services are stopped.
5. Reload systemd and start only the services that were previously active or restart-pending. A stopped worker remains
   stopped; the new worker requires the separate installation below.

The script leaves a `runtime/.deploy-services` snapshot until successful completion. A backup/sync/dependency/
migration failure prevents service startup; inspect the reported error before recovering. After a startup
failure, check which services actually started. A leftover snapshot blocks a second deployment. Keep the
pre-deploy database and a known previous code/dependency snapshot for operator recovery; this script does
not perform automatic rollback. Before retrying, restore a coherent code/database pair or resolve the
failure, deliberately restore the recorded services if appropriate, then remove the snapshot. Database
restore must be done with all writers stopped and with SQLite WAL/SHM sidecars handled consistently.

On Linux, prepare the runtime configuration and service when authorized:

```bash
cd ~/Desktop/job-search
mkdir -p runtime/config runtime/credentials ~/.config/systemd/user
# For a first installation; retain an existing customized rules file:
cp -n auto-application/config/screening.example.json runtime/config/screening.json
(umask 077; touch runtime/credentials/worker.env)
chmod 600 runtime/credentials/worker.env
# The owner edits worker.env privately to add the webhook when available.
cp auto-application/deploy/auto-app-worker.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now auto-app-worker.service
systemctl --user status auto-app-worker.service --no-pager
```

The unit paths assume the default base; adjust them before installing at another destination. The accepted
feed policy also removes `simplify-internships`, `speedyapply-swe-internships`, `speedyapply-ai-internships`
and `jobright-swe-internships` from the **remote** `runtime/config/feeds.yaml`. That was done on 2026-11-02,
together with the jobs screened as internships (docs/updates/2026-11-01-grok-feeds-dedup.md). The remote
config also sets `skip_internships: true`, so internships that other feeds list are not imported
(docs/updates/2026-11-02-skip-internships.md).

For staged live checks, stop the continuous worker before forcing a manual round:

```bash
cd ~/Desktop/job-search/auto-application
export AUTO_APP_HOME="$HOME/Desktop/job-search/runtime"
WORKER_DB="$AUTO_APP_HOME/data/auto_app.db"
.venv/bin/auto-app worker run --db "$WORKER_DB" --steps screen
.venv/bin/auto-app worker run --db "$WORKER_DB" --steps screen,enrich --enrich-limit 20
# Once the owner has configured the webhook, load it without printing it:
set -a; . "$AUTO_APP_HOME/credentials/worker.env"; set +a
.venv/bin/auto-app worker run --db "$WORKER_DB" --enrich-limit 20
python3 -S scripts/job_view.py --db "$WORKER_DB" context
systemctl --user start auto-app-worker.service
```

Do not predict live counts from test fixtures. A first default round should leave pre-cutoff jobs
unannounced; new arrivals after that cutoff may legitimately be delivered during later staged runs.

After **24 hours of live operation with a configured webhook**, record evidence for all of these gates:

1. Worker status reports `consecutive_failures: 0`, a recent completed round, and successful screen, enrich
   and notify results, plus successful rescreen whenever that step was needed. Confirm the running systemd
   service independently.
2. At least **90%** of active rule-kept jobs with a supported Greenhouse/Lever/Ashby/Workday application link
   have a saved nonempty JD. Report the numerator, denominator and collection timestamp, separately from
   unsupported platforms; empty descriptions and failed supported requests count against this measure.
3. Newly ingested eligible jobs appear in Discord within **15 minutes**; title-only jobs within **2 hours
   15 minutes**, with expected fields and no initial backlog flood. Measure saved first-seen time against
   actual receipt time; service status alone does not prove delivery. Queue caps, long rounds or outages
   can prevent this gate from passing and must be reported rather than described as a guarantee.
4. An actual connected Dot call uses `search_jobs(sponsorship="offers")` and returns the expected saved
   results. Refresh the connected tool schema if needed; local MCP tests are not a cloud acceptance result.
5. Manually review **30 randomly sampled rule-excluded jobs**, covering the full active excluded pool rather
   than only its newest page. At most **2** may be false exclusions; otherwise fix the rule and repeat the audit.

Record the live results and exact gaps in [the implementation entry](updates/2026-10-12-linux-worker.md).
No live deployment, Discord delivery, 24-hour observation, coverage measurement or cloud acceptance is
claimed by the local tests.

## 6. Employer boards

`auto-app worker boards --yaml --top 30` reads saved, active, visible, rule-kept jobs and recommends employer
board feeds, ranking sponsorship evidence first. It is a read-only suggestion; inspect the generated YAML
and choose sources before editing **runtime** `config/feeds.yaml`. The suggested feed formats are
`greenhouse-board`, `lever-board` and `ashby-board`; each is `kind: repository` with a public listing API
`location`. Workday, Eightfold, iCIMS Jibe, Oracle and SmartRecruiters boards (below) are added by hand. Optional `company` is a display hint when the board API omits the company name. Source names
stay stable across runs, and a board already configured is omitted from `--yaml` output. Ashby board names may
contain dots or spaces (`rivianvw.tech` → `board-ashby-rivianvw-tech`, `spectral labs` →
`…/job-board/spectral%20labs`). A link whose board name no listing API accepts is skipped, and the listing
continues.

```bash
uv run auto-app worker boards --yaml --top 30
# Example only: keep it disabled until the board and employer have been reviewed.
#   board-greenhouse-example:
#     enabled: false
#     kind: repository
#     format: greenhouse-board
#     company: Example Employer
#     location: https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true
```

**Greenhouse URLs must include `?content=true`** so each listing carries its JD. Lever can use either
regional public postings API; Ashby uses its public job-board API. The [disabled examples](../config/feeds.example.yaml)
show their URL shapes. The feed adapter retains full descriptions, source history and exact ATS identities;
repository copies of the same employer opening can then deduplicate by employer key. A partial board with
malformed rows keeps earlier absent records for retry instead of treating them as closed.

**Workday** (`format: workday-board`, 2026-11-06): `location` is the careers URL
`https://<tenant>.wd<N>.myworkdayjobs.com/<site>`, `company` is required, and optional `search` (one Workday
search per text), `facets` (parameter or label → values as the site shows them) and `max_pages` (pages of 20,
default 25, at most 100) narrow it. Every run POSTs each page with a one-second pause between requests. The
list has no JD, so imported postings are pending. This worker's screen step judges their titles and
locations, and its enrich step reads the CXS detail endpoint for each posting kept, within the round's
request budget. A search that falls short of Workday's total, at the page cap or Workday's 2000-result cap, is a
partial snapshot that closes nothing. Ready-to-review entries for GM, NVIDIA, Intel, Aptiv and Cisco, with
their live counts and request volumes:
[the Workday record](updates/2026-11-06-workday-board.md).

**Eightfold, iCIMS Jibe, Oracle and SmartRecruiters** (`eightfold-board`, `icims-jibe-board`, `oracle-board`,
`smartrecruiters-board`, 2026-11-06) read the same way: pages of the site's public search with a pause between
requests, an optional `country`, `search` and `max_pages` (Eightfold also `filters`), and a partial snapshot
when a search still falls short of the site's total after reading it a second time. Jibe lists carry the
full JD, so its jobs import complete. The others import pending, and this worker's enrich step reads each
kept posting's detail (Eightfold's `position_details`, Oracle's requisition details, SmartRecruiters' posting
API; table in §1). The location shapes, options and the live read-only counts for Qualcomm, Garmin, AMD,
Lutron, Rivian, Ford, TI, Honeywell, Bosch, Intuitive and Western Digital are in
[the paged boards record](updates/2026-11-06-paged-boards.md).

```yaml
#   board-workday-aptiv:
#     kind: repository
#     format: workday-board
#     company: Aptiv
#     location: https://aptiv.wd5.myworkdayjobs.com/aptiv_careers
#     facets:
#       Country: [United States of America]
#     max_pages: 10
```

After approved entries are added to `runtime/config/feeds.yaml` and enabled, the existing ingest worker
loads them on its next round, ordinarily every **30 minutes**. The read-only suggestion command alone
does not fetch those boards or prove the roadmap's 30-company daily coverage. Check `ingest status`,
`auto-app-query context` for `board-*` source health, same-opening deduplication, and the live acceptance
counts in [the P1 record](updates/2026-10-12-p1-coverage-and-control.md). No live board entries or runtime
feeds were changed during local implementation.


## 7. P2/P3 outcomes, tracking and operations (2026-10-13)

The `notify` step sends pending external-agent outcomes even when there are no new digest jobs and
advances its cursor only after acknowledgment. The bot owns outcome notifications in bot mode and
keeps `<db>.bot-state.json` independently. Pause suppresses these deliveries.
[Handoff contract](agent-handoff.md).

`summary` posts on the first due round and then after seven days since the last acknowledged summary,
covering the preceding seven days. It is not a Monday schedule. No webhook means skip, and failed
sends do not advance `summary_at`. `health` checks the database filesystem for at least 2 GiB free;
errors use the existing operational alert path. These steps run independently of new-job notification.
[Application tracking](application-tracking.md).

Nightly private SQLite backup templates cover both main DB and an optional existing preparation
ledger, verify every new archive before retention, and retain 14 recent daily / 8 older weekly
representatives per DB. Referenced attachments are outside coverage. Mac backup pull uses private SSH.
Service templates add `OnFailure`; optional clean-checkout Git deployment is available with a
pre-existing bare repo, while rsync remains default. [Operations and recovery](operations.md).
No P2/P3 production deployment, backup pull, actual alert or live acceptance was performed.

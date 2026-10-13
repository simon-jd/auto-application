# Fictional Ashby preparation — 2026-10-11

**Step:** 3, 4   **Status:** Ubuntu deployed; live Claude fill-only verified;
production queued-run/restart verified; cloud six-tool inventory refreshed;
132 preparation tests passed after regression fixes; both new cloud calls verified with idempotent result reuse

## What

Dot can request an Ashby form preparation through two opt-in MCP tools. A
separate Ubuntu worker fills only the packaged fictional candidate, checks
actual field values and returns missing questions. It stops before submission
and closes the browser. The report/screenshot are artifacts, not a retained
browser draft or an application submission.

This implements the first version authorized after the Simplify test. Simplify
is retained as an interface only. Real profiles, additional ATS platforms,
answer editing, submission and interview outcomes are outside this version.

## Changes

- `src/auto_app/prepare/`: fixed candidate and test PDF, strict Ashby URL
  validation, idempotent SQLite ledger, read-only job resolution, browser guards,
  read-back field checks and a separate serial worker. Readiness requires native
  and ARIA validity, committed location selection, an enabled final control,
  and resume storage/attachment acknowledgements.
- `prepare/network.py` and packaged query fixtures allow three exact observed
  GraphQL documents: `ApiCreateFileUploadHandle`, `ApiSetFormValue` and
  `ApiSetFormValueToFile`. The allowed fields/values belong to the fixed candidate
  and selected employer/job. Multipart file bytes are reconstructed from the
  packaged PDF; the observed upload destination, byte hash and acknowledgement
  are checked. All allowed fetches disable redirect following and reject 3xx
  responses. Final submit and account operations remain unavailable.
- `src/auto_app/agent/mcp_server.py`: optional `prepare_application` and
  `get_application_run`; configuration and current worker availability are
  reported in `capabilities.preparation`. With no preparation directory the
  existing four tools remain unchanged.
- `src/auto_app/agent/cli.py`: `serve --preparation-dir`, plus `prepare-worker`
  with `--once`, `--driver rules|claude`, `--browser-channel` and bounded
  `--task-timeout` options.
- `deploy/auto-app-preparation.service`: separate worker template; existing
  ingestion remains independent.
- The Dot guide, README, Step 3/4 docs and Job Search Skill describe the precise
  preparation-only workflow. Plugin version `0.2.0` preserves manual tracking.

The ledger stores candidate/resume hashes and copies the PDF into each run's
directory. Identical target/package requests reuse the existing run. A process
lock prevents competing workers; interrupted work is recovered only after lock
acquisition, with at most two total attempts. Timeouts and ordinary failures
are terminal. The job database is never written by preparation.

Saved-job requests reject excluded, archived, duplicate and already-applied
records. Explicit Ashby URLs are independent fictional test targets and do not
consult or change saved decisions; both paths deduplicate by normalized target
and candidate package. Account/password requirements produce an action blocker.
Unknown legal/work-authorization answers remain missing.

## How to use

Use the same explicit preparation directory for the MCP service and worker.
See the [Dot guide](../dot-setup.md#fictional-ashby-preparation-v1) for commands,
service configuration, result states and cloud discovery checks.

The default `rules` driver fills known mappings without model credentials.
`--driver claude` enables the constrained browser agent and requires a working
Claude Code login for the worker's Ubuntu user. The fixed candidate remains
the only data source under either driver. Unknown required questions are
reported, not invented. v1 has no answer-update or submit tool.

`get_application_run` returns the persisted run, with terminal details under
`result`: field checks, missing questions, guard evidence, hashes/usage and a
screenshot filename when available. Artifacts live under
`PREPARATION_DIR/runs/<run_id>/`. A successful preparation never updates
`applied`, and a browser failure cannot silently fall back to a submitting
applier.

## Ubuntu deployment

The production preparation worker is enabled with
`--driver claude --browser-channel chrome --task-timeout 240`. The preparation,
ingestion, MCP and tunnel user services were all active with `NRestarts=0` at the
deployment check. The four base MCP tools remain available, and the actual MCP
protocol exposes both new preparation tools for six total.

Changed deployed files were backed up to
`/home/postedism/Desktop/job-search/backups/preparation-v1-20261011-222528`.
The production preparation ledger is
`/home/postedism/Desktop/job-search/runtime/preparation/preparation.sqlite3`;
the existing job library and ingestion configuration are separate.

Production run `64f925a5-8147-46a2-8d5d-43d319658c27` was enqueued and retrieved
through the actual MCP protocol. The deployed worker reached `ready_for_review`
on **attempt 1**, with **six required fields matched**, **zero missing questions**
and **27.36 seconds from enqueue to completion**. After idle worker and MCP
restarts, the same run ID and result persisted. Repeating `prepare_application`
returned the same run without another attempt. The observed library applied
count was **0**.

The queued execution's SDK usage estimate was **$0.045427**, with **50,397 combined
input tokens (including cache)** and **961 output tokens**. This estimate is not
an actual billing record. Its timing covers this queue execution only, not a
cross-platform benchmark.

- [Production run](../../../reports/2026-10-11-ashby-preparation/production-run.json)
- [Restart and idempotency proof](../../../reports/2026-10-11-ashby-preparation/production-proof.json)
- [Production filled-form screenshot](../../../reports/2026-10-11-ashby-preparation/production-filled.png)

The live browser result in the next section is a separate probe, with its own
usage evidence.

## Live Ubuntu Claude/Chrome probe

The live target was Ramp's **Sales Development Representative, Mid-Market** on
Ashby. The isolated browser used only `fictional-sales-v1` and returned
`ready_for_review`: all six required fields matched, with no missing questions.
The site selected **Seattle, Washington, United States**, corresponding to the
fixed candidate's Seattle, WA value. The result records one upload handle,
one storage upload, `resume_uploaded=true`, `resume_attached=true` and
`location_verified=true`. Two optional fields remained empty.

The report records `submission_allowed=false`, `browser_session_retained=false`
and model status `ready`. No final submission occurred. The guards recorded a
blocked recaptcha resource and one unapproved autosave request, with no blocked
browser actions. Approved fictional autosaves and the resume upload did reach
the site's draft/storage endpoints; filling is not a claim of zero network
writes or deletion of site-side test data.

Evidence is saved locally:

- [Live structured report](../../../reports/2026-10-11-ashby-preparation/live-claude-report.json)
- [Filled-form screenshot](../../../reports/2026-10-11-ashby-preparation/live-claude-filled.png)

The SDK reported **43,371 combined input tokens (including cache)** and **1,001
output tokens**, with a cost estimate of **$0.0538562**. This is the SDK's estimate,
not evidence of an actual billed amount or a separately measured new-input/cache
breakdown. One live form establishes this bounded flow, not a general success
rate across employers or software-engineering screening questions.

## Tests / verification

The focused worker/MCP checks observed so far:

- 17 tests passed across the preparation MCP/worker tests and existing MCP
  regressions, including real stdio and loopback Streamable HTTP. The two HTTP
  cases initially hit the local sandbox's socket-bind restriction; they passed
  when rerun with local loopback access.
- After adding attachment-directory checks and a real competing-process lock
  check, 40 worker/service tests passed. The attachment-path regression first
  reproduced an incorrectly accepted external path, then passed after it was
  rejected before browser execution.
- Unconfigured MCP still exposes four tools; opt-in MCP exposes six. Results
  survive server restart, repeated enqueue reuses the run, timeouts cancel the
  injected browser executor, and worker interruption recovery is bounded.

Those counts describe scoped local runs. The first Ubuntu full-suite attempt
lacked Playwright's matching bundled Chromium binary. After installing the
official browser, the complete regression recorded **951 passed, 2 failed,
1 skipped in 345.60 seconds**. Both failures exposed the same preparation
boundary: account-form detection read `innerText` from `Document` on a page
without a `<form>`. The preparation session now falls back to the document body;
the historical apply session is unchanged.

After that fix, **all 132 preparation tests passed in 68.10 seconds**, including
both previously failing native-validity/ARIA cases. The fix was deployed and
all four Ubuntu services were again active with zero automatic restarts. The
single skipped web test requires an existing `web/dist` build. The full suite
was not rerun after the scoped preparation fix; no single clean 953-test run is
claimed.

- [Full regression before the fix](../../../reports/2026-10-11-ashby-preparation/full-suite-before-fix.log)
- [All preparation tests after the fix](../../../reports/2026-10-11-ashby-preparation/preparation-final.log)

Browser regressions distinguish fixture acknowledgements from the live protocol:
the ready-state fixture labels its upload acknowledgement as simulated; a file
input showing the right name without server acknowledgement remains
`needs_input`. Other cases cover required passwords, disabled submit controls,
native/ARIA invalid values, unknown required answers, committed dropdown choices,
attachment tampering and submission/navigation guards. No application submission
is part of this feature's verification.

## Cloud plugin and tool availability

Plugin **0.2.0** was uploaded and its version verified in the cloud UI. Refresh
of the connected private backend succeeded. Its tool inventory now shows
**Read 4 / Write 2**, including `prepare_application` and `get_application_run`.
The existing cloud `get_job_context` call returned
`capabilities.preparation.worker.available=true`.

The initial runtime cached the original four tools. On the user's follow-up,
all six tools were available. At **23:15 PDT on 2026-10-11**, these actual calls
through the connected Job Search Backend cloud tools succeeded:

1. `get_job_context`: preparation enabled, Claude worker available, applied count 0.
2. `prepare_application(application_url=...)`: the same Ramp Sales URL and fixed
   fictional package returned production run
   `64f925a5-8147-46a2-8d5d-43d319658c27`, `ready_for_review`, attempt 1.
3. `get_application_run(run_id=...)`: returned the identical persisted result,
   with six required fields matched, no missing questions, confirmed resume
   upload/attachment and location, and `submission_allowed=false`.
4. `get_job_context`: worker still available and applied count still 0.

This closes the cloud-tool callability gap; the verification did not substitute
SSH or loopback MCP for either connected-cloud call. Idempotency reused the
earlier completed run, with unchanged timestamps and attempt count, so **this
follow-up did not run Chrome/Claude again**. The earlier production worker run
proves actual filling; these cloud calls prove request routing, durable result
readback and duplicate protection. No new cloud-enqueue-to-browser execution,
new filling duration or new model cost is claimed. No application was submitted.

- [Plugin version UI evidence](../../../reports/2026-10-11-ashby-preparation/plugin-0.2.0.jpg)
- [Refreshed six-tool inventory](../../../reports/2026-10-11-ashby-preparation/cloud-six-tools.jpg)
- [Uploaded plugin archive](../../../reports/2026-10-11-ashby-preparation/job-search-dot-0.2.0.zip)
- [Actual cloud call responses and verification](../../../reports/2026-10-11-ashby-preparation/cloud-call-proof.json)

## Follow-ups

The requested cloud call verification is complete. Keep live-site filling,
fixture coverage, production queue execution and idempotent cloud-call evidence
distinct. A future explicit rerun feature would need its own audited interface;
changing a target or candidate to evade duplicate protection is not supported.

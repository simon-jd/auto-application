# Dot connected to the Ubuntu job library

The job database and feed worker stay on Ubuntu. Dot calls four library and
manual-tracking tools by default. An explicit preparation directory adds two
fictional Ashby preparation tools and a separate browser worker. The Skill
explains how to use the tools actually discovered in the current session.
The Mac is only used during setup.

```text
Dot + cloud Job Search Skill
  → registered private ChatGPT app
  → OpenAI Secure MCP Tunnel
  → tunnel-client on home-ubuntu
  → 127.0.0.1:8767/mcp
  → runtime/data/auto_app.db
```

## Tools

| Tool | Purpose |
|---|---|
| `get_job_context` | Counts, source health, effective preferences and capability flags |
| `search_jobs` | Search by query/company/source/state, with cursor pagination |
| `get_job` | Stable job ID, source evidence, judgments and manual history |
| `record_application_status` | Record `want_to_apply`, `applied` or `not_applied`, with an optional user-supplied reason |
| `prepare_application` (opt-in) | Queue one Ashby form using the fixed fictional candidate; supply exactly one saved `job_id` or official `application_url` |
| `get_application_run` (opt-in) | Read the durable preparation status, field checks and missing questions |

Searches exclude Canada using the existing preferences. Direct details retain
the original evidence. Pagination must reuse the same filters. Read tools never
migrate, expire, or modify the database. The write tool changes one job and its
review/feedback in one transaction. Repeating the same current status and reason
does not create another review. An applied job cannot be moved back by this tool.
Writing `applied` records the user's report; no application is submitted.

## Fictional Ashby preparation v1

This path is for explicitly requested fictional testing. It accepts only HTTPS
`jobs.ashbyhq.com/<employer>/<UUID>` application links (normalized to
`/application`) or a saved job with such an official link. It reads the saved
job without changing its decision. Contact details and the test PDF come only
from the versioned `fictional-sales-v1` package; real profiles, custom resumes,
credentials and answers cannot be supplied through these tools.

1. Discover `prepare_application` and `get_application_run` in the current
   callable namespace, then inspect `get_job_context().capabilities.preparation`.
   `enabled` reports configuration; `worker.available` separately checks a
   fresh heartbeat, live process and held worker lock.
2. Resolve the requested saved job ID with `get_job`, or use the explicit Ashby
   application URL. Call `prepare_application` with exactly one target. An
   identical target/candidate returns the same run rather than another browser
   task. `queued` is not evidence that the browser ran.
3. Read the returned `run_id` with `get_application_run`. Dot may disconnect
   and later retrieve the persisted result. Poll only while `queued`/`running`;
   a terminal result is not automatically retried.
4. Report field labels, expected/actual values, status and answer source from
   `result.field_checks`, plus `result.missing_questions` and any guard evidence.
   Unknown required work-authorization or sponsorship answers remain missing.
   v1 has no answer/resume editing tool, resumable browser or submit tool.

Saved `job_id` requests reject excluded, archived, duplicate and already-applied
jobs. Explicit `application_url` requests are independent fictional tests and
do not consult or change a saved job's decision; the preparation ledger still
deduplicates the normalized URL and candidate package.

| Run status | Meaning |
|---|---|
| `queued` / `running` | Saved for the worker / currently executing |
| `ready_for_review` | Observed form and enabled submit control; required values valid and matched, location committed, resume upload/attachment acknowledged; stopped before submit |
| `needs_input` | Required answers are unknown, missing or did not match read-back values; outstanding `ask_user` questions and model `done(needs_input)` also prevent readiness, including custom controls missed by DOM extraction |
| `action_needed` | Site or guarded operation needs attention; inspect the returned reason |
| `failed` | Execution/validation failed, including timeout; inspect the error |
| `interrupted` | Worker interruption exhausted the bounded recovery attempts |

The browser is ephemeral and closes when the run finishes. The report and
`filled.png` (when captured) live in `PREPARATION_DIR/runs/<run_id>/`; the tool
returns the report and screenshot filename, not image bytes or a live tab.
The separate `preparation.sqlite3` stores requests/results. Nothing in this
workflow records `applied`, changes library decisions or enables the legacy
automated appliers. Simplify is reserved as an interface and is not invoked.

Ashby autosaves values while the form is filled. Preparation permits three exact
observed GraphQL documents: `ApiCreateFileUploadHandle`, `ApiSetFormValue`, and
`ApiSetFormValueToFile`. They are bound to the selected employer/job and approved
fictional fields. The upload transport reconstructs multipart file bytes from
the fixed PDF, validates the observed signed-upload destination, and checks both
storage success and the attachment acknowledgement. Final submission remains
unavailable. Every permitted fetch disables redirect following and rejects 3xx
responses. Draft values and the test attachment can therefore reach Ashby
without a submitted application; the returned report distinguishes these states.

Readback rejects equal-looking values when native validity or `aria-invalid`
reports an error. An autocomplete query alone is not a committed location.
Required account/password fields return `action_needed`; unknown required
answers return `needs_input`. These states require a new implementation or a
later approved version of the workflow, because v1 has no answer-update tool.

Use the same preparation directory for MCP and worker:

```bash
.venv/bin/auto-app-agent serve \
  --db /home/postedism/Desktop/job-search/runtime/data/auto_app.db \
  --preferences /home/postedism/Desktop/job-search/runtime/config/preferences.json \
  --preparation-dir /home/postedism/Desktop/job-search/runtime/preparation \
  --transport streamable-http --host 127.0.0.1 --port 8767

.venv/bin/auto-app-agent prepare-worker \
  --preparation-dir /home/postedism/Desktop/job-search/runtime/preparation \
  --driver claude --browser-channel chrome --task-timeout 240
```

`--once` processes at most one queued task. `--task-timeout` accepts 1–900
seconds (default 120). The command above matches the deployed worker; CLI defaults
remain `--driver rules` and 120 seconds. `--browser-channel chrome` explicitly
selects installed Chrome. Without a channel, the browser tries installed Chrome
then bundled Chromium. Install Playwright's browser/runtime
dependencies as the worker user. `--driver rules` needs no model login;
`--driver claude` runs the constrained Claude Agent SDK driver and requires a
working Claude Code login for that same Ubuntu user. A login in another app or
on the Mac does not prove Ubuntu execution. Keep login credentials out of chat,
source snapshots and plugin archives.

`deploy/auto-app-preparation.service` is the separate user-service template for
the existing Ubuntu layout. Review its paths and the MCP service's matching
`--preparation-dir`, install the worker service under `~/.config/systemd/user/`,
then reload systemd and restart the selected services. Its process lock allows
one worker per ledger. Startup recovery happens only after acquiring that lock;
an interrupted task may have at most two total attempts. This does not change
the ingestion service or its schedule.

The 2026-10-11 deployment has the preparation worker enabled alongside ingestion,
MCP and tunnel services; all four were active with zero automatic restarts at
the deployment check. The actual Ubuntu MCP protocol lists six tools. A live
Claude/Chrome Ramp Sales fill-only probe matched all six required fields, with
no missing questions and verified location/upload/attachment acknowledgements.
See the [preparation verification record](updates/2026-10-11-ashby-preparation.md)
for report files. The production MCP queue run also reached `ready_for_review`
on attempt 1 with six required fields matched and no missing questions. Its
unchanged result survived worker/MCP restart, and an identical request reused
the same run without another browser execution. The full Ubuntu regression
recorded 951 passed, two failures and one skip; both failures were fixed, then
all 132 preparation tests passed again. See the dated record for exact scope.

The private cloud plugin `0.2.0` was uploaded and its version checked in the UI.
Refreshing the connected backend succeeded; the UI lists four read/two write
tools, including both preparation tools. The runtime subsequently discovered all
six tools. At 23:15 PDT on 2026-10-11, actual connected-cloud calls to
`prepare_application` and `get_application_run` both succeeded for the selected
Ramp Sales URL. The first call reused production run
`64f925a5-8147-46a2-8d5d-43d319658c27`; the second returned the identical completed
result: six required fields matched, zero missing questions, submission disabled.
`get_job_context` reported the Claude worker available before and after, and
the applied count stayed zero. This completes the cloud callability check;
idempotency prevented another browser execution. The earlier production run
remains the evidence for actual worker filling. See
[the cloud call proof](../../reports/2026-10-11-ashby-preparation/cloud-call-proof.json).

## Run on Ubuntu

From the deployed source directory, after `uv sync --frozen --python 3.12`:

```bash
.venv/bin/auto-app-agent serve \
  --db /home/postedism/Desktop/job-search/runtime/data/auto_app.db \
  --preferences /home/postedism/Desktop/job-search/runtime/config/preferences.json \
  --transport streamable-http --host 127.0.0.1 --port 8767
```

Default transport is `stdio`. HTTP is restricted to loopback and uses `/mcp`.
There is no application OAuth layer in this personal deployment: access from
ChatGPT is controlled by the private tunnel's account/workspace permissions.
Do not expose this unauthenticated listener publicly or share the private app.

`deploy/auto-app-agent.service` is a user service for the existing
`~/Desktop/job-search` layout. If that layout differs, edit the paths first.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/auto-app-agent.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now auto-app-agent.service
systemctl --user status auto-app-agent.service --no-pager
```

## Private tunnel

Use the [official Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
and the latest [OpenAI tunnel-client release](https://github.com/openai/tunnel-client/releases/latest).
Run the client on Ubuntu, as the same user that owns the service. It connects
outbound to OpenAI; no Mac SSH relay or public inbound port is required.

Create a tunnel in [Platform settings](https://platform.openai.com/settings/organization/tunnels),
associated with the personal Platform organization and the ChatGPT workspace
where Dot runs. The runtime principal needs Tunnels Read + Use; creating the
tunnel needs Read + Manage. Create a separate runtime key from the Platform
runtime API keys page; do not give the daemon an admin key.

Store `CONTROL_PLANE_API_KEY=...` in
`~/Desktop/job-search/runtime/credentials/tunnel.env` on Ubuntu, readable only by
the owner. Never paste the key into chat or commit it. The file is outside the
source snapshot. Use the real tunnel ID in this command:

```bash
~/Desktop/job-search/bin/tunnel-client init \
  --sample sample_mcp_remote_no_auth --profile job-search \
  --tunnel-id YOUR_TUNNEL_ID \
  --mcp-server-url http://127.0.0.1:8767/mcp \
  --health-listen-addr 127.0.0.1:8768
```

Check that 8768 is free before using it. `tunnel-client doctor --profile
job-search --explain` requires the runtime key in its environment. Then install
`deploy/job-search-tunnel.service` as a user service and enable it. Confirm
`http://127.0.0.1:8768/readyz` succeeds before registering the ChatGPT app. Both
services use systemd supervision and must run on Ubuntu. `Linger=yes` allows the
user services to survive logout and start without an interactive login.

## Register the cloud tool and Skill

In ChatGPT Plugins, select the **Personal** directory, then choose
**Add → Add custom MCP server**, select **Tunnel**,
use the real tunnel ID, choose **No authentication** within this private tunnel,
and register the app. Verify the four base tools appear (six when preparation
is configured). Keep the plugin private.

Build the Skill plugin using the registered `asdk_app_...` ID:

```bash
python3 scripts/build_dot_plugin.py \
  --app-id asdk_app_ACTUAL_REGISTERED_ID \
  --output /tmp/job-search-dot.zip
```

Upload the archive through ChatGPT's **Add → Upload plugin archive** and install
it. The package uses `.app.json` to refer to the registered cloud connection. It
does not contain `mcp.json` or `.mcp.json`: those are desktop-only plugin paths.
A `--skills-only` bundle contains instructions but does not connect a database.
The [official plugin management guide](https://learn.chatgpt.com/docs/enterprise/plugin-management)
explains the distinction.

## Acceptance checks

1. Use a real MCP client on Ubuntu to initialize, list all tools, query the
   runtime database and fetch one result's details. Confirm Canada exclusion.
2. Test state writes, repeated writes, error handling and restart persistence
   against a separate disposable database. Never invent real application history
   for a smoke test.
3. In ChatGPT/Dot, ask for context and one job by ID through the installed app.
   Inspect actual tool results; a registered connector alone is not success.
4. Stop the Mac's local client or disconnect it, then ask Dot to query again.
   The tunnel and MCP service should continue running on Ubuntu. Record this
   separately from backend test success.
5. When preparation is enabled, use the real MCP protocol and queued worker
   against synthetic form fixtures; check result persistence after restart,
   unknown required answers and field mismatches. Verify a live Ashby fill-only
   probe separately. In Dot, discover both preparation tools and call them with
   the authorized test target; verify the final report and unchanged job state.

The tools do not add full opportunity qualification, saved public company
research, résumé generation, mailbox authorization, or automated submission.
The context response reports these capability gaps explicitly. Existing Gmail
and Outlook OAuth setup remains stopped until the user resumes it.

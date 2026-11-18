# Dot connected to the Ubuntu job library

**2026-11-17 handoff checkpoint.** A new Dot-managed cloud task read the live
screening rules and invoked `propose_screening_change`. Both the initial call
and an explicit plugin retry timed out in chat. Ubuntu later saved both
no-change proposals. The preview took over 3 minutes against the tunnel's
roughly 2-minute deadline; a preview that takes about 35 s on a copy of the live
database is implemented locally and not deployed yet. A separate Dot cloud task
passed the isolated handoff acceptance, and the real private listener now
advertises the handoff tools. The Skill source is 0.4.1; no ChatGPT upload of
0.4.0 or 0.4.1 is recorded. Dot has not yet filled a real form, uploaded a file
into a web form, run a question or change round, or submitted anything. See
[deployment and acceptance](#option-c-private-handoff)
and [the verification record](updates/2026-11-17-dot-loopback-handoff.md).

**2026-10-21 compatibility review:** see [review and rollout boundaries](reviews/2026-10-21-dot-compatibility.md).
The current session discovers 18 tools and five read calls succeeded. At that review the local
plugin source was 0.3.1; it is now 0.4.1. Neither establishes the cloud installation.
Historical deployment claims below are dated.


**Current state (2026-10-14):** updated source is deployed on Ubuntu with schema
`8d4e0f2a5b31`; the existing private MCP service is active with `--control-tools`.
The backend UI refresh shows all 18 configured tools, classified **Read 8 / Write 10**.
Plugin **0.3.0** was uploaded and its version read back. Actual connected-cloud
`get_job_context`, `search_jobs` and `get_job` calls succeeded against the new code.
The already-running Codex session still has the older six-tool catalog; no new
control tool has been verified through that cloud session. Refreshing backend
metadata and having a callable tool in a running chat are separate checks.

The job database and feed worker stay on Ubuntu. Current code has five base
library/tracking tools, two optional fictional Ashby preparation tools, eleven
opt-in control/review tools and twelve opt-in handoff tools. On 2026-10-14 the
private service enabled the first three groups; since 2026-11-17 the persistent
`50-dot-loopback-handoff.conf` override also enables the handoff tools without
MCP authentication (30 tools). The Skill follows the tools actually
discovered in each session. The Mac is only used during setup.

Discord and Grok units are installed and systemd-verified but disabled/inactive;
only configuration examples are staged. Discord.py 2.7.1, MCP 2.3.0 and
checksum-verified cloudflared 2026.10.0 are installed. The owner fills real values
on Linux using the [connector setup checklist](linux-connectors-setup.md).
No public tunnel, Discord message or Grok call has been performed.

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
| `record_application_event` | Record an owner-reported application milestone in the timeline |
| `prepare_application` (opt-in) | Queue one Ashby form using the fixed fictional candidate; supply exactly one saved `job_id` or official `application_url` |
| `get_application_run` (opt-in) | Read the durable preparation status, field checks and missing questions |
| `get_pipeline_status` (controls) | Read ingestion, worker and pause state |
| `get_screening_rules` (controls) | Read effective screening rules and revision |
| `get_digest` (controls) | Read eligible pending digest jobs without marking them notified |
| `pause_pipeline` (controls) | Pause screening/enrichment/notification work and the bot digest |
| `resume_pipeline` (controls) | Resume those components; ingestion is independent |
| `propose_screening_change` (controls) | Save a proposed rule change and its preview |
| `apply_screening_change` (controls) | Apply the owner's confirmed proposal with the expected revision |
| `record_filter_feedback` (controls) | Save the owner's verdict about a filter result |
| `list_unmatched_updates` (controls) | Read saved application updates awaiting matching |
| `assign_update` (controls) | Assign an update to the job explicitly identified by the owner |
| `dismiss_update` (controls) | Dismiss an update the owner identifies as unrelated |

This table lists the 18 base, preparation and control tools. Since 2026-11-17
the private listener also serves the twelve handoff tools in the
[handoff contract](agent-handoff.md#cli-and-mcp), 30 in all. Neither list
guarantees that an already-open chat exposes each tool. The backend UI inventory
and the three actual cloud read calls above are the evidence for the 18; newly
added control calls still require discovery and direct verification in a
refreshed client session.

Searches exclude Canada using the existing preferences. Direct details retain
the original evidence. Pagination must reuse the same filters. Read tools never
migrate, expire, or modify the database. `record_application_status` changes one job and its
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
  --transport streamable-http --host 127.0.0.1 --port 8767 --control-tools

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

### Historical preparation acceptance (2026-10-11)

The 2026-10-11 deployment had the preparation worker enabled alongside ingestion,
MCP and tunnel services; all four were active with zero automatic restarts at
the deployment check. The actual Ubuntu MCP protocol then listed six tools. A live
Claude/Chrome Ramp Sales fill-only probe matched all six required fields, with
no missing questions and verified location/upload/attachment acknowledgements.
See the [preparation verification record](updates/2026-10-11-ashby-preparation.md)
for report files. The production MCP queue run also reached `ready_for_review`
on attempt 1 with six required fields matched and no missing questions. Its
unchanged result survived worker/MCP restart, and an identical request reused
the same run without another browser execution. The full Ubuntu regression
recorded 951 passed, two failures and one skip; both failures were fixed, then
all 132 preparation tests passed again. See the dated record for exact scope.

At that checkpoint, private cloud plugin `0.2.0` was uploaded and its version checked in the UI.
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
  --preparation-dir /home/postedism/Desktop/job-search/runtime/preparation \
  --transport streamable-http --host 127.0.0.1 --port 8767 --control-tools
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

tunnel-client 0.0.16 checks the MCP server and OAuth discovery once at startup
and does not retry a refused connection. If it starts before auto-app-agent
listens, as at boot on 2026-11-07, `/readyz` stays 503 for the life of the
process although the tunnel keeps working. Adding `startup_wait_timeout: 60s`
under `mcp:` in `~/.config/tunnel-client/job-search.yaml` makes it wait. It was
applied on home-ubuntu on 2026-11-17 (one tunnel restart, which briefly dropped Dot's connection) and
`/readyz` now returns 200 ([record](updates/2026-11-17-dot-loopback-handoff.md#follow-up-the-same-day)).

## Register the cloud tool and Skill

In ChatGPT Plugins, select the **Personal** directory, then choose
**Add → Add custom MCP server**, select **Tunnel**,
use the real tunnel ID, choose **No authentication** within this private tunnel,
and register the app. On 2026-10-14 the deployed private service had five base,
two preparation and eleven control/review tools, and its refreshed backend UI
listed all 18. After the 2026-11-17 handoff opt-in the refreshed app lists 30.
The dated 2026-10-11 six-tool acceptance record above describes the
earlier deployed version; the running Codex session's cached six-tool catalog
has not yet demonstrated callability of the newly added controls.
Keep the plugin private.

Build the Skill plugin using the registered `asdk_app_...` ID:

```bash
python3 scripts/build_dot_plugin.py \
  --app-id asdk_app_ACTUAL_REGISTERED_ID \
  --output /tmp/job-search-dot-0.4.1.zip
```

The script refuses to overwrite an existing file, so name the output after the
version in `plugins/job-search/plugin.json`. Upload the archive through ChatGPT's
**Add → Upload plugin archive**, install it and confirm the installed version
reads 0.4.1. The 2026-10-14 upload/readback confirmed version 0.3.0; no later
upload is recorded. The package uses `.app.json`
to refer to the registered cloud connection. It
does not contain `mcp.json` or `.mcp.json`: those are desktop-only plugin paths.
A `--skills-only` bundle contains instructions but does not connect a database.
The [official plugin management guide](https://learn.chatgpt.com/docs/enterprise/plugin-management)
explains the distinction.

## Option C private handoff

The owner can add `--handoff-tools --allow-unauthenticated-loopback-handoff`
to the private service after deploying the code. This works only on Streamable
HTTP bound to `127.0.0.1`. The local Host is `127.0.0.1:8767` for this tunnel
target. The server accepts an absent Origin or `http://127.0.0.1:8767` and
rejects other Hosts and Origins. Processes on Ubuntu that can reach this port
can invoke exposed tools without a token. Keep this endpoint behind the existing
private tunnel. This is accepted for a single-owner server: grokbot is blocked
by its firewall ([grok-bot.md §4](grok-bot.md#4-grokbot-账户的隔离边界)), and local
processes running as `postedism` can already read the same database and
`apply-kit/` files. The [stock systemd unit](../deploy/auto-app-agent.service)
requires a separate opt-in override, versioned as
[`deploy/auto-app-agent.service.d/50-dot-loopback-handoff.conf`](../deploy/auto-app-agent.service.d/50-dot-loopback-handoff.conf)
and installed on home-ubuntu since 2026-11-17.

### Ubuntu checklist

1. After owner approval, run `scripts/deploy_linux.sh home-ubuntu` from this
   checkout on the Mac. The script syncs the checkout, backs up and checks the
   runtime database, and resumes services. Unit overrides are installed
   separately. Confirm the existing `job-search-tunnel.service` still targets
   `http://127.0.0.1:8767/mcp` and check `http://127.0.0.1:8768/readyz` on Ubuntu.
   If readiness returns 503, use the [tunnel-client health guide](https://github.com/openai/tunnel-client/blob/master/docs/troubleshooting.md#debug-why-readyz-is-failing)
   to check startup discovery; on home-ubuntu the 503 since 2026-11-07 is the
   boot race described under [Private tunnel](#private-tunnel). Record health
   separately from actual tool results.
2. For cloud acceptance use a new private test runtime with its own sibling
   `apply-kit/`. In Bash from `~/Desktop/job-search/auto-application` on Ubuntu, create
   one marked fictional job and copy the repository's fictional PDF into its
   package. Save the printed root path, job ID and digest for the checks below.

   ```bash
   umask 077
   DOT_TEST_ROOT=$(mktemp -d "$HOME/Desktop/job-search/dot-handoff-test.XXXXXX")
   .venv/bin/python - "$DOT_TEST_ROOT" <<'PY'
   from hashlib import sha256
   import json
   from pathlib import Path
   import sys

   from auto_app.agent.service import AgentService
   from auto_app.screen.rules import ScreeningRules
   from auto_app.screen.store import screen_jobs
   from tests.imported_jobs import ingest, job_id_of, make_store, record

   root = Path(sys.argv[1])
   title = "Dot Fictional Handoff Acceptance"
   store = make_store(root / "runtime")
   try:
       ingest(store, [record("dot-acceptance", title, company="Dot Test Only",
                             url="https://forms.example.invalid/dot-acceptance")])
       screen_jobs(store, ScreeningRules())
       job_id = job_id_of(store, title)
       db = store.db_path
   finally:
       store.engine.dispose()
   AgentService(db).record_application_status(job_id, "want_to_apply")
   package = root / "apply-kit" / "jobs" / str(job_id)
   package.mkdir(parents=True)
   pdf = Path("src/auto_app/prepare/fixtures/fictional-sales-resume.pdf").read_bytes()
   (package / "resume.pdf").write_bytes(pdf)
   digest = sha256(pdf).hexdigest()
   (package / "job.json").write_text(json.dumps({"job_id": job_id, "title": title,
       "files": {"resume.pdf": {"sha256": digest, "bytes": len(pdf)}}}))
   print(f"root={root}\njob_id={job_id}\nresume_sha256={digest}")
   PY
   ```

3. Temporarily use `auto-app-agent.service` at 8767 for **that isolated DB** so
   the registered ChatGPT app reaches the fixture through its existing tunnel.
   Pause other Dot jobs that use this plugin during the switch. Keep a distinct
   `90-dot-handoff-test.conf` runtime drop-in and restore the previous target
   after at most seven minutes or when told to finish. The test service keeps the existing control and
   preparation tool schemas and points them at the isolated runtime. The public
   8769 service and the production `jobq` are separate. Use the same Ubuntu
   shell and checkout:

   ```bash
   (
   set -euo pipefail
   test -f "$DOT_TEST_ROOT/runtime/data/auto_app.db"
   dropin_dir="$XDG_RUNTIME_DIR/systemd/user/auto-app-agent.service.d"
   override="$dropin_dir/90-dot-handoff-test.conf"
   install -d -m 700 "$dropin_dir"
   (set -o noclobber; cat > "$override" <<EOF
   [Service]
   ExecStart=
   ExecStart=$PWD/.venv/bin/auto-app-agent serve --db $DOT_TEST_ROOT/runtime/data/auto_app.db --preparation-dir $DOT_TEST_ROOT/runtime/preparation --transport streamable-http --host 127.0.0.1 --port 8767 --control-tools --handoff-tools --allow-unauthenticated-loopback-handoff
   EOF
   )
   restore_route() {
     rm -- "$override"
     systemctl --user daemon-reload
     systemctl --user restart auto-app-agent.service
   }
   trap restore_route EXIT
   trap 'exit 129' HUP
   trap 'exit 130' INT
   trap 'exit 143' TERM
   systemctl --user daemon-reload
   systemctl --user restart auto-app-agent.service
   .venv/bin/python scripts/check_agent_mcp.py --url http://127.0.0.1:8767/mcp
   deadline=$((SECONDS + 420))
   while ((SECONDS < deadline)) && [[ ! -f "$DOT_TEST_ROOT/.cloud-test-finished" ]]; do
     sleep 2
   done
   )
   ```

   Confirm that the read-only smoke result lists handoff tools and the single
   fictional job. If the target is wrong, cancel the window before the cloud test.
4. Complete the ChatGPT checklist below. Check that the isolated lease was
   released with `AUTO_APP_HOME="$DOT_TEST_ROOT/runtime" .venv/bin/auto-app-jobq held`.
   It must return `{"items": []}`. The installed `jobq` wrapper pins the live
   runtime, so use the entry point above for the fixture. If a call times out,
   record its stage and inspect the fixture before retrying a write.
5. After checking the isolated result, use the recorded root in a second
   terminal and create the marker with `touch "$DOT_TEST_ROOT/.cloud-test-finished"`.
   Check that the first terminal exits, the runtime `90-dot-handoff-test.conf`
   is gone and `auto-app-agent.service` is active on the previous target. The
   window also restores the route on failure or timeout.
6. For normal operation after acceptance, install the versioned persistent
   override. It resets `ExecStart=` and repeats the stock unit's command with
   the real runtime DB, preferences, preparation and controls, plus the two
   opt-in flags:

   ```bash
   cd ~/Desktop/job-search
   install -d -m 700 ~/.config/systemd/user/auto-app-agent.service.d
   cp auto-application/deploy/auto-app-agent.service.d/50-dot-loopback-handoff.conf \
     ~/.config/systemd/user/auto-app-agent.service.d/
   systemctl --user daemon-reload
   systemctl --user restart auto-app-agent.service
   ```

   Verify live tool discovery with reads only. Because the override replaces
   the whole `ExecStart`, a later change to the stock unit's `ExecStart` must be
   made in this file too. To roll back, remove that specific persistent
   drop-in, reload and restart.

### ChatGPT checklist

1. In **Plugins → Personal**, open the existing private **Job Search Backend**
   MCP plugin. Select **Manage → Refresh tools** (管理 → 刷新工具). The
   connection stays on its existing private **Tunnel** and **No authentication**.
   Confirm the refreshed catalog exposes the handoff actions, including
   `submit_open_questions`, and has no `answers-ready`.
   [OpenAI plugin test guide](https://developers.openai.com/plugins/deploy/connect-chatgpt).
2. Build the 0.4.1 ZIP as described above (`--output /tmp/job-search-dot-0.4.1.zip`)
   and use **Add → Upload plugin archive**. Install the updated Skill for Dot
   with the private backend attached, and confirm the installed version reads
   0.4.1. Keep the connection and Skill private.
3. Ask Dot to start a new cloud task. In it, explicitly select the private
   plugin with `@` if needed. Restrict the run to the single fictional job ID
   and expected title confirmed on Ubuntu. Call in order:

   ```text
   get_application_queue
   claim_application(job_id=<fictional ID>, agent="dot")
   get_application_packet(token=<claim token>)
   get_application_file(job_id=<fictional ID>, name="resume.pdf")
   hold_application(token=<claim token>)
   get_held_applications
   release_application(token=<claim token>)
   ```

4. Compare the fetched PDF's SHA-256 with the fixture digest. Require hold to
   return `ready`, the held list to contain that ID under `dot`, and release to
   return `released: true`. Report the fields and any app approval or timeout
   without including the token or the PDF's base64. This exercise stops at the
   simulated pre-submit state and uses no employer form or submission call.
   `answers-ready` remains an owner-side CLI operation. The [Skill](../plugins/job-search/skills/job-search/SKILL.md)
   covers open questions, remaining pre-submit form work and batch review.

Dot has not yet attached a fetched PDF to any web form. Whether a cloud task
can turn the `base64` result into a browser upload for real-size PDFs (about
200 KB) is unverified; if it cannot, the Skill reports the error and releases
the claim. Keeping a filled form open in Dot's cloud browser for up to the
24-hour hold and reading it back later is also unverified. No employer form,
open-question or change round, re-claim with `resumed: true`, or submission has
run through Dot yet.

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

# Private Dot handoff via Ubuntu loopback

On 2026-11-17 a new Dot-managed cloud conversation read `get_screening_rules`
and invoked `propose_screening_change` with the existing `exclude_non_cs: true`.
The first call and a retry with the private plugin explicitly selected both
returned `MCP request timed out` in chat. Ubuntu later saved two proposals at
16:19:11 and 16:23:41 Pacific. Their saved rules matched the active rules, whose
revision remained `9ae0eab02ef0d941`. The preview write reached Ubuntu. Chat did
not receive the proposal ID or preview counts. No proposal was applied.
Tunnel health returned 200 for `/healthz` and 503 for `/readyz` after the test.
The readiness problem and successful server-side writes were observed separately.
The causes of both, and a faster preview, are in the [follow-up](#follow-up-the-same-day).

The `serve` CLI now permits an explicit `--allow-unauthenticated-loopback-handoff`
option alongside `--handoff-tools` on Streamable HTTP bound to `127.0.0.1`.
The server requires `Host: 127.0.0.1:<port>` and accepts only an absent Origin
or `http://127.0.0.1:<port>`. Static Bearer on 8769 keeps its existing
configuration. The stock private unit requires the owner's systemd override to
opt in. `answers-ready` stays on the owner-side CLI.

Any Ubuntu process that can open 127.0.0.1:8767 can call every handoff tool
without a token: claims, holds, outcome records (including `submitted` and
`skipped`) and the token-free package reads, `fields.json` included. The owner
accepts this for a single-owner server whose 8767 listener is reached from
outside only through the private tunnel. The grokbot account is blocked by its
firewall ([grok-bot.md §4](../grok-bot.md#4-grokbot-账户的隔离边界)), and local
processes running as `postedism` can already read the same database and
`apply-kit/` files.

Job Search Skill 0.4.0 covers Ubuntu package retrieval, open-question handback,
and holds for batch review. The updated archive was built and verified locally.
Its upload to ChatGPT is not recorded. All 2,617 tests passed, including 346 in
the agent, handoff and jobq suites.

Ubuntu deployment with `scripts/deploy_linux.sh home-ubuntu` completed on
2026-11-17. A bounded test window temporarily routed private port 8767 to an
isolated database and apply kit. After refreshing the existing private app to
30 tools, a new Dot cloud task called `get_application_queue`,
`claim_application(agent="dot")`, `get_application_packet`,
`get_application_file`, `hold_application`, `get_held_applications` and
`release_application` in order for fictional job #1. It reported the expected
2,898-byte PDF and matching SHA-256, `ready`, the hold under `dot` and
`released: true`. The independent database check found one `ready` application,
zero active claims and zero applied events. Isolated `jobq held` returned
`{"items": []}`. Open-question handback was exercised in local MCP tests.

Not yet done through Dot: no employer form has been filled, no file has been
uploaded into a web form, no open-question or change round has run, no job has
been claimed again with `resumed: true`, and nothing has been submitted. Dot has
not yet attached a fetched PDF to any web form. Whether a cloud task can turn
the `base64` result into a browser upload for real-size PDFs (about 200 KB,
against the 2.9 KB fixture) is unverified; if it cannot, the Skill reports the
error and releases the claim. Keeping a filled form open in Dot's cloud browser
for up to the 24-hour hold and reading it back later is also unverified.

The test window removed its temporary override and restored the real target.
The persistent `50-dot-loopback-handoff.conf` then enabled handoff on the
production private listener. A read-only cloud context call returned
`handoff_tools: true`. The final health check still returned HTTP 503 for
`/readyz` while `/healthz` returned 200. The [Ubuntu and ChatGPT checklists](../dot-setup.md#option-c-private-handoff)
record the setup, acceptance and rollback steps.

## Follow-up the same day

These changes are in the working tree. None is deployed to Ubuntu or uploaded
to ChatGPT yet, and nothing on the server or tunnel was reconfigured.

**Releasing a held form.** `release` after `hold` used to delete the claim but
leave the application `ready` with the note "form filled; waiting for the
user's go", so the job re-entered the queue with a stale status (the isolated
fixture's job #1 showed this before the fixture folder was removed). `release` now sets a held application back
to `queued` and moves `updated_at` in the same transaction that deletes the
claim. It clears the note only when it is exactly the hold's note (one constant,
`HOLD_NOTE`, shared with `hold`). Releasing a claim that was never held leaves
the application row unchanged. The return value and token rules are the same.
A hold that simply runs out (24 hours, no release) used to leave the same note behind once another
claim set the row to `queued`; a fresh, non-resumed claim now drops the hold's note too, keeping any
other note. Tests in `tests/handoff/test_held_and_kit.py` cover hold then release, release of a resumed
hold, a plain release, a release that keeps another note, and a fresh claim after an expired hold.

**Stronger option C tests.** Runs against in-memory copies of `cli.py` with
code removed showed two tests that passed without the code they protect.
- `test_handoff_requires_authentication_by_default` (stdio and Streamable HTTP)
  now uses a valid database, fails if the server starts, and checks the full
  refusal message.
- `test_loopback_handoff_pins_the_exact_host_and_origin` expects 421 for the
  Hosts `external.example:8767`, `localhost:8767`, `127.0.0.1:9999` and
  `[::1]:8767`, 403 for the Origins `https://external.example`,
  `http://localhost:8767` and `http://127.0.0.1:9999`, and 200 with no Origin
  or `http://127.0.0.1:8767`. The MCP library's default loopback lists would
  let `localhost:8767` through, so this row fails without the pinning.
- New `test_handoff_tools_with_authentication_still_start_through_the_cli`:
  `--handoff-tools` with a token file and a public URL starts, answers 401
  without the token and lists `claim_application` with it.

**Screening preview timeout.** `propose_screening_change` previews the change
over every active job. On a copy of the live database (42,879 active jobs) it
took 201–208 s. That matches the roughly 3 min 24 s between each Dot request and
its saved proposal, while the tunnel drops a response after about 2 minutes
("command response deadline reached"). Almost all of the time went into two
full screening passes, each re-reading every description with regexes,
although that reading does not depend on the rules.
- `screen/rules.py`: the rule-independent reading is now `evidence(facts)`, and
  `screen(facts, rules, read=None)` reuses it when given. Checks skip only text
  that cannot match: the citizenship check needs one of its key words, the
  sponsorship check a sentence with "sponsor", the years check a digit (except
  an "or …" alternative after an earlier requirement). Its regexes are cached.
- `agent/control.py`: the preview reads each job once and judges it under both
  rule sets.

On the same copy, five previews took 31–35 s, and their output matched the old
code exactly. A per-job comparison over all 42,879 jobs found no difference. A
full screening pass, which the worker runs after a rule change, went from about
100 s to 25–29 s. Two tests were added (`tests/screen/test_rules.py`,
`tests/agent/test_control.py`). Until this is deployed, Dot's
`propose_screening_change` still times out in chat. The time grows with the
number of active jobs (estimated about 60 s at 75,000).

**Tunnel `/readyz` 503.** It is a race at boot on 2026-11-07. Both units started
at 23:22:32 Pacific. tunnel-client 0.0.16 ran its one-time MCP startup probe
and OAuth discovery at 23:22:33 and got "connection refused"; auto-app-agent
began listening on 127.0.0.1:8767 at 23:22:40. Version 0.0.16 retries only
timeouts, so both failures are kept for the life of the process, and `/readyz`
reports `oauth discovery failed` with the failed MCP probe behind it. The
unit's `After=auto-app-agent.service` orders the start but does not wait for
the port. Only `/readyz`, the `ready` flag in `/health`, a metric and the admin
badge read readiness; the control plane and dispatcher work (33 commands
completed at the check). Run now, `tunnel-client doctor --profile job-search
--explain` passes both checks: the MCP server is reachable, and OAuth metadata
is not advertised (every candidate returned 404). So auto-app needs no change.

Applied 2026-11-17 18:20 PST, with the owner's go-ahead: `startup_wait_timeout: 60s` added under `mcp:` in
`~/.config/tunnel-client/job-search.yaml` (backup `job-search.yaml.bak-before-startup-wait-20261117`;
equivalents: `MCP_STARTUP_WAIT_TIMEOUT=60s` in a drop-in, or `--mcp.startup-wait-timeout=60s`), then
`job-search-tunnel.service` restarted once while auto-app-agent was up. `/readyz` and `/healthz` both
return 200. The log still shows a WARN that OAuth discovery read invalid (non-JSON) metadata from 8767,
which does not affect readiness. The wait also covers the next reboot, unlike a plain restart.

**Override in the repository.** The live drop-in is now versioned as
[`deploy/auto-app-agent.service.d/50-dot-loopback-handoff.conf`](../../deploy/auto-app-agent.service.d/50-dot-loopback-handoff.conf):
the stock unit's `ExecStart` plus the two opt-in flags, identical to the live
file apart from comments. It replaces the whole `ExecStart`, so a later change
to the stock `ExecStart` has to be made there too. Install and rollback are in
the [Linux checklist](../linux-connectors-setup.md) and
[Dot setup](../dot-setup.md#ubuntu-checklist).

**Skill 0.4.1** (`plugins/job-search/plugin.json`):
- Handoff tools are described as option C runs them (Bearer on the public
  listener, or the loopback opt-in on Dot's private one).
- Every uncovered open question goes back in one `submit_open_questions` call,
  required or optional, except optional personal questions, which are left
  blank and reported. Dot never writes, paraphrases, shortens or merges an
  answer, and never edits written answers, the resume or the cover letter.
- Dot never queues or skips jobs on its own. A claimed job that requires
  clearance, U.S. citizenship or U.S.-person status is recorded `skipped` with
  the reason.
- A hold lasts `hold_hours` (24 h by default).
- Attaching a fetched PDF to a web form is marked untested; if it fails, Dot
  reports the error and releases the claim.
- `propose_screening_change` may time out while Ubuntu still saves the
  proposal; Dot reports this instead of retrying in a loop.

Build it with a versioned output name, for example
`--output /tmp/job-search-dot-0.4.1.zip`: `scripts/build_dot_plugin.py` will not
overwrite an existing file. The supplied `job-search-dot-0.4.1.zip` matched the
repository Skill and the previous private app binding. On 2026-11-17, **Upload
new version** upgraded the existing Job Search plugin from 0.3.0 to 0.4.1.
Reloading the plugin page confirmed version 0.4.1, the updated Skill description
and the connected Job Search Backend.

**Tests.** `uv run pytest -q -p no:cacheprovider`: 2,626 passed, 1 warning (the
`audioop` deprecation in `discord/player.py`), in 597 s; 2,617 before. Added: four handoff tests and two
screening tests from the follow-up, then a release that keeps another note, a fresh claim after an
expired hold, and the "U.S. national" citizenship case. The agent, handoff and jobq suites now hold
353 tests (346 before; one of the screening tests is in `tests/agent`).

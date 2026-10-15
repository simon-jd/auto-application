# P2 application handoff — 2026-10-13

Local implementation of the approved [P2 plan](../superpowers/plans/2026-10-12-p2-agent-handoff.md):
guarded queue/claims, claim-gated packet, outcome recording, account metadata, resume registration,
CLI and authenticated opt-in MCP tools, plus worker/bot outcome notifications.
The [contract](../agent-handoff.md) describes actual interfaces, not an agent prompt. The system
does not call an agent/model or submit applications; the owner arranges external work.

Defaults: 20 submissions per UTC day, 3 per normalized company in 30 days, 30-minute leases. (Superseded: the day is now local and outcome recording no longer rechecks eligibility; see [the review fixes](2026-10-13-p2-p3-review-fixes.md).)
Eligibility includes manual/applied history, country and rule exclusions, duplicate warnings,
live leases and source/link evidence. Expired/consumed tokens fail; accepted attempt/application/
decision/event writes share a transaction. Manual applied reports and agent submissions retain
distinct timeline sources. `needs_user`/`blocked` park until explicit CLI requeue.

Decisions and costs:

- Reused the existing checkout to preserve uncommitted P0/P1 work; baseline copies support review,
  with less branch isolation.
- Active claims reserve daily/company quota until consumption, release or expiry. Capacity may
  temporarily be unavailable even before any external submission completes.
- Packet profile export uses an allowlist plus recursive secret-field removal. Unknown benign
  fields may be omitted and need explicit support later.
- Registered files are bounded, contained regular files and packet reads verify resume hashes;
  changing a file requires registering it again.
- Handoff tools require the authenticated HTTP listener; existing Dot/cloud schemas must be
  refreshed and accepted separately rather than assuming the local additions are live.

Notification cursors advance only on acknowledgment; bot outcomes use their own `.bot-state.json`
cursor. No-new-job rounds still deliver pending outcomes. Retries after a lost acknowledgment may
duplicate a message. In bot mode the worker keeps `screen,enrich,summary,health`.

Final local verification on 2026-10-13: **1792 passed, 1 warning in 534.05 seconds**
(`.venv/bin/python -m pytest -q --tb=short`). The warning is discord.py's existing `audioop`
deprecation. Independent specification and quality reviews passed; all confirmed findings were
fixed. Fingerprints/modes of 388 code, test, script and configuration files were unchanged during
the final complete run. Shell syntax and whitespace checks passed.

Earlier test runs exposed obsolete MCP tool inventories in the base and preparation protocol
tests. These now assert the exact current five/seven-tool sets; both real transports also verify
milestone persistence and idempotence. The final result above is from a fresh complete run after
those corrections, not a sum of targeted results.

Deployment, real-agent outcome/Discord acceptance and live client schema readback remain pending.
No real profile, secret or mailbox was accessed; no application submitted, deployment performed,
commit or push made. The existing local checkout and all prior work are preserved.

# Application handoff contract

P2 supplies interfaces for an agent arranged by the owner: queue, lease, packet, external-outcome
recording, account metadata and resume registration. It does not invoke an agent or model, provide an
agent prompt, open a browser or submit an application. The repository's [CLAUDE.md](../CLAUDE.md)
manual-application and no-secrets rules still apply. This is a local implementation; deployment and
real-agent/Discord acceptance remain pending. See [the dated record](updates/2026-10-13-agent-handoff.md).

## Runtime and guardrails

The CLI uses `$AUTO_APP_HOME/data/auto_app.db`; when unset, the checkout is the runtime home. MCP
derives the runtime home from its explicit database (`data/auto_app.db` uses the grandparent directory).
Handoff requires the P2/P3 schema migrations; the MCP server does not create or migrate its database.

The optional `$AUTO_APP_HOME/config/handoff.json` has the following defaults; the committed template is
[handoff.example.json](../config/handoff.example.json). Only these keys plus an optional `timezone`
are accepted. Each value must be an integer from 1 to 1000; booleans and unknown keys fail visibly.
`timezone` is an IANA zone such as `"America/New_York"`; it sets the daily limit's calendar day.

```json
{
  "daily_submissions": 20,
  "company_window_days": 30,
  "company_limit": 3,
  "lease_minutes": 30
}
```

The daily limit uses local calendar days: the `timezone` zone, or the host's local zone when unset
(00:00 UTC would reset the limit mid-evening in the US). Company limits use normalized company identity over a rolling window.
Submitted applications, reported manual applications and applied events count once per job; active
claims reserve daily and company capacity until consumed, released or expired. A full quota can
therefore produce an empty queue even while `submitted_today` is below the limit. Claim and outcome
writes serialize their checks with SQLite transactions.

Only active `want_to_apply` jobs are eligible, ordered by decision time then job ID. Archived jobs,
duplicates, country/rule exclusions, prior applied history, submitted applications, applied-repost
warnings, parked applications, live claims, inactive/missing sources, gone links and unsafe application
URLs are excluded. Queue returns `items`, `total`, `blocked.daily_limit`, `blocked.by_reason` and
`submitted_today`. Items carry `job_id`, title, company, application URL, platform, sponsorship and
decision time. Queue and claim enforce the same guards, and packet rechecks them, so a changed
decision or saved exclusion stops an agent before it applies. Outcome recording does not: it reports
what already happened on the employer's site, and refusing a real submission would leave the job
queued for a second one. Eligibility evidence is read only for the jobs being judged, so a claim
holding the SQLite write lock does not scan the whole posting history.

## CLI and MCP

All `handoff` commands return JSON; validation errors return an error and a nonzero exit. The commands
below describe the contract using fictional IDs and a placeholder token. They do not authorize or
perform an external application. A token is a short-lived capability returned by `claim`; only its
hash is stored. Tokens never begin with "-", so command lines can pass them as plain arguments.
Do not commit or disclose tokens.

| CLI after `uv run auto-app handoff` | MCP with `--handoff-tools` | Inputs / result |
|---|---|---|
| `queue --limit 20` | `get_application_queue(limit=20)` | Limit 1–100; eligible items and blocked counts. |
| `claim 123 --agent example-agent` | `claim_application(job_id=123, agent="example-agent")` | Token, lease expiry, application ID and job. |
| `packet CLAIM_TOKEN` | `get_application_packet(token=...)` | Requires an unexpired live token. |
| `record CLAIM_TOKEN needs_user --note "verification pending"` | `record_application_attempt(token=..., outcome=..., note=..., evidence=...)` | Consumes the claim after one accepted outcome. |
| `release CLAIM_TOKEN` | `release_application(token=...)` | Returns an active claim without an outcome. |
| `requeue 123` | No MCP requeue tool | Owner resolves `needs_user` / `blocked`, then makes it queued; the job must still be active `want_to_apply`, and all guards still apply. |
| `account careers.example.com person@example.com --existing` | `record_site_account(site=..., email=..., created_by_agent=False)` | Host/email metadata only; omit `--existing` for an agent-created account. |
| `resume 123 "$AUTO_APP_HOME/resumes/example.pdf" --note "example"` | `register_resume(job_id=123, path=..., note=...)` | Server computes SHA-256 and byte count. |

The `jobq` command ([guide](grok-bot.md); `deploy/jobq` on the server) is a third entry to these services: it exposes `queue`, `claim`, `packet`, `record`, `release` and `account` under the fixed agent name `grok-bot`, accepts no evidence files or paths, and leaves `requeue` and resume registration to the owner's CLI.

A released or consumed token cannot record an outcome, nor can an expired one, except that a late
`submitted` is still accepted while nobody else has claimed the job (a re-claim replaces the token).
A `skipped` outcome changes the decision only while the job is still `want_to_apply`; a `submitted`
outcome on a job that is archived or merged as a duplicate records the applied event without a
decision change. Accepted outcome, attempt,
application status, decision and applied event are written atomically. `submitted` records the
caller's report of an external submission; the service does not independently inspect the website.

| Reported outcome | Application / attempt | Job decision / follow-up |
|---|---|---|
| `submitted` | `submitted` / `submitted`, submission timestamp | `applied`; reason identifies agent source; `applied` timeline event has source `agent`. |
| `needs_user` | `needs_input` / `needs_input` | Decision unchanged; parked until explicit requeue. |
| `blocked` | `action_needed` / `action_needed` | Decision unchanged; parked until explicit requeue. |
| `failed` | `failed` / `failed` | Decision unchanged; may be claimed again if eligible. |
| `skipped` | `dropped` / `unsupported` | `not_applied`; reason identifies agent source. |

Handoff MCP tools are opt-in. The network entry point requires both authentication flags and
Streamable HTTP; `--handoff-tools` without authentication is rejected, including over stdio. Existing
private Dot configuration does not silently gain these tools. See [remote MCP](remote-mcp.md).

```bash
uv run auto-app-agent serve --db /srv/job-search/runtime/data/auto_app.db \
  --transport streamable-http --host 127.0.0.1 --port 8769 \
  --auth-token-file /srv/job-search/runtime/credentials/mcp-token \
  --public-url https://mcp.example.com --handoff-tools
```

This is a configuration example, not a deployment completed here. The authenticated listener binds
loopback; the owner chooses any network exposure separately.

## Packet and files

The packet returns the saved JD/application link/screening, permitted profile fields, answer-bank
entries and registered resume paths/hashes. Profile reads are limited to private `profile/profile.yaml`
and `profile/answers.yaml`, at most 1 MiB each. Missing files yield empty mappings; invalid YAML fails.

Profile fields are allowlisted: `name` (first/last/preferred), email, phone, cv, `location`
(city/state/country/address1/address2/postal_code), `links` (linkedin/github/portfolio), `education`
(school/degree/field/start/end/gpa), and `experience` (company/title/start/end). Answer entries only
export question, answer, note, kind and adapt. The `accounts` section and recursively named secret,
password, token, OTP, credential and key fields are stripped. Unknown benign profile fields may also
be omitted; packet export is not an arbitrary YAML pass-through. Only deliberately non-secret data
belongs in allowed fields or notes.

Resume registration accepts regular `.pdf`/`.docx` files of at most 10 MiB beneath runtime `resumes/`.
Packet retrieval rereads each registered file and rejects changed hash/size.
Registration history is retained; the latest registration for each path supersedes its previous
fingerprint in packets, so re-registering an overwritten file makes the updated bytes available.
Evidence is an existing regular file of at most 10 MiB beneath runtime `evidence/`; this interface accepts a path, not a mail
ID. Traversal, symlink components and paths outside the corresponding folder are rejected. Metadata
registration neither generates nor uploads a file. Runtime profile, resume and evidence folders are
git-ignored; database backups cover rows, not the referenced files.

Account registration stores site, email, creator flag and note, never passwords. Unexpected fields,
including credential fields, reject the entire request. Password-manager access belongs to the
owner's external workflow, outside this API.

## Outcome notifications and acceptance

In webhook mode, the worker's `notify` step also sends pending external-agent outcomes even when no
new jobs are eligible. It advances `handoff_cursor` only after Discord acknowledges each batch.
In [bot mode](discord-bot.md), the bot owns these messages and persists its own cursor in
`<db>.bot-state.json`; the worker runs `screen,enrich,summary,health` so the webhook path retains weekly
summaries and health alerts without duplicating outcome delivery. Pause suppresses outcome delivery.
An accepted message followed by a lost response/state write can still duplicate on retry.

Local tests use fictional jobs/files and fake Discord transports. Real acceptance still requires an
owner-authorized external test, result readback through the intended client, Discord receipt, expired
token rejection and duplicate/quota refusal. No application, production profile access, network
listener activation or deployment was performed for this implementation.

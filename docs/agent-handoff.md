# Application handoff contract

P2 supplies interfaces for an agent arranged by the owner: queue, lease, packet, external-outcome
recording, account metadata and resume registration. It does not invoke an agent or model, provide an
agent prompt, open a browser or submit an application. The repository's [CLAUDE.md](../CLAUDE.md)
manual-application and no-secrets rules still apply. It is deployed on the Linux host; Grok Bot ran it end
to end through `jobq` up to the Submit button on 2026-10-21, and no real submission had gone through it by
2026-11-11. See [the dated record](updates/2026-10-13-agent-handoff.md). Open questions handed back to the owner and
holds for a filled form were added and deployed on 2026-11-17 ([record](updates/2026-11-17-handoff-questions.md)).
The list of held forms and token-free reads of the published packages in `apply-kit/` were added the same day; they
were deployed the same day ([record](updates/2026-11-17-held-kit-files.md)). Since 2026-11-17 Dot also reaches these
tools over its private loopback listener; it passed a fictional queue-to-release acceptance and has not filled a real
form ([record](updates/2026-11-17-dot-loopback-handoff.md)). A fix that returns a released hold to `queued` was made
the same day and is not deployed yet.

## Runtime and guardrails

The CLI uses `$AUTO_APP_HOME/data/auto_app.db`; when unset, the checkout is the runtime home. MCP
derives the runtime home from its explicit database (`data/auto_app.db` uses the grandparent directory).
Handoff requires the P2/P3 schema migrations; the MCP server does not create or migrate its database.

The optional `$AUTO_APP_HOME/config/handoff.json` has the following defaults; the committed template is
[handoff.example.json](../config/handoff.example.json). Only these keys plus an optional `timezone` and
an optional `company_groups` are accepted. Each of the five values below must be an integer from 1 to 1000;
booleans and unknown keys fail visibly. `timezone` is an IANA zone such as `"America/New_York"`; it sets the
daily limit's calendar day.

```json
{
  "daily_submissions": 20,
  "company_window_days": 30,
  "company_limit": 3,
  "lease_minutes": 30,
  "hold_hours": 24
}
```

`hold_hours` is how long `hold` keeps the claim on a filled form that waits for the user's go (see below).
The server's `runtime/config/handoff.json` has set `"daily_submissions": 200` since 2026-11-17; the company limit there
is still 3 in 30 days.

`company_groups` (optional, empty by default) lists names that are one employer for the company limit:
a group's name maps to the employer's other names, the way they appear on postings.

```json
{
  "company_groups": {
    "amazon": ["amazon web services", "amazon science"],
    "gm": ["general motors"]
  }
}
```

Every name is normalized the way a job's `company_norm` is (lowercase, no punctuation or parentheticals, no
trailing legal suffix such as Inc, LLC or Corp), so `"Amazon.com, Inc."` and `"amazon com"` are the same
entry. A file is rejected when a name sits in two groups, when a group's name is also listed inside another
group, or when a name normalizes to nothing; at most 500 groups of 50 names each. Only the company limit reads
the groups. Job identity, deduplication and what a queue item shows are unchanged.

The daily limit uses local calendar days: the `timezone` zone, or the host's local zone when unset
(00:00 UTC would reset the limit mid-evening in the US). Company limits use normalized company identity over a
rolling window, and names listed together in `company_groups` count as one company.
Submitted applications, reported manual applications and applied events count once per job; active
claims reserve daily and company capacity until consumed, released or expired. A full quota can
therefore produce an empty queue even while `submitted_today` is below the limit. Claim and outcome
writes serialize their checks with SQLite transactions.

Only active `want_to_apply` jobs are eligible, ordered by decision time then job ID. Archived jobs,
duplicates, country/rule exclusions, prior applied history, submitted applications, applied-repost
warnings, parked applications, applications waiting for answers (`waiting_for_answers`), live claims (a hold is a
live claim), inactive/missing sources, gone links and unsafe application URLs are excluded. Queue returns `items`, `total`, `blocked.daily_limit`, `blocked.by_reason` and
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
| `claim 123 --agent example-agent` | `claim_application(job_id=123, agent="example-agent")` | Token, lease expiry, application ID, job and `resumed` (true when the same agent picks up its own hold). |
| `packet CLAIM_TOKEN` | `get_application_packet(token=...)` | Requires an unexpired live token. |
| `record CLAIM_TOKEN needs_user --note "verification pending"` | `record_application_attempt(token=..., outcome=..., note=..., evidence=...)` | Consumes the claim after one accepted outcome. |
| `release CLAIM_TOKEN` | `release_application(token=...)` | Returns an active claim without an outcome. A held form (application `ready`) goes back to `queued` and its hold note is cleared, in the same transaction; any other note is kept. Releasing a claim that was never held leaves the application as it was. |
| `hold CLAIM_TOKEN` | `hold_application(token=...)` | The form is filled and waits for the user's go: the claim lasts `hold_hours` and the application is `ready`. The same agent name may `claim` the job again for a fresh token (`"resumed": true`, the hold's end kept); others still see it claimed. |
| `held` | `get_held_applications()` | Read-only. Jobs whose latest application is `ready` (held) and whose claim is still live, oldest `held_since` first (then job ID): `job_id`, `title`, `company`, `apply_url` (the application's), `agent` (the claim's), `lease_until`, `held_since` (the application's last update; a re-claim by the same agent moves it). |
| `questions CLAIM_TOKEN FILE` | `submit_open_questions(token=..., questions=[...])` | Hands back open questions the package does not answer, or the user's change requests from the pre-submit check: 1–60 `{"text", "required", "limit", "page", "kind"}` (`kind` is `question`, the default, or `change` for the user's words verbatim; text 1–4000 characters, no control characters but line breaks and tabs, 64 KiB in all; unknown fields refused). Uses up the claim; the application becomes `needs_answers` and the job leaves the queue. |
| `answers-needed` | No MCP tool | Each waiting job's latest unanswered round of questions, oldest first. |
| `answers-ready 123` | No MCP tool | The owner's step once the answers are published: a `want_to_apply` job waiting for answers returns to `queued`, and the round gets `answered_at`. |
| `requeue 123` | No MCP requeue tool | Owner resolves `needs_user` / `blocked`, then makes it queued; the job must still be active `want_to_apply`, and all guards still apply. |
| `account careers.example.com person@example.com --existing` | `record_site_account(site=..., email=..., created_by_agent=False)` | Host/email metadata only; omit `--existing` for an agent-created account. |
| `resume 123 "$AUTO_APP_HOME/resumes/example.pdf" --note "example"` | `register_resume(job_id=123, path=..., note=...)` | Server computes SHA-256 and byte count. |
| No owner command (`jobq kit ID FILE`) | `get_application_file(job_id=123, name="resume.pdf")` | Read-only, no token. One file of the job's published package; `name` is `job.json`, `fields.json`, `jd.txt`, `resume.pdf`, `cover_letter.pdf`, `answers.md` or `message.txt`. MCP returns `job_id`, `name`, `bytes`, `sha256`, plus `text` (`.json`, `.md`, `.txt`; UTF-8) or `base64` (`.pdf`); `jobq` prints the raw bytes. `jobq kit` alone prints `jobs/index.json` and `jobq kit ID` the job's `job.json` as JSON. |
| No owner command (`jobq guide [cheatsheet\|readme]`) | `get_apply_guide(which="cheatsheet")` | Read-only, no token. `apply-kit/answers.md` (the cheat sheet, the default) or `apply-kit/README.md`; MCP returns `{"name", "text"}` with the file name, `jobq` prints the text. |

The `jobq` command ([guide](grok-bot.md); `deploy/jobq` on the server) is a third entry to these services: it exposes `queue`, `claim`, `packet`, `record`, `release`, `hold`, `held`, `questions`, `answers-needed`, `answers-ready`, `kit`, `guide` and `account` under the fixed agent name `grok-bot`, accepts no evidence files or paths, and leaves `requeue` and resume registration to the owner's CLI. Its `answers-ready` is refused when `SUDO_USER` is set (grokbot reaches `jobq` only through sudo); Claude Code runs it as `postedism` without sudo after republishing the answers (directly on home-ubuntu after the 2026-11-17 move, or over ssh from the Mac).

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
| (`questions`) | `needs_answers` / `needs_input` (pass `handoff`) | Decision unchanged; out of the queue until `answers-ready`. |
| (`hold`) | `ready` / no attempt | Decision unchanged; the claim lasts `hold_hours`, then the job is eligible again (the next fresh claim sets it to `queued` and drops the hold's note). |
| (`release` of a hold) | `queued` / no attempt | Decision unchanged; the job is eligible again at once. |
| `skipped` | `dropped` / `unsupported` | `not_applied`; reason identifies agent source. |

Enable `--handoff-tools` with Streamable HTTP and either the existing Bearer
configuration (`--auth-token-file` and `--public-url`) or the private
`--allow-unauthenticated-loopback-handoff` opt-in at `--host 127.0.0.1`.
The latter enforces the selected local Host and Origin and permits callers on
the Ubuntu host to reach the same tools. Dot's private 8767 listener has used it
since 2026-11-17 through the override
[`deploy/auto-app-agent.service.d/50-dot-loopback-handoff.conf`](../deploy/auto-app-agent.service.d/50-dot-loopback-handoff.conf).
See [remote MCP](remote-mcp.md) and the
[isolated Ubuntu and ChatGPT acceptance steps](dot-setup.md#option-c-private-handoff).

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
entries, registered resume paths/hashes and `open_questions`: the latest round of questions handed back for this job
(`submitted_at`, `answered_at`, `questions`), or null. Profile reads are limited to private `profile/profile.yaml`
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

### Published packages (apply-kit)

Claude Code's jd-resume-tailor skill publishes each job's package with `scripts/apply_kit.py` into an `apply-kit/`
folder that is the runtime's sibling: `HandoffService` reads `apply-kit/` in the runtime's parent directory, once the
database path is resolved (on home-ubuntu, `/home/postedism/Desktop/job-search/apply-kit`, next to `runtime/` and
`auto-application/`). The service only reads it:

- `jobs/index.json`: the published packages; `{"jobs": []}` while it does not exist yet. Invalid JSON, or anything
  but an object whose `jobs` is a list, is an error.
- `jobs/<job_id>/<name>` for the seven names above; a missing package or file is the error
  `job #<id> has no <name> in its package (not published yet?)`.
- `answers.md` (the agents' cheat sheet) and `README.md`.

These reads need **no claim token** (the owner's decision on 2026-11-17: Grok gets the materials through `jobq`, which
already runs as `postedism`, so the folder's permissions stay as they are). Whoever can run `jobq` or the handoff MCP
tools can therefore read every published package, including `fields.json`. Each path step is opened without following
symlinks (the `apply-kit` folder itself included), only regular files of at most 10 MiB are read, and the file names
are the fixed list, so nothing outside these files is reachable. A package's `job.json` gives, under `files`, the
SHA-256 and size of each file published with it, for a reader that wants to check what it received.

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
listener activation or deployment was performed for the original 2026-10-13 implementation. On 2026-11-17 the
private Dot listener enabled these tools through the loopback opt-in after an isolated acceptance
([record](updates/2026-11-17-dot-loopback-handoff.md)).

# Gmail, Outlook and recurring feed ingestion — 2026-10-10

**Step:** 1, 4  **Status:** implemented and verified locally; real mailbox authorization and Linux activation pending

## What and why

The shared job library can now receive scoped recommendations directly from Gmail and Outlook. A separate feed worker imports these sources and repository snapshots every 30 minutes by default, without starting browser collection or model filters. The user selected both providers and will supply Linux SSH details later.

## Changes

- `ingest/mail_auth.py`, `auth_cli.py`: Gmail Desktop OAuth with PKCE and a loopback callback; Outlook public-client device authorization; read-only mail scopes, offline refresh and atomic token rotation. Token files retain an account key and belong in ignored `credentials/`.
- `ingest/mailbox.py`: official message list/MIME APIs with explicit provider scope, a fixed date window, bounded pagination, successful-message receipts and resumed scans. Initial lookback defaults to 14 days, with 50 messages per page and four pages per round. Subsequent windows overlap by two minutes. Deleted messages receive zero-link receipts; expired cursors restart the same window. These reads never send, move, delete or mark mail read.
- `core/db/tables.py`, migration `c2f14e8a903b`: `mail_messages`, keyed by source/provider/account/message, preserving existing job decisions and separate provenance when accounts recommend the same job.
- `ingest/scheduler.py`, `schedule_cli.py`: persisted last/next run, reports and failures; restart recovery; config reload each round; worker/import locks shared with manual imports; SIGTERM shutdown; user systemd template generation. Interval is measured from the end of a round.
- `ingest/runner.py`, `cli.py`: mailbox config, source isolation and recorded source status; entry points for `auth`, `watch`, `status`, `systemd`.
- `query/service.py`: allowlisted per-source run and mailbox progress, without credentials or opaque page cursors. Scheduler state remains available through `ingest status`.
- README, Step 1 §14.5, Step 4 §15, CLAUDE, example feeds and the [setup guide](../mailbox-linux-setup.md): actual commands and connection/deployment requirements. No new runtime dependency.

## How to use

Follow the [Gmail/Outlook and Linux setup guide](../mailbox-linux-setup.md) for OAuth app setup and mailbox scope. Once authorized, enable the mailbox sources in your private `config/feeds.yaml`.

```bash
uv run auto-app ingest run --config config/feeds.yaml --db data/auto_app.db
uv run auto-app ingest watch --config config/feeds.yaml --db data/auto_app.db --interval 1800
uv run auto-app ingest status --db data/auto_app.db
uv run auto-app-query --db data/auto_app.db context
```

`watch --once` forces one immediate round. `ingest systemd` prints a user service; it does not install or start it. Detailed examples use explicit target-host paths.

## Tests / verification

- Provider, scheduler, authorization, schema and command behaviors were tested failing before implementation. Synthetic HTTP responses and temporary databases cover pagination, failed-message retry, gone messages, expired cursors, account/scope changes, token refresh/rotation, OAuth callback state, process locks, restart due times and SIGTERM handling.
- Both-provider integration refreshes synthetic expired tokens, imports the same job through two mailboxes, preserves two provenance entries and skips both messages on the next scan. Query exposes one job and no token content.
- Full regression: `AUTO_APP_HOME=/private/tmp/auto-app-test-home PLAYWRIGHT_BROWSERS_PATH=/private/tmp/auto-app-playwright .venv/bin/pytest -q` → **639 passed, 1 skipped in 341.42s**. The existing web smoke skips because `web/dist` is unbuilt. Browser tests used permission to launch synthetic Chromium outside the macOS sandbox; checks were not weakened.
- A final root check reproduced a status mismatch: failed mailbox detail showed `error` while its source summary still showed `ok`. A query regression first failed; the runner now writes both statuses together. After this fix, `tests/ingest`, `tests/query`, CLI structure, service and serve tests → **312 passed in 9.89s**.
- Executable CLI smoke in a disposable home: first `watch --once` created one job; second import reported one unchanged record and no new job; query returned one job; status persisted the next run exactly 1800 seconds after completion. The Linux user service was generated and checked for explicit config/DB paths. It was not installed.
- Independent code review, documentation/CLI checks and a targeted recheck of the final status fix found no remaining blockers. `git diff --check` passed.
- No real email, user database, application submission or remote Linux service was accessed. No commit/push.

## Follow-ups

Real activation needs the user's Google/Microsoft OAuth application configuration, chosen mailbox sender/label/folder scope and SSH target. Validate successful imports and a second round without duplicates on the target host before claiming it is running.

Full JD retrieval, broad per-track qualification, company research, existing resume-provider orchestration and shared task/chat delivery remain later batches. Automated applying stays disabled. A date-window scan does not discover arbitrarily old messages newly moved into scope; use a deliberate wider backfill/source when needed, as described in the setup guide.

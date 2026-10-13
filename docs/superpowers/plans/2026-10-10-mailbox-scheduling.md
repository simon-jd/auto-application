# Gmail, Outlook and recurring ingestion Implementation Plan

> User explicitly requested implementation of mailbox access and scheduled runs, selected Gmail and Outlook, and will provide Linux SSH access later. Continue the approved architecture without another approval gate. No commit/push or real account access without the connection being configured.

**Goal:** Read scoped recommended-job email from both providers and continuously import repository/mail inputs every 30 minutes on the user's Linux host.

**Architecture:** Official Gmail/Graph read APIs supply MIME bytes to the existing email parser. OAuth credentials live outside the database; successful-message receipts and bounded scan checkpoints live with the job library. A separate feed worker reuses `run_config` without depending on browser searches or model filters. Existing `serve` behavior remains compatible.

**Tech Stack:** Python stdlib, existing httpx/Typer/SQLAlchemy/Alembic/PyYAML. No new service or runtime dependency.

**Spec:** `docs/reviews/2026-10-09-job-agent-proposal.md`, email source section; the user's current Gmail/Outlook and Linux instructions.

## Constraints and review focus

- Read only explicit Gmail query/labels or Outlook folder/sender scope; do not send, delete, mark read, or create mailbox rules.
- No access to `.env`, `profile/`, real tokens or real mailbox during implementation. Synthetic OAuth responses, MIME and temporary databases only. Do not log token bodies.
- Preserve job IDs, decisions and all prior uncommitted work. Applying stays disabled.
- Failed messages/pages cannot advance past unprocessed data; empty/no-job messages still receive receipts; repeat/overlap scans do not re-import processed mail.
- Pagination is bounded per run and resumable; transient failures retry on the next run. Scope changes restart their scan. Token refresh is persisted, including rotation.
- The feed scheduler runs without searches.yaml, model credentials or configured filters; one source failure does not stop others. Restart respects persisted next-run time. One scheduler per DB and no overlapping import rounds.
- Implement/configure everything possible locally. Actual mailbox authorization and Linux installation are separate observable states, pending user-provided access.

## Shared contracts

Mailbox source definition: `kind: mailbox`, `provider: gmail|outlook`, `credentials: PATH`, optional `query`/`label_ids` (Gmail), `folder`/`senders` (Outlook), `lookback_days: 14`, `page_size: 50`, `max_pages: 4`. Paths resolve relative to feeds YAML. At least one explicit nonempty provider scope is required.

`mailbox.run_mailbox(store, source, definition, *, client=None, now=None) -> dict` returns ingest counters/status/issues plus messages_processed/messages_skipped, has_more, checkpoint. Stores parser observations as `kind='email'` and provider IDs in record metadata.

`mail_auth.access_token(credentials_path: Path, *, provider: str, client=None) -> str` refreshes expired tokens and persists rotation. Token file JSON: provider, client_id, optional client_secret, tenant (Outlook), access_token, refresh_token, expires_at (epoch seconds). It is read by application code only, never printed. Authorization commands live in the separate auth CLI module.

`mail_messages`: composite PK (source, provider, account_key, message_id), received_at UTC (nullable for deleted messages with no available timestamp), imported_at UTC, job_links integer. `account_key` identifies the credential file plus provider account identity (credential account key if known); folder/query changes may rescan but do not duplicate receipts. Checkpoint remains under `source_health.data.mailbox` with scope fingerprint, lower/upper bound, next page and last completed watermark. Reuse `update_health` merge semantics.

`scheduler.watch(config_path, db_path, *, interval_seconds=1800, once=False, ...)` owns durable schedule state beside DB and run locking; `scheduler.status(db_path)` is read-only. CLI: ingest watch/status/systemd; systemd unit runs watch, defaults to 30m, no browser/model startup. Installation waits for SSH target.

## Task 1: Provider retrieval and resumable import

- [x] Add `ingest/mailbox.py` and provider tests using actual HTTP-shaped fixtures, real parser/service/temporary DB. Root adds schema, migration, auth and runner wiring.
- [x] Prove initial fetch, paginated resume, no-job receipt, repeat scan, failed MIME retry, scope change and provider account separation.
- [x] Gmail lists messages with scoped query/date window and gets `format=raw`; Outlook lists filtered messages in folder and gets `/$value` MIME. Use only GET for mail.
- [x] Persist each successful message receipt after service commit; only commit page checkpoint after all messages in the page succeed. Retried pages skip existing receipts. Complete windows advance watermark with an overlap on next scan.

## Task 2: Recurring feed worker and Linux service

- [x] Add `ingest/scheduler.py` plus tests for clocks, restart/due time, isolation, shutdown, lock contention and state reads without database creation.
- [x] Add `ingest/schedule_cli.py` for watch/status/systemd registration; root wires main ingest CLI. Own separate service generation to avoid changing prior `serve systemd` contract.
- [x] Expose last run, next run, status/failures; reload config each run; exit cleanly on SIGTERM/KeyboardInterrupt. Generate a user systemd service so Linux deployment can use systemctl --user.

## Task 3: OAuth, schema and entry points

- [x] Add Gmail installed-app PKCE loopback authorization and Outlook device-code authorization, read-only scopes, refresh-token persistence and a CLI that prints only instructions/path/status. Test refresh rotation/errors, callback state and polling states with synthetic responses.
- [x] Add mail_messages migration/table; wire kind mailbox config/run and mail auth commands; credential paths never appear with contents in query results. Basic failure diagnostics describe action needed without token bodies.
- [x] Add Gmail/Outlook disabled examples and configuration instructions (client registration and scoped mail folders/queries). Tests prove config validation and source failure isolation.

## Task 4: Integrate, review and deliver

- [x] Extend lightweight read-only context with allowlisted mailbox/scheduler source health progress, excluding raw credential/checkpoint tokens.
- [x] Both-provider integration with shared job identity and expired-token refresh; executable CLI smoke plus restart recovery tests. Full suite with the established temporary browser/home environment, then only affected checks after review fixes.
- [x] Update README, Step 1, Step 4, CLAUDE and progress index/entry; include observed implementation versus connection/deployment status. No real mail or service activation claimed without validation.

## Execution record

- Baseline is the existing uncommitted feature branch. Previous full suite: 550 passed, one unbuilt frontend skip; final review-focused suite: 359 passed.
- Official Gmail message list/get, desktop OAuth, Microsoft Graph messages/MIME and Microsoft device authorization docs checked before choosing provider protocols.

- All implementation tasks completed; real mailbox authorization and Linux installation remain pending the user's OAuth/scope/SSH information.
- Provider/auth/scheduler tests used synthetic HTTP responses and temporary databases; no real account or private runtime data was read. Gmail and Outlook imports merge one shared job while preserving distinct provenance and skipping repeat messages.
- Full regression: **639 passed, 1 skipped in 341.42s**; existing web smoke skips because web/dist is unbuilt. Temporary AUTO_APP_HOME and Chromium paths were used, with permission for the synthetic browser launch.
- Final status consistency regression observed RED (source summary ok while mailbox run error), then fixed at the runner's health write. Final affected suite: **312 passed in 9.89s**.
- Actual executable CLI smoke passed: create one job, repeat unchanged, one queried job, durable next run exactly 1800 seconds after completion, explicit-path user service generated without installation.
- Independent implementation review, docs/CLI audit and targeted final status-fix review passed. README, Step 1, Step 4, CLAUDE and dated progress records updated; git diff --check passed. No commit/push.

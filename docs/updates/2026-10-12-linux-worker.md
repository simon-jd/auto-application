# Linux screening, JD enrichment and Discord worker — 2026-10-12

**Step:** 1, 2, 4  
**Status:** implemented; deployment pending

## What

The new `auto-app worker run / watch / status` processes the saved library through deterministic screening,
public employer-API description enrichment, rescreening and a sponsorship-first Discord digest. It calls
no model or AI agent and submits no applications. The accepted defaults target broad entry-level CS roles,
exclude internships, explicit senior/non-CS titles and citizenship/clearance/ITAR requirements, and exclude
explicit requirements of at least three years' experience. Sponsorship is preferred, not required.

## Why

Repository imports often contain only a title and an application URL. These jobs need descriptions and a
cheap, auditable screening view before manual review or user-run model filtering. A separate Linux worker
keeps this path independent of browser collection and the older `serve` model loop.

## Changes

- Alembic revision `5b1e0c7d2a91` adds URL enrichment caches and current per-job screening verdicts.
- Pure screening rules keep ambiguous evidence, reject negated/preferred false matches and expose reasons
  and sponsorship signals. Explicit experience requirements apply even to a new-grad title. An explicit
  non-CS title beats a generic repository category; CS title evidence, including FPGA, takes precedence.
- Imported Greenhouse, Lever, Ashby and Workday links use allowlisted public APIs, bounded per-host request
  rates and a 200-request round budget. Successful caches restore reimported descriptions; failures back
  off and unavailable jobs receive closure evidence without removing their history.
- Read-only CLI/HTTP/MCP queries hide rule-excluded jobs by default, expose audit evidence and accept
  sponsorship filters. `filter run` skips these exclusions while retaining manual decisions and history.
- Digests sort sponsorship offers first and distinguish user-filter recommendations. New title-only jobs
  wait two hours. A durable first-run cutoff prevents a backlog flood, and accepted batches alone receive
  `notified_at`; ambiguous acknowledgments or a crash before that write can still cause a retry duplicate.
- The durable loop uses a 900-second start-to-start cadence, no overlapping rounds, persisted restart
  deadlines and consecutive failure counts for partial/error rounds. Screening failures prevent a stale
  verdict from reaching a notification; step failures otherwise remain isolated.
- `deploy/auto-app-worker.service` and executable `scripts/deploy_linux.sh` are installation artifacts.
  Deployment stops previously active services, makes a private verified SQLite backup **before** syncing
  code, installs dependencies, explicitly migrates, then restores those services. It preserves private
  remote config and excludes local credentials/data/agent state/build caches. Failure stops progression;
  recovery remains an operator action.

## How to use

```bash
uv run auto-app worker run --steps screen
uv run auto-app worker run --steps screen,enrich --enrich-limit 20
uv run auto-app worker status
uv run auto-app-query jobs --sponsorship offers
uv run auto-app-query jobs --include-excluded
```

Configuration, supported URL forms, webhook setup, scheduling, deployment ordering and the exact live
acceptance gates are documented in [Linux worker](../linux-worker.md).

## Tests / verification

- Tests exercise pure rules and negation/ambiguity, persistence without decisions, cache recovery on
  reimport, bounded public requests, default query hiding and read-only legacy compatibility, partial
  Discord failures, first-run persistence, scheduling/restart and failed-screen notification suppression.
- Deployment artifact: **11 passed** in the final scoped run. Tests use fake SSH/systemd/dependency commands, real local `rsync`,
  SQLite backup and actual Alembic migrations in temporary directories. They verify order, backup contents
  and permissions, preserved remote configuration, exclusion of fictional private files, and failures at
  backup/sync/dependency/migration boundaries. They also cover restart-pending and deactivating units,
  missing units and failed systemd inspection. `bash -n` and `git diff --check` passed.
- `worker --help`, `worker run --help`, `worker watch --help` and `auto-app-query jobs --help` were checked
  against the documented commands.
- Final integrated repository run: `.venv/bin/python -m pytest -q --tb=short` — **1435 passed in 516.91s**.
  All 299 source, test, migration, script and service files matched their pre-run SHA-256 snapshot after
  completion. `git diff --check` also passed. Network-facing integrations used local fixtures/mocks;
  this result does not establish production deployment or actual Discord delivery.

## Independent review repairs

The final review reproduced and verified fixes for three cross-step failures: a job excluded after JD
write-back can still recover an interrupted projection from cache without a fresh request; negative
sponsorship clauses no longer become affirmative signals; and deployment explicitly stops units awaiting
automatic restart instead of treating an unsuccessful activity check as safely stopped. Service inspection
errors abort deployment. Additional regressions keep the worker out of the legacy model-credential loader,
redact malformed CLI inputs, and keep changing enrichment-failure counts from bypassing alert deduplication.

## Remaining work and evidence boundary

The user accepted the defaults and requested code plus local verification first. No SSH/deployment,
production configuration edit, webhook setup, real Discord message, model call, application, commit or push
was performed for this feature. The existing production service state has not been rechecked here.

Pending: deploy the reviewed snapshot; configure the owner-managed webhook; apply the four internship-feed
disables; run staged screening/enrichment checks; install/start the worker; manually audit 30 exclusions;
then observe 24 hours. Acceptance requires zero consecutive failures, successful latest steps, at least
90% JD coverage among active kept jobs on supported platforms, measured delivery within 15 minutes (or
2 hours 15 minutes for title-only jobs), an actual connected Dot sponsorship-filter call, and at most two
false exclusions in the 30-job sample. Record observed numerators, timestamps and any unmet criterion here.

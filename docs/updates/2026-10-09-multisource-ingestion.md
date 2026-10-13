# Repository and saved-mail ingestion — 2026-10-09

**Step:** 1  **Status:** second batch implemented and verified locally; mailbox connections and feed scheduling pending

## What and why

Repository listings and emailed recommendations now enter the same job library as website postings. Stable source identities and observation history preserve updates and decisions across repeated imports. Sparse recommendations remain available for later JD retrieval instead of being judged from invented details or removed by the existing TTL.

## Changes

- `src/auto_app/ingest/`: pure repository/email parsers, URL normalization, HTTP/file retrieval, persistence, config runner and three CLI commands. No new dependency, account connection or model call.
- `core/db/tables.py`, the ingestion Alembic migration: current source observations and append-only change history. Import summaries reuse existing batches; HTTP validators use source health.
- `process/`: reuse normalization/linking, veto conflicting requisitions, restrict sparse matches to exact identities, refresh current job projection, preserve `ingest-v1` data during general parsing.
- `select/engine.py`, `core/db/job_ttl.py`, `core/db/store.py`: automatic filtering skips pending-only evidence; unresolved imports without a decision hold TTL; other expired imports archive with history.
- `query/service.py`: optional ingestion context, compact per-job provenance and detailed payload/history; older compatible databases remain readable without migration.
- `config/feeds.example.yaml`, README, Step 1 §14 and the plan: executable inputs, incremental semantics and remaining work.

## How to use

```bash
uv run auto-app ingest repository listings.json --source saved-repo --db data/try.db
uv run auto-app ingest email first.eml second.eml --source recommendations --db data/try.db
uv run auto-app ingest run --config config/feeds.example.yaml --db data/try.db
uv run auto-app-query --db data/try.db context
uv run auto-app-query --db data/try.db jobs --source recommendations
```

Imports write/migrate the selected database. The example configuration includes two public repository feeds and a disabled local-mail source. Query commands are read-only. CLI reports use JSON stdout and diagnostic stderr; partial/failed results exit nonzero. See [Step 1 §14](../step1-collect.md#14-repository-and-email-ingestion) for formats, source names, missing/inactive handling, checkpoint rules and retention.

## Tests / verification

- New behaviors were tested failing before implementation; main registration first failed two command-tree checks. Broad focused checks before the final review fixes: **348 passed in 22.53s**.
- Independent review reproduced UTC conversion, inactive job renewal, unrelated-source linking, first-time evidence reassessment, conflicting requisitions on a reused URL, partial-snapshot checkpoint defects, and collisions with existing collector source names. Every correction has a regression case. Independent rechecking of the final four findings: **10 passed in 1.45s**.
- Public feeds were fetched and fully parsed: New Grad **19,689** records, Internships **17,188**, **0 row issues** in either snapshot. This verifies these feed shapes, not account access or every job's current availability.
- Actual command smoke used three active samples from each feed and one synthetic email linking an existing sample. Result: **6 jobs, 7 postings, 7 provenance entries**. Repeated imports reported unchanged **3 / 3 / 1**, with no new jobs. Query CLI and standalone script exposed pending state and history; complete business-data snapshots matched before/after reads. Temporary database/home were deleted.
- Final targeted suite (`tests/ingest`, query/core/process/select and CLI structure): **359 passed in 25.22s**, after all review corrections.
- Complete regression: **550 passed, 1 skipped in 341.77s**. Collection started before the final review corrections and their additional cases; the 359-case final targeted suite above verifies those final changes. The existing web-browser smoke is skipped because `web/dist` is not built.
- Actual main CLI help matches documented commands; `git diff --check` passed.
- Test environment uses a disposable `AUTO_APP_HOME` and Chromium under `/private/tmp/auto-app-playwright`; browser tests require permission to launch outside the macOS sandbox. Checks were not weakened.
- No real mailbox, user DB, application submission or Linux service was accessed. No commit/push.

## Follow-ups

The current feed commands run on demand. Next: mailbox providers, company-page JD retrieval, site access verification and track-based broad qualification. The existing service loop still schedules browser/plugin searches, not `feeds.yaml`. Public research, existing resume-generator integration and shared persistent task/chat interfaces follow the [approved plan](../reviews/2026-10-09-job-agent-proposal.md). Automated applying remains off.

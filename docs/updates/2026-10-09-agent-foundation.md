# Agent queries and Linux foundation — 2026-10-09

**Step:** 4  **Status:** first batch implemented and verified locally; Linux deployment pending

## What and why

Several agents and chat clients need to inspect the same job library quickly. Existing application commands load the wider runtime and create/migrate the database through `DbStore`. A separate query layer can read an existing database without those effects, while preserving current collection, filtering, UI and manual application behavior.

## Changes

- `src/auto_app/query/service.py`: standard-library SQLite read-only queries for context, filtered job pages and individual details. Source/location projection is limited to the selected page. ID cursors bind filters and bound new insertions; mutable records are read afresh per response.
- `src/auto_app/query/cli.py`, `scripts/job_view.py`, `pyproject.toml`: JSON CLI and Python script, without browser/model/UI/config imports. Missing database or incompatible schema returns an error rather than initializing data.
- `src/auto_app/query/api.py`: optional FastAPI GET context/list/detail adapter, local by default; no write routes.
- `src/auto_app/service.py`, `cli.py`: `serve systemd` generates a Linux system unit with explicit project, runtime home and uv path. It performs no installation.

## How to use

```bash
uv sync
uv run auto-app-query context
python3 scripts/job_view.py --db data/auto_app.db jobs --limit 50
uv run auto-app-query job 41
uv run auto-app-query serve
uv run auto-app serve systemd --project /srv/auto-application --home /srv/auto-app-home --uv /home/jobs/.local/bin/uv
```

Follow list `next_cursor` with unchanged filters until null. See [README](../../README.md#agent-interface-and-linux-foundation) for filters/API semantics and [Step 4 §14](../step4-run.md#14-agent-query-and-linux-foundation-2026-10-09) for Linux deployment preparation.

## Tests / verification

- New functionality was tested failing before implementation: 29 query-service cases, 7 CLI/API cases, 9 systemd cases.
- Focused query, service, existing serve and CLI structure regression after review fixes: 89 passed.
- Independent review found and verified fixes for cross-OS systemd path rewriting and separate-home filter documentation. The path regression failed before its fix and passed afterward.
- Installed console command, standalone script with Python `-S`, and API passed a disposable-database smoke check: all 3 IDs were returned across pages and the business-data dump remained unchanged.
- A local warmed timing check over 2,000 synthetic jobs without postings, 30 runs of open + context + first 50 jobs + close, measured median 1.72 ms and p95 2.55 ms (page size 26,691 JSON bytes). This is not a production or network-latency guarantee.
- Complete regression: **415 passed, 1 skipped in 334.46s**. The skip is the existing web-browser smoke test because `web/dist` is not built. This run was collected before the additional path regression; the final focused run above covers that extra test and the final changes.
- Verification environment: `AUTO_APP_HOME=/private/tmp/auto-app-test-home`, `PLAYWRIGHT_BROWSERS_PATH=/private/tmp/auto-app-playwright`. The initial baseline lacked Chromium (64 failed, 306 passed, 1 skipped); after installation the sandbox denied browser startup, so the full suite was restarted with permission to launch the test browser and passed. No test checks were weakened.
- `uv sync --frozen` installed the new entry point successfully; CLI help matches documented options; `git diff --check` passed. No commit or push was made.
- Tests use disposable databases/configuration. No real mailbox access, job collection, application submission or Linux deployment was performed.

## Follow-ups

The [approved direction](../reviews/2026-10-09-job-agent-proposal.md) includes repositories, recommended jobs from email, LinkedIn/Jobright/Simplify websites, broad opportunity qualification, public research with citations, external resume generators and persistent tasks. Those are subsequent batches in the [implementation plan](../superpowers/plans/2026-10-09-agent-foundation.md). This batch does not implement new source adapters, `possible/unknown/excluded` qualification, MCP, proactive delivery or shared task state. Automated applying remains off.

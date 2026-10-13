# Ashby Preparation Implementation Plan

> **For agentic workers:** Use executing-plans for integration and parallel bounded implementation tasks. Tests precede implementation; user explicitly authorized this first version.

**Goal:** Dot can enqueue a fictional Ashby preparation on Ubuntu and retrieve actual field checks and missing questions, with no submission.

**Architecture:** Separate persistent preparation ledger and worker, opt-in tools on the existing MCP endpoint, constrained browser executor. Reuse existing snapshots/agent SDK; preserve library and manual tracking.

**Tech Stack:** Python 3.12, sqlite3, Playwright, existing MCP 2.3 and Claude Agent SDK, systemd.

**Spec:** ../specs/2026-10-11-ashby-preparation.md

## Global Constraints

Ashby only. Fixed fictional-sales-v1 candidate. No real profile/.env/credential reads. No application submission. No Git commit/push. Existing library decisions unchanged. Do not replace unrelated working-tree changes.

## Review Focus

- Implicit submission, hostile button names, and network paths bypassing the visible Submit button.
- Filled controls showing stale/wrong values, radio groups and custom location inputs.
- Concurrent enqueue or worker restart causing duplicate browser runs.
- Arbitrary URL, file path or profile injection through MCP inputs.
- Empty/non-form pages being labelled ready or cloud tool availability being overstated.

## Task 1: Persistent preparation service

Files: prepare/candidate.py, urls.py, store.py, service.py; tests/prepare/test_service.py.
- [x] RED: enqueue same target concurrently and verify one run; reject unsupported URLs and immutable candidate substitutions; read job details without modifying decisions.
- [x] Implement fixed packaged candidate and resume hash, strict Ashby target normalization, durable idempotent ledger and bounded interrupted-run recovery.
- [x] GREEN: service tests and related query/agent tests.
Interfaces: RunStore and PreparationService exactly as specified in the spec.

## Task 2: Browser executor

Files: prepare/browser.py, session.py, verification.py, guards.py; tests/prepare/test_browser.py.
- [x] RED: real browser fixtures verify known values/readback, missing required answers, failed values, no-form detection and network/DOM submit blocking.
- [x] Implement rules prefill with existing field snapshots; optionally call constrained Claude driver. Capture screenshot/report and actual final values. Unknown values remain unanswered.
- [x] GREEN: browser fixtures, existing apply regressions. Test model invocation separately from scripted fixtures.
Interface: async fill_ashby(run, artifact_dir, *, driver=None, browser_channel=None, route=None).

## Task 3: MCP and worker

Files: agent/mcp_server.py, agent/cli.py, prepare/worker.py; tests/prepare/test_mcp.py, test_worker.py; deploy/auto-app-preparation.service.
- [x] RED: opt-in MCP tools enqueue/retrieve, unsupported calls fail; exclusive worker and restart persistence; old server still exposes only its previous tools when unconfigured.
- [x] Implement separate worker CLI, process lock, bounded jobs and crash recovery; task-level tools, capabilities and service templates.
- [x] GREEN: real stdio/HTTP MCP protocol checks on disposable databases.

## Task 4: Integration, documentation and deployment

- [x] Document prepare-only scope, test identity, returned questions and no live-browser handoff in README, step3/step4, Dot guide/Skill and dated update.
- [x] Run relevant tests then full suite on Ubuntu; retain concrete failures/skips.
- [x] Back up only changed deployed source files, copy allowlisted source snapshot, install worker service and restart MCP service; do not change existing ingestion configuration or job decisions.
- [x] Verify actual worker via MCP with fixture and live Ashby fill-only smoke, verify persisted results after service restart.
- [x] Refresh private cloud tools and check discovery/callability; report any client refresh dependency accurately.
- [x] Independent code review and fix important findings; final report includes test/deployment/external-submit states separately.

## Final verification record

The production MCP queue and Ubuntu Claude/Chrome worker completed a live fictional Ashby form with six required fields matched, no missing questions, and no submission. Results survived restart and an identical request reused the same run. All 132 preparation tests passed after fixing two failures found by the full regression (951 passed, 2 failed, 1 skipped before the fix). The full suite was not repeated after the preparation-only fix. Plugin 0.2.0 and the cloud six-tool inventory were refreshed. On follow-up, actual connected-cloud calls to both new tools succeeded and reused/read back that same completed production run; no new browser execution occurred. See [the dated record](../../updates/2026-10-11-ashby-preparation.md) for evidence.

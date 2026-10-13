# Ashby preparation v1

Approved scope: Dot selects a saved job or an explicit Ashby application URL; an Ubuntu worker fills an Ashby form using an isolated, clearly fictional candidate, reads back actual values, and returns field checks and missing questions. Stop before submission. Simplify remains an interface only. No commits/pushes requested.

## Architecture

Add `auto_app.prepare` beside the existing apply system. The existing job database stays read-only for this workflow; a separate SQLite ledger under an explicit preparation directory owns immutable request snapshots, idempotency, durable run state and results. Two opt-in MCP tools enqueue and inspect preparations. A separate single-worker service owns browser execution, with exclusive process locking and crash recovery. The existing manual tracking tool is unchanged.

Browser execution reuses the existing field snapshot/agent abstractions with stricter preparation-only controls. Basic fields use deterministic mappings; a dedicated constrained Claude SDK driver may handle controls, but only approved fictional values are available. Unknown answers become missing questions. Browser contexts are ephemeral and contain no personal account/extension data. Ashby only, single application form; login, account creation and multi-step progression stop for action. No submit API/tool exists. Final checks compare read-back values and the uploaded file against the versioned candidate package, not model assertions.

## Interfaces

`PreparationService(job_db, preparation_dir, preferences_path=None).prepare_application(job_id=None, application_url=None)` requires exactly one target. `get_application_run(run_id)` retrieves persistent results. `capabilities()` reports fictional preparation only.

`RunStore(root).enqueue(target, candidate)` returns a run dictionary; `get(run_id)`, `claim()`, `finish(run_id, result)`, `recover_interrupted()` manage the ledger. Run fields: run_id, status, job_id (nullable), title, company, apply_url, candidate (including candidate_id, profile, answers, resume_path, resume_sha256, package_hash), created_at, updated_at, attempt, result. Only queued/running/ready_for_review/needs_input/action_needed/failed/interrupted are allowed. Repeated identical target/package returns the same run. Recovery happens only while holding the worker lock; allow at most two attempts.

`async fill_ashby(run, artifact_dir, *, driver=None, browser_channel=None, route=None)` returns status, field_checks, missing_questions, guard evidence, screenshot name, candidate/resume hashes and usage. `driver=None` is deterministic rules; `driver='claude'` invokes the preparation-only constrained SDK driver. `route` is test dependency injection only, serving local fixture responses at approved Ashby URLs. Browser channel falls back from installed Chrome to bundled Chromium.

Each field check includes label, required, expected, actual, source, status (matched/missing/mismatch/unanswered/optional_empty), options where relevant. Missing questions describe required unknown/mismatching values. Summary makes test identity, stopped-before-submit, no persisted live browser, and any blocked operations explicit. A ready result requires a real form, at least one checked field, final submit control present, no unresolved required values and no mismatches.

## Constraints and acceptance

- No real profile, .env or credentials are read by the preparation subsystem.
- Fixed fictional-sales-v1 candidate only; records always identify test-only mode.
- HTTPS jobs.ashbyhq.com employer/UUID URLs only, normalized to /application; reject userinfo, ports, foreign redirects and unsupported targets.
- Existing job state, applied count, ingestion and preferences must remain unchanged.
- No final submit click, implicit Enter submit, native form submit, registration, or application-submission request may pass. Capture blocked attempts.
- Real browser regression fixtures cover standard fields, unknown required choices, incorrect read-back, missing forms, hostile submit controls, foreign navigation and duplicate requests.
- Ubuntu smoke uses the actual queued worker and MCP protocol, including persisted result after restart. A live Ashby fill-only probe is separately labelled from fixtures and never submitted.
- Dot's registered cloud tool discovery must be refreshed/verified separately from local MCP success.

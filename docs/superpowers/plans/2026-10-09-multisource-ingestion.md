# Repository and email ingestion Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for the independent parser and matching tasks. User approved the design and explicitly requested continuation; implement and review without another approval round. No commit/push.

**Goal:** Import multiple compatible repository feeds and recommended-job emails into the existing job library, with repeatable updates, provenance and explicit incomplete records.

**Architecture:** Pure parsers produce `IngestRecord`; a small ingestion service stores observations and writes ordinary postings, then reuses normalization/dedup. CLI commands accept files or configured HTTP feeds. Retrieval, parsing and storage stay separate so later mailbox/browser providers can reuse the same path.

**Tech Stack:** Existing Python, SQLite/SQLAlchemy/Alembic, httpx, Typer and PyYAML; stdlib email/HTML parsing. No new dependency.

**Spec:** `docs/reviews/2026-10-09-job-agent-proposal.md`, especially multi-source inputs and incomplete evidence.

## Global constraints

- Preserve all prior foundation work and existing user decisions; automated applying stays off.
- Use synthetic/disposable databases. Do not read `.env`, `profile/`, mailbox accounts or real browsing sessions. Public feed reads for format validation are allowed; do not import into a real user database.
- Record source status separately from confirmed official closure. A missing row is not proof the role closed.
- No LLM calls or automatic research/reassessment in this batch. Expose pending enrichment and changed-input status.
- Keep this batch focused on working ingestion. Multi-track qualification, mailbox authorization and authenticated website verification remain subsequent work.

## Review focus

- Identical imports must not duplicate postings/jobs or overwrite decisions; changed content retains history and refreshes job projection.
- Repeated multipart email links yield one recommendation per message/URL; subjects and generic button labels cannot become claimed job titles.
- Missing title/JD remains visible and pending, not automatically judged or fuzzily merged.
- Distinct employer requisitions must not merge due to identical templates.
- Malformed/failed/incomplete feeds cannot silently mark existing rows absent. A 304 still refreshes successful observation time; reimported archived entries stay archived.

## Shared contracts

`src/auto_app/ingest/models.py` defines `IngestRecord` and `ParsedFeed` dataclasses. Records contain external_id, url, optional title/company, locations, description, posted_at, employment_type, active and metadata. Metadata is source evidence, not instructions.

```python
parsed = parse_repository(content: bytes)  # ParsedFeed
parsed = parse_email(content: bytes)       # ParsedFeed
# Service consumes parser records; retrieval metadata is separate.
report = IngestService(store).ingest(source, parsed, kind=kind,
    origin=origin, revision=revision, complete_snapshot=True)
```

## Task 1: Pure sources

- [x] Repository parser and HTTP/local reader: same adapter for SimplifyJobs New-Grad-Positions and Summer2027-Internships listings JSON. Preserve inactive/hidden flags and terms; never infer that every internship is Summer 2027.
- [x] Write parser tests first: real-schema synthetic records, status/date handling, row errors, invalid document; reader conditional request and failure behavior. Implement `read_feed(location, etag=None, last_modified=None, client=None) -> ReadResult`.
- [x] Email parser tests first: HTML/plain text, multipart, repeated/tracking links, multiple jobs, missing title, message ID fallback, no-job mail. Implement parse-only `.eml`; do not connect accounts.
- [x] Verify focused parser tests.

## Task 2: Existing pipeline compatibility

- [x] Test distinct requisition/board IDs and sparse observations before fixing matching; preserve exact official-key matching.
- [x] Add `Item.pending_enrichment` from imported parsed extras. Pending records only match exact employer key and are skipped by automatic filter planning.
- [x] Extract `refresh_job_projection(store, items, job_ids)` to replace current keys/locations and recompute display fields without changing decisions/judgments. Use it for link-created/joined jobs and changed import records.
- [x] Run dedup/link/select regressions. No global pipeline/TTL cleanup in imports. General parsing skips imported observations; normal TTL retains unresolved imports and archives other imported jobs instead of deleting provenance.

## Task 3: Persist and expose import state

- [x] Test insert/repeat/update/missing/reappearance, cross-source same job, same-source archived reimport, partial feed, transaction rollback and decision retention.
- [x] Add Alembic tables `ingest_entries` (source/external ID, posting, latest payload/hash/status/seen) and `ingest_changes` (content/status revisions). Reuse existing batches for run summaries.
- [x] Store normalized CommonPosting with `extras.ingestion.enrichment_status` and `title_observed`. Use an explicit display placeholder only when no title is observed. Preserve record versions and source origin; expose `needs_reassessment` without fabricating a fresh judgment.
- [x] Extend read-only context/list/detail with optional ingestion metadata and history; old compatible databases remain queryable.

## Task 4: Commands, docs and verification

- [x] Add `auto-app ingest repository LOCATION --source NAME --db PATH`, `ingest email FILE... --source NAME --db PATH` and `ingest run --config FILE --db PATH` in a separate CLI module.
- [x] Add example feeds YAML with two verified repository URLs and optional local email paths. No edits to user's config or active schedule.
- [x] Verify CLI errors, JSON reports, per-source failure isolation, cursor/ETag updates only after success and invalidated conditional reads after partial writes, and query visibility end-to-end.
- [x] Update README, Step 1, progress log and first-batch plan linkage. Review independently, run focused and complete tests with disposable home/browser environment, and record exact results.

## Execution record

- Baseline from preceding batch: 415 passed, 1 frontend-build skip; final focused path correction 89 passed. This batch starts from the same uncommitted feature branch.
- Public feed schemas verified at the two upstream `dev/.github/scripts/listings.json` URLs. Both are complete snapshots with opaque UUID IDs; neither supplies full descriptions.

- Review added necessary integration coverage for old saved emails: pending imports without a decision hold TTL; other imported jobs archive rather than delete. `ingest-v1` records bypass browser-plugin parsing, including reparse. Tests first reproduced 8 failures, then focused retention/core TTL/process: 73 passed.
- Parser/reader, email and runner/CLI tests were observed failing before implementation. Main command registration first failed 2 command-tree checks; registration fixed both.
- Broad focused verification before final review corrections: 348 passed in 22.53s.
- Independent review reproduced and corrected UTC conversion, inactive observations renewing job TTL, unrelated normalized postings being linked on an empty import, and new source evidence failing to signal reassessment. Additional matching/checkpoint corrections and final verification are recorded in the update entry.
- Final reviewer corrections: generic URL identity no longer overrides a conflicting explicit requisition; new source evidence on an existing job flags reassessment; collector source names cannot be silently taken over; partial snapshot writes invalidate old conditional headers before writes. Independent review rechecked all four findings: 10 passed.
- Full regression: **550 passed, 1 skipped in 341.77s** (collected before final review corrections). Final targeted suite after all corrections: **359 passed in 25.22s**. Existing frontend-build skip unchanged.
- Actual public-feed/CLI smoke: 36,877 parsed rows, no row issues; six active samples plus one synthetic email yielded six jobs/seven postings/seven provenance entries; repeat imports unchanged 3/3/1. Read-only business-data snapshots matched.
- README/Step 1/update log/main CLI help aligned; diff whitespace check passed. No runtime account/data changes, service installation, commit or push.

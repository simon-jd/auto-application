# 2026-10-12 review remediation

Scope: the 15 confirmed findings in `claude-review-final.json` supplied by the user. Changes are local, layered over the existing working tree. No commit, production database modification, deployment or real application submission was performed.

## Fixes and regression coverage

| Finding | Repair | Regression coverage |
|---|---|---|
| Latest batch selects ingestion by text ID | Select latest collection by start time in both stores; explicit ingestion IDs format their own counters | Core database and collection unit tests |
| Error-only filter retries appear broken | Persisted successes for the exact current filter version count across passes/restarts; explicit auth/config errors still stop | Real select + Serve retry and stale-version tests |
| Excluded unjudged jobs get deleted | Archive preference-excluded jobs, using normalized and original posting locations; archived raw evidence still screens nonprimary locations | TTL dry-run/live and nonprimary archived location tests |
| Runner exception strands saved verdicts | Save runner exceptions as pair failures; apply saved decisions in cleanup, including progress callback failures | Serial/parallel runner failures and saved-result cleanup tests |
| Workday apply suffix becomes ID | Strip `/apply`, `/application`, `/apply/applyManually` before extracting requisition identity | Normalization and link tests |
| One collector aborts the batch | Isolate source exceptions, persist error health and finish the overall batch | Concurrent and interactive collection tests with both stores |
| Preparation ignores model questions | Merge outstanding `ask_user` questions into missing questions; `done(needs_input)` prevents readiness | Offline fictional Ashby button/control tests |
| ID veto splits location copies | Accept same Workday requisition/location suffixes, Lever location UUIDs, and JR/numeric equivalence; keep known distinct requisitions apart | Multi-location, numeric requisition and distinct R1/R2 tests |
| Ingestion repeatedly reloads everything | Batch-load relevant evidence, refresh only affected projections, checkpoint successful saved emails, invalidate old normalizer versions, update 304 timestamps in sets | Query-count, checkpoint, interrupted projection and existing ingestion tests |
| Serve silently exits successfully on errors | Validate window syntax; report runtime errors and exit nonzero after cleanup | Invalid-window and real subprocess exit tests |
| Transient/404 failures trigger cooldown | Retry transient session loads; exhausted sessions report error without cooldown; 404/410 never enter later retry; always finish retry runs | Session, terminal detail, retry block/error/cancel tests |
| Query API permits DNS rebinding | Validate Host and Origin on all requests; account for custom CLI bind host/port | Context/list/detail attack tests and allowed local/private hosts |
| Mixed Workday locations lose matches | Union resolved-facet results and unresolved fallback; preserve facet-proven IDs; report result-cap truncation as incomplete | Mixed-country/city and capped-result tests |
| LinkedIn import identity differs from collection | Keep a separate LinkedIn listing identity alongside its ATS identity; restrict pending matching without blocking complete group members | Collected/imported alert, pending group and generic URL conflict tests |
| Web posting links are null | Return source URL, company and posted time from the database projection | HTTP job-detail readback test |

## Compatibility decisions

- The review quoted older documentation saying different requisition IDs were never a veto. Existing ingestion tests correctly protect separate R1/R2 openings that share boilerplate. That protection remains; the specific multi-location and ID-equivalence cases are repaired.
- Exact generic URLs do not override conflicting role/company/requisition evidence. LinkedIn source IDs and supported ATS job IDs are stronger identities.
- Country exclusions remain reversible visibility preferences. TTL can archive excluded jobs; after clearing the preference, expired jobs appear in archived/all views, not as newly active openings.
- Existing sticky job IDs are not rebuilt automatically. The fixes govern subsequent normalization/import/linking; already incorrect merges or splits, and records deleted before this repair, require separate data repair/reimport decisions.
- Query Host/Origin checks prevent rebinding; they do not provide authentication for a publicly exposed server.

## Performance evidence

A synthetic temporary SQLite library of 7,700 records was used. No production performance claim is implied. Before the change, a 40-row unchanged snapshot emitted 609 SQL statements, a one-row change 612, and HTTP 304 495.

| 7,700-row operation after repair | Total | SQL statements | Longest measured write interval |
|---|---:|---:|---:|
| Unchanged snapshot | 0.593 s | 10 | 0.194 s |
| Snapshot with one changed record | 0.613 s | 25 | 0.344 s |
| HTTP 304 | 0.147 s | 3 | 0.105 s |
| Existing single-record import | 0.011 s | 10 | 0.001 s |

The write interval runs from the first DML statement to SQLAlchemy's commit event and excludes the underlying commit/fsync duration. First-time bulk imports still require full candidate matching and insertion.

## Verification

- Root storage/query/selection/service regressions: 165 passed.
- Preparation and web targeted run: 27 passed, 1 skipped before frontend build.
- Final ingestion/process suite after version-cache repair: 404 passed; dedup-focused suite: 121 passed.
- Collection/source suite: 105 passed; result-cap fixes additionally passed 26 related tests.
- Frontend dependencies installed from the existing lockfile; `npm run build` passed.
- The first full test attempt encountered missing Playwright Chromium and was stopped with 19 failures / 147 passes. The required test browser was installed. The subsequent full run passed: **1,035 passed in 496.16 s**. While that run was active, two normalization-cache compatibility regressions were added and fixed; a fresh run of the complete affected ingestion/process suites passed **404 tests**, including those two additions.

- `git diff --check` passed after integration. CLI help for query serving and service execution was checked. The Git head remains `008bf87`; no commit or deployment was made.

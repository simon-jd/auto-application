# Review remediation implementation plan

**Goal:** Resolve the 15 findings in the supplied Claude review, preserving the current working tree and manual-application boundary.

**Architecture:** Repair existing collection, identity, ingestion and presentation paths. Keep SQLite provenance and current APIs; add focused regression tests before each fix. Independent agents own disjoint code regions, with root reviewing integration.

**Tech Stack:** Python 3.12, SQLite/SQLAlchemy, pytest, Playwright, FastAPI.

**Spec:** `/Users/suture/Documents/Codex/2026-10-11/z-m/outputs/claude-review-final.json`

## Constraints

- Preserve pre-existing uncommitted work. Do not commit, deploy, touch production data or activate applying.
- Use temporary databases and fictional browser fixtures.
- Keep country exclusions reversible and source history intact.

## Tasks

- [x] Identity: Workday suffix stripping, multi-location merges, LinkedIn import/collection identity; tests in process and source URL suites.
- [x] Collection: isolate source exceptions, distinguish transient and terminal detail failures from hard blocks, preserve Workday location union; collection tests.
- [x] Ingestion: bulk loading, touched-job projection updates, unchanged email receipts and 304 set updates; ingestion correctness and query-count tests.
- [x] Storage: chronological collection-only latest batch and posting detail projection; core/web tests.
- [x] Retention: archive preference-excluded collected jobs, including raw location matches; TTL tests.
- [x] Selection: current-version prior successes across retries and isolate runner exceptions while applying saved results; select/serve tests.
- [x] Preparation: include outstanding ask_user questions and needs_input status in final readback report; fictional form tests.
- [x] Query API: validate Host and Origin with explicit custom-host configuration; API/CLI tests.
- [x] Service CLI: validate windows and preserve runtime exception diagnostics/nonzero exit; subprocess tests.
- [x] Integrate: review diffs, run complete pytest suite and frontend build, update design docs and dated update.

## Review focus

1. Exact source identities must still refuse unrelated generic URLs.
2. Current-version filter success must not hide authentication/configuration errors.
3. Interrupted or partially failed processing must preserve completed evidence.
4. Custom query ports must remain usable while attacker Host/Origin are rejected.
5. Performance changes must preserve refresh times, archived-job behavior and import history.

## Completion evidence

All 15 findings have code changes and regression coverage. Cross-review additionally caught archived raw-location visibility and Workday cap behavior; both were reproduced and fixed. Full suite: 1,035 passed. The later cache-version compatibility fix passed all 404 ingestion/process tests, including its two new regressions. Frontend build and whitespace checks passed. See the dated repair record for measurements and limitations. No commit, deployment or production-data repair was performed.

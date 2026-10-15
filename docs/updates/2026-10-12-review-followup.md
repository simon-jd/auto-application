# 2026-10-12 review follow-up

Scope: the user's follow-up findings after the first review remediation. Baseline is the fresh `JDinSeattle/auto-application` repository, initial commit `695e245`. Repairs are local; no live database repair, mailbox authorization, model rescoring, deployment, commit or push was performed in this follow-up.

## Repairs

| Finding | Change and regression boundary |
|---|---|
| Invalid preferences make a saved decision report failure | TTL defers otherwise-deletable jobs and emits a warning when preferences cannot be loaded. Real decision, implied-label and undo regressions verify persisted state. Safe archiving of judged/decided/imported records continues. |
| Every decision scans all posting location JSON | First determine expired deletion candidates; only those candidates require country checks. A decision with no candidates performs no location JSON query. Excluded secondary-posting locations remain protected by existing regressions. |
| Prose becomes a requisition ID | `job id when applying` no longer produces `WHEN`; numeric IDs and explicit uppercase ID fields remain supported. A dedup regression verifies the LinkedIn copy can match the company posting. |
| Review falls back to the oldest judgment | Prefer a non-error current judgment; otherwise use the latest non-error historical judgment. Implied feedback uses that same version. |
| Company exclusions match job titles; punctuation never matches | Exclude normalized title lines, including known title badges, from company evidence. Title terms use punctuation-aware boundaries, covering `Sr.` and `C++`. |
| Non-US locations pass the US filter | US names require token boundaries; Lever supplies country separately from location text, and an explicit non-US country overrides ambiguous state abbreviations. |
| Identical copied filters overwrite results | Filter names scope the version hash. Both results/catalog entries survive and the next unchanged batch makes no model calls. |
| Archived state conflicts with adapter defaults | Normalize explicit `state=archived` before cursor binding. Query and real in-process MCP pagination cover the default `archived=False` case. |
| A malformed Simplify URL aborts every feed run | Use canonical URL validation while parsing individual rows; invalid userinfo, host whitespace and port rows become isolated row issues. Valid neighboring records continue importing. |
| A bare `--uv uv` becomes an invalid local path | Resolve bare executable names through PATH; report a missing command, and preserve explicit executable path handling. |
| A batch crossing quota reset waits another day | Record actual runner failure times and calculate recovery from that evidence. Batch start is a fallback for legacy/custom reports; timezone and midnight cases are covered. |
| Salary parsing consumes the next word's initial | Magnitude suffixes need a word boundary; `Medical` after `$200,000` does not multiply the amount by a million. |
| Google/Tesla email job URLs are discarded | Accept the providers' exact job-detail routes before generic navigation-word rejection. Cached saved MIME files are reprocessed after the email-parser version changes. |
| One poison email freezes the scan | Keep two page-level retries, then persist a deferred message after its third failure and advance the scan. Deferred attempts have capped exponential backoff and bounded requests per run. Success/removal clears the retry; failures never receive a success receipt. Query context exposes pending/deferred counts without private message records. |

## Existing data and compatibility

- Shared parsing changes add a cache version to source parsers. Active postings with retained raw evidence are reparsed by normal processing; raw-purged records retain their previous parse. `ingest-v1` remains owned by the import adapter.
- Independent review reproduced stale `req_id` job keys after a linked posting was reparsed. Normal `link_jobs` now refreshes loaded existing projections too, preserving job IDs, associations, decisions and judgments. Preview remains read-only. This full processing path rewrites loaded projections; incremental repository/email ingestion retains its scoped refresh and its performance regressions.
- Existing stable job links are not rebuilt. Already merged/split jobs and historical values whose raw evidence is gone require a separate data repair decision.
- Filter hash scoping makes old hashes historical. Existing judgments remain readable; ordinary selection does not silently rescore them. Use explicit `--rescore` when current-version results are wanted, including collision remnants.
- Email parsing changes invalidate saved-file checkpoints. Mailboxes replay an unfinished window from its beginning or, after a completed scan, replay only the configured lookback. Older mail outside that bounded interval is not automatically reread. Receipt replacement occurs only after successful replay; retry and parser checkpoint state survive restart.
- Invalid preferences still fail explicit preference-dependent reads/model planning; expiry maintenance cannot turn an already-saved decision into a false failure or delete potentially excluded evidence.

## Verification

Each reported defect was reproduced before its production fix; additional edge cases were checked afterwards. Fixtures use temporary databases, fictional messages, fake model responses and local servers. Independent review found the stale-key issue above, then verified its repair; no further confirmed correctness defects were found in the reviewed changes.

- Final complete suite on the frozen source/test snapshot: **1,151 passed in 500.57 s (8 min 20 s)** using `.venv/bin/python -m pytest -q --tb=short` with local browser/HTTP facilities available. Exit status 0.
- Source/test snapshot hashes remained unchanged during that run; `git diff --check` passed.

- TTL/archive/query targeted suite: 71 passed; decision/MCP follow-up: 13 passed.
- Query service with retry counters: 33 passed.
- Selection and service scheduling suite: 121 passed.
- Sources, collection, processing and purge suite: 238 passed.
- Ingestion suite including parser replay: 357 passed; the final mailbox/performance run including the extra bounded-backoff test: 38 passed.
- Reparsed projection, linking, ingestion performance/service and purge regressions: 61 passed. The independent reviewer reran the new stale-key test successfully.
- Web decision/undo endpoint with malformed preferences: 1 passed.
- Ingest systemd and query jobs CLI help were checked.
- Sandbox-only HTTP/browser failures were traced to local port/browser restrictions; tests requiring those facilities are rerun with the permitted local test environment.

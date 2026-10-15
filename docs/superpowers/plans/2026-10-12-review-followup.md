# Review follow-up repair plan

User-authorized continuation of the October 12 review fixes, starting from the new repository's initial commit `695e245`. Preserve private configuration, saved evidence, and manual-only applications.

**Completed:** all work items below were implemented and reviewed. Independent review also identified and verified the repair of stale job projection keys after reparsing. Final full suite: 1,151 passed; source/test snapshot unchanged throughout the run; diff check passed. Details and existing-data limits are in the dated update record.

## Work and acceptance criteria

1. **TTL and query (root):** reproduce malformed preferences after a real decision, ensure saved decisions remain undoable, and keep potentially excluded expired jobs when preferences cannot be read. Determine expiration candidates before loading preferences; restrict location JSON checks to otherwise-deletable candidates. `state=archived` must return archived jobs with adapter defaults and stable pagination.
2. **Parsing and collection:** reproduce prose requisition IDs, company/title confusion, punctuation boundaries, US/country-code confusion, and salary suffix capture. Preserve real requisition IDs, US state matches and salary suffixes; invalidate affected parser caches.
3. **Selection:** use the newest historical verdict when the current version is absent, scope filter versions by name, and compute quota recovery against actual failure time. Verify implicit labels, separate copied-filter judgments, zero repeated model calls, and reset crossings.
4. **Ingestion:** isolate malformed repository URLs; resolve a bare executable through PATH; retain valid Google/Tesla career URLs. Reproduce poison-message mailbox starvation and preserve durable retries while allowing pagination/window progress.
5. **Integration:** review all changes, update step documentation and dated repair notes, run affected tests followed by the complete suite. Record real results and any remaining limits; no production data rewrite or deployment is part of this repair.

## Verification method

Each owner adds behavior regressions and observes the reported failure before changing production code. Fixtures use disposable databases, fictional mail and local test servers. Root runs `.venv/bin/python -m pytest -q` after all implementations settle, checks the diff, and records verification in `docs/updates/2026-10-12-review-followup.md`.

## Review focus

- Invalid preference configuration must never fall back to deleting excluded evidence.
- Excluded locations on secondary postings must still protect an expired job.
- Current-filter verdict preference must survive historical insertion order and errors.
- Structured non-US countries must outrank ambiguous state abbreviations.
- Deferred mail failures must remain retryable across restart without replaying successful items.

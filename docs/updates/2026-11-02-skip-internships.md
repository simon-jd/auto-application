# 2026-11-02 — `skip_internships`: leave internships out at import

## Why

The owner looks for new-grad roles only. On 2026-11-02 the four internship feeds were removed and the 11,391
jobs screened as internships were deleted ([2026-11-01 record](2026-11-01-grok-feeds-dedup.md)). Within one
round 152 internship jobs were back. Other feeds list internships too: Grok's Jobright and Handshake files,
and whole employer boards. A deleted posting's import entry has no posting, so the next read imports the row as
new. Screening excluded those jobs, but the library kept filling with them.

## Change

- Feed config: an optional top-level `skip_internships` (boolean, default `false`). Each source may set its own
  value, which overrides the top-level one. A value that is not a boolean is a config error.
- `IngestService.ingest(..., skip_internships=True)` leaves out every record whose own title or employment type
  says internship. The test is `screen.rules.stated_internship`, which the screening rule now also uses:
  - an intern, co-op or apprentice title;
  - an `internship` employment type;
  - a seasonal title without a new-grad word.

  Screening's clause for jobs seen only in internship feeds stays in screening.
- What happens to a skipped row:
  - It creates no posting or job and doesn't renew an existing posting, so an earlier import ages out by the
    TTL.
  - It still counts as listed, so a complete snapshot doesn't mark it missing. Once the source drops it, the
    entry is marked missing as usual.
- `run_repository`, `run_email` and mailbox imports pass the setting through. Reports and each source's
  `ingest_run` health gain a `skipped_internships` counter.
- `ingest repository` and `ingest email` on the command line keep importing every row.

## Tests

- `tests/ingest/test_runner.py`:
  - the top-level setting skips the intern, seasonal and `INTERNSHIP` rows of a repository and an email file;
  - a source with `skip_internships: false` keeps its intern row;
  - a value that is not a boolean is rejected;
  - a skipped row is neither renewed nor marked missing, and is marked missing once it leaves the feed.
- `tests/ingest/test_mailbox.py`: the setting survives the mailbox config's second validation and skips an
  intern link in a mail.
- Each test fails without the change.

## Deployment (2026-11-02)

Deployed with the [enrichment sites](2026-11-02-enrichment-sites.md) change (2,151 tests pass; pre-deploy backup
`auto_app.20261103-064809-423363.pre-deploy.db`), with ingest and the worker stopped:

| Step | Result |
|---|---|
| `skip_internships: true` at the top of the remote `feeds.yaml` (backup `feeds.yaml.bak-before-skip-internships-20261102`) | All 74 sources skip internships |
| Delete the internship jobs imported again since the morning | 250 jobs; active jobs 28,003 → 27,753 |
| Ingest round | All sources ok; 95 rows skipped (Grok's Jobright file 84, Handshake 11); no new jobs |
| Services started; first ingest and worker rounds | Ingest skipped 180 rows (`simplify-newgrad` 74, Grok's Jobright 84, Handshake 11, two boards 10, ApplyGuy 1). No job is screened as an internship. |

## Status

Deployed 2026-11-02 and on in the remote config.

# 2026-11-01 — Grok site feeds: deduplicate before importing

## Why

Grok Bot now scrapes LinkedIn, Handshake and Jobright for the owner and is to refresh them regularly. Before
adding them as feeds, its results were imported into copies of the Linux database to see what deduplication
left behind. Two problems came out of that, and one change of plan.

## Findings

1. **`FULL_TIME` vetoed every LinkedIn merge.** The `simplify` repository parser passed `employment_type`
   through unchanged. LinkedIn's export says `FULL_TIME`; every other source says `full_time` or nothing.
   Dedup's veto treats different employment types as different jobs, so no LinkedIn job could merge with a
   full-time job from another source (36 Handshake look-alikes on the copy were vetoed for exactly this).
2. **Grok wrote Jobright's employer links straight into the database.** On 2026-11-01 it replaced
   `postings.apply_url` on 1,887 `jobright-swe-newgrad` rows (backup:
   `runtime/data/auto_app.db.bak-before-jobright-applyurl-20261101`). Parsed fields, identity keys and job
   links were untouched, so dedup never saw the links. The enrichment worker did read the column and fetched
   566 employer JDs into those rows, but any re-parse of the rows (a content change, or the normalizer
   version bump below) would restore the Jobright link from the feed and drop the JD.
3. **Jobright's links are accurate.** The owner's test job (Rocket Lab, Flight Software Engineer I) resolved
   to `job-boards.greenhouse.io/embed/job_app?for=rocketlab&token=8038139003`, the employer posting already
   known from Greenhouse. Jobright appends `jr_id=…` and sometimes `source=jobright`.

## Change

- `ingest/repository.py`: `employment_type` goes through `employment_of` with `_` read as `-`, the idiom
  already used for JSON-LD in `sources/parsing.py`. Unrecognized values become `unknown`.
- `simplify` rows accept an optional `listing_url` (`IngestRecord.listing_url`). The posting's `apply_url` is
  the employer link and its `url` (and `source_url`) the listing page. `_payload` omits the field when empty,
  so existing records keep their content hash.
- `source_job_key` recognizes `jobright.ai/jobs/info/<id>` as `jobright:jobs:<id>`, as it does LinkedIn job
  pages; a resolved Jobright row therefore matches the existing Jobright listing exactly.
- `canonical_url` drops `jr_id`, and `source` only when its value is `jobright`.
- `NORMALIZER_VERSION` 5 → 6, so stored postings are parsed again at their next import (§14.2). A one-off
  source such as `linkedin-20261031` is only repaired when its file is imported again.
- Linking is unchanged: a new posting that matches two existing jobs joins one and records the other as a
  near miss. Merging two existing jobs (their decisions, judgments and history) is not part of this change.

Tests: parser vocabulary and `listing_url` (`tests/ingest/test_repository.py`), Jobright source keys
(`tests/process/test_normalize.py`), Jobright tracking markers (`tests/ingest/test_email.py`), and end to end
(`tests/ingest/test_multisource_repository.py`): a LinkedIn and a Handshake copy of one job merge; a resolved
Jobright row joins the Jobright listing's job or the employer job, and never merges the two. Each fails
without the change.

## Grok's files

Grok delivers files, never database edits. Each is a complete snapshot of the listings currently open (rows
missing from the next file are marked missing from that source and age out), in the `simplify` JSON format,
written to a temporary name and renamed into place:

| Source | File |
|---|---|
| `grok-linkedin` | `/home/grokbot/feeds/linkedin-feed.json` |
| `grok-handshake` | `/home/grokbot/feeds/handshake-feed.json` |
| `grok-jobright` | `/home/grokbot/feeds/jobright-feed.json`: `url` the employer link, `listing_url` the Jobright page |

LinkedIn and Handshake rows can use `listing_url` the same way when Grok captures the employer link. The files
leave out internships: screening excludes them, and since 2026-11-02 the library keeps none. Until
Grok writes these paths, the first batches are `runtime/feeds/grok-linkedin-20261031.json` (Grok's LinkedIn
file, copied out of `/tmp`) and `runtime/feeds/grok-jobright-20261101.json` (exported from the 1,887 rows Grok
edited). A feed whose file is never refreshed keeps its jobs "seen" forever; remove the source when Grok
stops updating it.

On 2026-11-11 Grok had refreshed only `handshake-feed.json` (2,069 rows). For Jobright it wrote a batch of
13 newly resolved rows, `jobright-employer-ready-2026-11-06.json`, instead of a complete `jobright-feed.json`.
The batch has employer links and descriptions. It was merged by id with the 2026-11-01 export into
`runtime/feeds/grok-jobright.json` (1,899 rows), and `grok-jobright` now reads that file. The import created
13 postings, of which 8 became new jobs, and skipped 84 internship rows. The dated export was removed. No
LinkedIn file exists yet, so `grok-linkedin` still reads the 2026-10-31 batch.

## Rehearsal on a copy of the Linux database (2026-11-02)

The version bump alone does not re-normalize the library: feeds that answer HTTP 304 (both Simplify feeds and
most others) are not read again, and Jobright's rolling table only re-reads the rows still in it (929 of
3,450 new-grad postings). The other postings stayed on normalizer versions 4 and 5, without the Jobright
identity, and 887 resolved Jobright rows became new jobs. `auto-app process normalize`, which recomputes
stale stored postings from their parsed fields, fixes that. The rehearsed order, with the patched code:

| Step | Result |
|---|---|
| First ingest round after the deploy | 18 s; no new jobs |
| Restore `apply_url` from each edited row's parsed link | 1,887 rows |
| `process normalize` (stale postings) | 273 s |
| Re-import `linkedin-20261031` | 4,614 updated, 0 new jobs; no `FULL_TIME` left |
| `grok-linkedin` | 4,614 records, 0 new jobs |
| `grok-handshake` | 2,061 records, 1,801 new jobs |
| `grok-jobright` (1,886 rows; one Jobright link was `about:blank`) | 0 new jobs: 1,675 joined their Jobright listing's job, the others an employer-link job from another feed |
| Worker round, screen and enrich only | 200 requests, 237 jobs enriched; the backlog continues at 200 per round |

Active jobs: 36,704 → 38,505. 319 resolved Jobright rows also match a second, existing job by employer link:
those pairs stay two jobs until existing jobs can be merged.

## Internship feeds removed (2026-11-02)

The owner looks for new-grad roles only, and asked for the library's internships to go. Two parts:

- `simplify-internships`, `speedyapply-swe-internships`, `speedyapply-ai-internships` and
  `jobright-swe-internships` left the remote `feeds.yaml` (backup `feeds.yaml.bak-before-intern-removal-20261102`).
- A one-off script deleted every active job the screening rules exclude as `internship` (docs/linux-worker.md,
  screening reasons). It used `job_ttl._delete_jobs`, the TTL's own path: it removes the job, its
  postings, keys and locations, and `ingest_entries` rows are released by `ON DELETE SET NULL`. It skipped any job
  that was judged, decided, applied to, claimed by the agent or used for a résumé; there were none. The removed
  feeds' import entries and change rows that no longer pointed at a posting went too. 21 postings of
  `simplify-internships` stay, on non-internship jobs that other feeds also list.

Two things were learned on the database copy:

- **Bulk deletion needs indexes the schema lacks.** `ingest_changes.posting_id`, `jobs.primary_posting_id`,
  `postings.duplicate_of` and `job_locations.posting_id` reference postings without an index, so each deleted
  posting scans those tables. 11,000 jobs did not finish in ten minutes. With temporary indexes, created and
  dropped in the same transaction, it took 32 s. The TTL normally deletes a few jobs at a time and does not
  notice this.
- **A deleted posting comes back when its feed lists it again.** An import entry without a posting is imported
  as new, so internships listed by other feeds (Grok's files, employer boards, new-grad lists) return as new jobs
  that screening excludes. Local files are read every round; URL feeds when they change.

## Deployment (2026-11-02)

The ingest and worker services were stopped first, so the deploy left them stopped (pre-deploy backup
`auto_app.20261103-030037-536203.pre-deploy.db`). Then, in order:

| Step | Result |
|---|---|
| First ingest round, intern feeds already removed | 252 s; 15 new jobs |
| Restore `apply_url` from each edited row's parsed link | 1,305 rows (the round had re-parsed the rest) |
| `process normalize` | 522 s |
| Re-import `linkedin-20261031` | 4,614 updated, 0 new jobs; no `FULL_TIME` left |
| `grok-linkedin`, `grok-handshake`, `grok-jobright` added to `feeds.yaml`; every feed run | 0, 1,814 and 0 new jobs; 230 s |
| Screen | 11,391 jobs excluded as `internship` |
| Delete them | 32 s; active jobs 39,112 → 27,721 |
| Services started; first ingest and worker rounds | No failures. 152 internship jobs back (Grok's Jobright file 77, Anduril and Coinbase boards 66, Handshake 9), all excluded by screening. The worker enriched 697 jobs. |

Active jobs: 37,279 before, 27,874 after; 13,039 pass screening. Every edited Jobright row is back on its
Jobright link; 1,606 resolved Jobright rows joined their Jobright listing's job and 196 an employer-link job, and
310 also match a second existing job.

## Status

2,088 tests pass. Deployed to the Linux host on 2026-11-02.

# 2026-11-02 — JD enrichment for six more sites; Greenhouse EU and Ashby board fixes

## Why

Enrichment read only Greenhouse, Lever, Ashby and Workday. Every other imported link was cached `unsupported`
and never read again. On 2026-11-03 06:22 UTC the server had 10,472 pending imported postings; Jobright,
TikTok/ByteDance, SmartRecruiters, Workable, Amazon and Microsoft links made up 4,485 of them. Two parsing bugs left
supported links unread too: Greenhouse's EU hosts were not recognized, and neither were Ashby board names with a
dot or a space (`jerry.ai`, `Spectral%20Labs`).

## Change

- **New adapters** (`src/auto_app/enrich/sites.py`, link parsing in `enrich/targets.py`). Each endpoint was checked
  live with read-only GETs on 2026-11-02:

  | Site | Links | Read from |
  |---|---|---|
  | Microsoft | `apply.careers.microsoft.com/careers/job/<id>` | `api/pcsx/position_details?position_id=<id>&domain=microsoft.com&hl=en` |
  | TikTok / ByteDance | `lifeattiktok.com/search/<id>`, `joinbytedance.com/search/<id>`, `jobs.bytedance.com/[en/]position/<id>/detail` | the server-rendered job page |
  | Jobright | `jobright.ai/jobs/info/<id>` | the logged-out page's `__NEXT_DATA__` |
  | SmartRecruiters | `jobs.smartrecruiters.com/<company>/<id>[-<title>]`, `/oneclick-ui/company/<company>/publication/<UUID>` | `api.smartrecruiters.com/v1/companies/<company>/postings/<id>` |
  | Workable | `apply.workable.com/<account>/j/<shortcode>[/apply]` | `apply.workable.com/api/v2/accounts/<account>/jobs/<shortcode>` |
  | Amazon | `(www.)amazon.jobs/[<locale>/]jobs/<id>[/…]` | `www.amazon.jobs/en/search.json?base_query=<id>`, keeping the hit whose `id_icims` matches |

  These sites have no collector, so the readers live in the enrichment package rather than `platforms/`.
  `valid_target` rebuilds a public link from the board and id and requires it to give the same Target, so only
  request URLs that `target_for` builds can reach the network.
- **Greenhouse EU**: `job-boards.eu.greenhouse.io` and `boards.eu.greenhouse.io` are accepted.
  `boards-api.eu.greenhouse.io` does not resolve (checked from the Mac and from home-ubuntu), and
  `api.eu.greenhouse.io` redirects to a sign-in page. The global `boards-api.greenhouse.io` serves EU boards: its
  `absolute_url` points back at the EU host.
- **Ashby boards**: names may contain dots and spaces (not at the start or end). The API URL percent-encodes the
  space (`job-board/Spectral%20Labs`), which the live API accepts.
- **Retrying unsupported links**: a cache row with status `unsupported` and platform `NULL` was written when no
  adapter knew the link. Such a link is now read in the first round where `target_for` resolves it. Rows an adapter
  wrote (empty description, Jobright hand-off) keep their verdict.
- **`unsupported` fetch result**: about 1 Jobright listing in 15 answers logged-out visitors with a 307 to
  ZipRecruiter. The fetcher returns `unsupported`. The service caches it with platform `jobright` and doesn't retry
  it, so these listings don't come back as failures every day.

What was not possible:

- TikTok's job API (`api.lifeattiktok.com/api/v1/public/supplier/job/posts/<id>`) answers `400 EOF` to a GET, with or
  without the site's headers. The adapter reads the server-rendered page instead.
- Amazon's `/jobs/<id>.json` redirects to a slugged URL that answers 406, so there is no per-job JSON.
- Microsoft rate-limits quickly (429 after a few requests from the Mac); home-ubuntu got 200. A 429 is a normal
  failure with backoff.
- Jobright's logged-out page has its summary of the JD, not the employer's text.

## Server coverage (read-only, 2026-11-03 06:22 UTC)

Pending imported postings on active jobs whose link each adapter now resolves. "To fetch" excludes rule-excluded
jobs, which make no request; all were either uncached or cached `unsupported` with platform `NULL`.

| Adapter | Pending postings | Distinct links | To fetch (postings / links) | Excluded |
|---|---:|---:|---:|---:|
| Jobright | 3,112 | 3,112 | 2,676 / 2,676 | 436 |
| TikTok / ByteDance | 553 | 362 | 537 / 346 | 16 |
| SmartRecruiters | 332 | 329 | 256 / 253 | 76 |
| Workable | 262 | 259 | 186 / 183 | 76 |
| Amazon | 147 | 140 | 126 / 119 | 21 |
| Microsoft | 79 | 49 | 72 / 43 | 7 |
| Greenhouse EU | 70 | 62 | 37 / 29 | 33 |
| Ashby, dotted or spaced board | 33 | 33 | 26 / 26 | 7 |
| **Total** | 4,588 | 4,346 | **3,916 / 3,675** | 672 |

Every pending link on these hosts parsed. At 200 requests per 15-minute round, the backlog takes about 19 rounds.

## Verification

- New tests, all watched failing before the code existed (42 failures): `tests/enrich/test_targets.py` (EU hosts,
  Ashby boards, 14 new-site links), `tests/enrich/test_sites.py` (one reader per site, with closed, other-job and
  malformed responses), `tests/enrich/test_fetch.py` (EU and Ashby request URLs) and `tests/enrich/test_service.py`
  (unsupported retry, Jobright hand-off). The new unsafe-link cases in `test_targets.py` are guards that passed
  before and still pass.
- The readers were run on saved live responses: two TikTok pages and a not-found page, two ByteDance pages, two
  Jobright pages, Microsoft, two SmartRecruiters, two Workable and one Amazon record.

## Status

Deployed to the Linux host on 2026-11-02 together with `skip_internships` (2,151 tests pass; pre-deploy backup
`auto_app.20261103-064809-423363.pre-deploy.db`). The first worker round fetched 200 links: 149 ok, 51 gone,
3 failed; the new adapters returned Workable 64, TikTok 23, SmartRecruiters 20, Amazon 18 and Microsoft 10
descriptions.

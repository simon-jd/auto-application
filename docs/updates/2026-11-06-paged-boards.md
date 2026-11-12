# 2026-11-06 — Eightfold, iCIMS Jibe, Oracle and SmartRecruiters boards as feeds

## Why

The same coverage audit that led to `workday-board` found four more systems behind large employers the
pipeline could not read as boards:
- **Eightfold** (Qualcomm), through its new PCSX search;
- **iCIMS career sites on the Jibe front end** (Garmin, AMD, Lutron, Rivian);
- **Oracle Recruiting Cloud** (Ford, Texas Instruments, Honeywell);
- **SmartRecruiters** (Bosch, Intuitive Surgical, Western Digital).

Their postings reached the library only when a repository or mail linked one. All four have public JSON
searches, verified from home-ubuntu with the pipeline's User-Agent (the workspace's
`reports/2026-11-06-embedded/unreadable-employers-plan.md`).

## Change

- Four repository formats, registered with the others and validated by `load_feed_config`:
  `eightfold-board`, `icims-jibe-board`, `oracle-board` and `smartrecruiters-board`.
  - `src/auto_app/ingest/repository_paged.py` holds the registry of paged boards (Workday too: its module
    gained a registry entry, nothing else) and the shared reading and parsing.
  - One module per format: `repository_eightfold.py`, `repository_jibe.py`, `repository_oracle.py`,
    `repository_smartrecruiters.py`.
  - `run_repository(..., options=)` and the loaded feed's `options` replace the Workday-only `workday=` /
    `"workday"`.
- Settings, each validated at load. A setting of another paged format is a config error naming the formats
  it belongs to.

| Format | `location` (normalized) | `company` | Options |
|---|---|---|---|
| `eightfold-board` | `https://<tenant>.eightfold.ai/careers?domain=<domain>` (a job path is dropped; the domain is required) | required | `country` (Eightfold's location text), `search`, `filters` (filter name → values, sent as `filter_<name>`), `max_pages` (default 30, at most 200) |
| `icims-jibe-board` | `https://<host>` of the careers site (any path is dropped) | optional (the list's hiring organization) | `country` (the site's country name), `search`, `max_pages` (default 30, at most 100) |
| `oracle-board` | `https://<pod>.fa.<region>.oraclecloud.com/hcmUI/CandidateExperience/en/sites/<site>` | required | `country` (a location facet's name), `search` (no `,` `;` `"`), `max_pages` (default 20, at most 100) |
| `smartrecruiters-board` | `https://careers.smartrecruiters.com/<CompanyIdentifier>` (also from `jobs.` or API URLs) | optional (the list's company name) | `country` (ISO code, sent lower-case), `search`, `max_pages` (default 20, at most 100) |

- Reading, shared: GET requests with the enrichment fetcher's User-Agent
  (`auto-app/1 (personal job search; one request per job)`), no redirects, one second between pages (two for
  Eightfold), one search per `search` text merged by job id. Each run writes one JSON snapshot (platform,
  board, UTC read date, each search's total, rows read and pages, the rows sorted by id) and parses it
  without network access.
  - Eightfold: `GET /api/pcsx/search?domain=&query=&location=&start=&page_size=10&sort_by=timestamp`.
    Eightfold refuses a larger `page_size` ("Too many rows requested"). The old `/api/apply/v2/jobs`
    answers 403 "Not authorized for PCSX".
  - Jibe: `GET /api/jobs?page=&limit=100&sortBy=posted_date&descending=true&keywords=&country=`. The
    `country` filter keeps a job with any location in that country. Each job's `meta_data` (search-engine
    and ATS bookkeeping) is dropped from the snapshot.
  - Oracle: the `recruitingCEJobRequisitions` finder
    `findReqs;siteNumber=<site>,facetsList=LOCATIONS,limit=100,offset=,sortBy=POSTING_DATES_DESC[,keyword="…"][,selectedLocationsFacet=<id>]`
    with `expand=requisitionList.secondaryLocations`. The site itself asks 25 a page; the finder answers 100
    and 200. With `country`, one request without requisitions lists the location facets first, and a name
    that is not among them fails the read.
  - SmartRecruiters: `GET api.smartrecruiters.com/v1/companies/<id>/postings?limit=100&offset=&country=&q=`.
- Records:

| Format | Identity and URL | Locations, country | Other fields | Description |
|---|---|---|---|---|
| Eightfold | position id; `https://<tenant>.eightfold.ai/careers/job/<id>?domain=<domain>` | the list's locations; the country from `standardizedLocations` | `displayJobId` as requisition; posted time; the department as `metadata.upstream.category` | none: pending |
| Jibe | the slug (= requisition at all four sites); `https://<host>/jobs/<slug>` | "City, State, Country" for the primary and each additional location; `country_code` | posted time; `employment_type` (`FULL_TIME` …) | the list's description (HTML or text), with the responsibilities or qualifications appended when it lacks them |
| Oracle | requisition `Id` (digits, or an imported one such as Honeywell's `req480976`); `…/sites/<site>/job/<Id>` | primary and secondary locations and country codes | `PostedDate`, a date: `posted_at_precision: day` | none: pending |
| SmartRecruiters | posting id; `https://jobs.smartrecruiters.com/<CompanyIdentifier>/<id>` | `fullLocation`; `country` | `refNumber` as requisition; released time; `typeOfEmployment`; `experienceLevel` kept as metadata | none: pending |

  `metadata.upstream.country` is set only when every location is in one country, as structured evidence for
  screening's US rule. The department as Eightfold's category matters at Qualcomm, where titles are often
  just "Engineer": "Software Engineering" makes it a CS role and "Hardware Engineering" not, by screening's
  existing category vocabulary.
- Descriptions:
  - Jibe jobs import complete.
  - The other three import pending. The worker screens their titles and locations, and its enrich step reads
    each kept posting's detail once (cached, so a re-import costs nothing).
  - SmartRecruiters links already resolved to the Posting API.
  - New enrichment targets and readers (`enrich/targets.py`, `enrich/sites.py`):
    - **Eightfold tenants**: `<tenant>.eightfold.ai/careers/job/<id>?domain=<domain>` reads
      `/api/pcsx/position_details`, through the Microsoft reader, now shared, with no company of its own.
    - **Oracle**: `…/hcmUI/CandidateExperience/<language>/sites/<site>/job/<id>` reads
      `recruitingCEJobRequisitionDetails` (finder `ById`). Its text is the external description,
      responsibilities and qualifications. An empty `items` list means the requisition is closed (`gone`).
  - The enrichment fetcher waits two seconds between requests to an Eightfold tenant (one second elsewhere):
    a first import of Qualcomm reads about 120 details. A closed Eightfold position answers 404 (`gone`), as
    Microsoft's does.
- Snapshot safety: every page of these searches reports the total. A read of a search is complete only when it
  ends (a short page, or as many pages as its total needs) having read as many distinct postings as the
  largest total its pages reported.
  - A posting added or closed while the pages are read shifts the newest-first order: a row repeats on the
    next page and another is never served. A read that ends short is therefore repeated once. The search is
    complete when the second read is, and it keeps the rows of both reads.
  - A read stopped by the page cap is not repeated.
  - Otherwise the run imports what it read as a partial snapshot and closes nothing. The feed scheduler
    counts a round with a partial source as failed, so a board that keeps ending partial raises the "feed
    worker failed" alert after three rounds.
  - HTTP errors (a 429 too), redirects, and responses without the expected list and total fail the read; the
    next round tries again.
- `ingest repository --format` help lists the new formats.
- Docs: [Step 1 §14.1, §14.6, I4](../step1-collect.md#146-employer-board-feeds),
  [Linux worker §1, §6](../linux-worker.md#6-employer-boards), the README's Employer boards section, and four
  disabled examples in `config/feeds.example.yaml`.

## Found on the way

- Eightfold answered HTTP 429 "Please try again later" to every request, even a single one, for about
  15 minutes on the evening of 2026-11-06, then recovered without any change. The reader pauses two seconds
  between pages, and a 429 fails that round's read.
- Oracle detail lookup values follow the requisition's own locale. A Ford requisition answered
  "Dearborn, MI, Estados Unidos" and "Período integral" while its text was English. The import's English
  locations are kept, and the job schedule then reads as an unknown employment type.

## Live check (read-only, 2026-11-06)

Method:
- Each employer's US board was read through the new readers: `country` United States (`us` for
  SmartRecruiters), no search, a page cap high enough for the whole board.
- The live `runtime/config/screening.json` from home-ubuntu judged each posting with the current rules code
  (v8), as the worker would.
- Jibe postings were judged with their list JD. For the other three, the enrichment `Fetcher` read the detail
  of every posting kept with a CS-role title (2.5 seconds apart for Eightfold, 1 second elsewhere), and the
  posting was judged again with its JD.
- "US entry-level SW" counts the postings still kept with a CS-role title and a US location.
- The reads ran on the Mac, which shares home-ubuntu's public IPv4 address, though not its IPv6 address.

| Employer (format) | Postings read | List requests, bytes | Kept by title (CS / other) | JDs read | US entry-level SW |
|---|---|---|---|---|---|
| Qualcomm (Eightfold, US) | 795 of 797 | 80, 2.3 MB, 177 s | 210 (116 / 94) | 116 | 100 |
| Garmin (Jibe, US) | 439 | 5, 4.9 MB | 149 (73 / 76) | in list | 73 |
| AMD (Jibe, US) | 753 | 8, 22.9 MB | 204 (87 / 117) | in list | 87 |
| Lutron (Jibe, US) | 112 | 2, 3.2 MB | 8 (4 / 4) | in list | 4 |
| Rivian (Jibe, US) | 923 | 10, 17.5 MB | 95 (13 / 82) | in list | 13 |
| Ford (Oracle, US facet) | 734 | 9, 1.4 MB | 311 (91 / 220) | 91 | 38 |
| Texas Instruments (Oracle, US facet) | 500 | 6, 0.8 MB | 223 (56 / 167) | 56 | 26 |
| Honeywell (Oracle, US facet) | 450 of 451 | 6, 0.9 MB | 94 (24 / 70) | 24 | 7 |
| Bosch (SmartRecruiters, us) | 361 | 4, 1.1 MB | 93 (21 / 72) | 21 | 9 |
| Intuitive Surgical (SmartRecruiters, us) | 491 | 5, 1.7 MB | 87 (14 / 73) | 14 | 4 |
| Western Digital (SmartRecruiters, us) | 83 | 1, 0.3 MB | 13 (1 / 12) | 1 | 1 |

For Jibe, "kept by title" is the final verdict, because the list carries the JD.

Notes on the reads:
- Qualcomm's 80-page read (about three minutes) served 2 postings twice and 2 never: the board changed
  during the read. That made the snapshot partial, which led to the single re-read above. Two reads of a
  105-posting department search, with either sort, matched exactly.
- Honeywell lists an imported requisition `req480976`. The first reader accepted digits only; the Id rule now
  accepts it. The saved snapshot parses 451 of 451.

After the JD, the CS-role titles excluded were mostly `experience_required`: Ford 52, TI 30, Qualcomm 15,
Honeywell 14, Bosch 12 and Intuitive 10. Honeywell had 7 `citizenship_required`, Ford and Qualcomm 1 each.
On the Jibe boards, `senior_title` excluded most CS-role titles (AMD 153, Garmin 78, Rivian 76).

The counts include title-rule noise:
- Ford's "Buffalo Stamping Plant (BSP)" trades match the live extra CS term `bsp`.
- Other broad title words match too:
  - "mobile": Rivian's mobile field-service technicians, a Ford mobile product designer;
  - "security": a Ford security officer;
  - "front end": a Ford front-end maintenance supervisor;
  - "web": TI's web-marketing program;
  - "ai": Intuitive's finance AI analysts;
  - "automation": Bosch and Honeywell technicians.
- Jobs whose title names no role stay kept after their JD unless the JD excludes them, for example Garmin's
  product support specialists and Rivian's service advisors. On the Jibe boards that is 76 at Garmin, 117 at
  AMD and 82 at Rivian. The opt-in `exclude_unknown_without_software_evidence` would drop 52 of Garmin's 76,
  but at most 11 of AMD's and none of Rivian's: Rivian's JDs mention "cloud" (mostly in shared text), and
  AMD's mention embedded, firmware or C++.

Candidate feeds, each read live with exactly these settings:

| Feed | Settings | Postings | Requests a round | Bytes | Covers |
|---|---|---|---|---|---|
| `board-eightfold-qualcomm` | US, 24 software-leaning departments (`filters: job_family`) | 290 | 29 | 0.8 MB | 84 of 100 |
| `board-eightfold-qualcomm-asics` (optional) | US, ASICS and CPU Engineering | 184 | 19 | 0.5 MB | 11 more |
| `board-jibe-amd` | US, search "software engineer" | 379 | 4 | 6.7 MB | 77 of 87 |
| `board-jibe-rivian` (optional) | US, search "software" | 362 | 4 | 6.8 MB | 8 of 13 (all 8 real; the 5 missed are technicians) |

The other entries are the US boards in the table above. The ready-to-paste entries are in the workspace's
`reports/2026-11-06-embedded/feeds-additions-more-ats.yaml`, and `load_feed_config` accepts them.

Cost of the ten recommended feeds:
- 71 list requests and about 22 MB per ingest round, about 1 GB a day at the 30-minute cadence, about
  2.5 minutes of reading a round. AMD's whole US board instead of its search would add 16 MB a round.
- On the first import, screening keeps 940 of their pending postings by title, whatever their role.
  Enrichment reads one detail each, within its 200-request round budget, so this takes about five rounds
  if nothing else is pending.

None of this was added to the live config.

## Tests

Fixtures in `tests/fixtures/{eightfold,jibe,oracle,smartrecruiters}/`, recorded on 2026-11-06 and trimmed
(facet catalogs and Jibe `meta_data` cut). Each is a small search read in two pages, the page size patched
to the recording's:
- Qualcomm "GNSS firmware", plus one position detail;
- Garmin "GNSS" in the US, with jobs in two countries;
- Ford "infotainment" in the US facet, plus Ford's location facets and one requisition detail;
- Western Digital "embedded" in the US.

- `tests/ingest/test_repository_eightfold.py` (34), `test_repository_jibe.py` (41), `test_repository_oracle.py`
  (38), `test_repository_smartrecruiters.py` (31). For each format:
  - request parameters, the User-Agent and pauses;
  - record identity, URL, locations, country, dates and employment type;
  - the enrichment target of each URL;
  - partial snapshots at the page cap and when pages end early;
  - malformed rows isolated, never echoed;
  - bad responses, HTTP errors and redirects fail;
  - location normalization and rejection;
  - feed config validation, including another format's options;
  - `run_config` closing only after a complete read;
  - the command-line import.
  Format-specific checks: Jibe's sections appended to a description that lacks them; Oracle's facet
  resolution, an unknown country, secondary locations and an imported requisition Id; Eightfold's filters
  and its department reaching screening as a category.
- `tests/ingest/test_repository_paged.py` (5): the registry, options that belong to another format, the
  largest-total rule, the single re-read and rows without a key.
- `tests/enrich/test_sites.py` (+4): Eightfold tenant and Oracle detail readers, closed and mismatched records.
  `test_targets.py`: their links, unsupported links, and forged targets refused.
  `test_fetch.py` (+1): two seconds between Eightfold requests.
- `test_repository_workday.py`: the loaded feed's options key is now `options`.

Full suite: 2503 passed (`uv run pytest -q`, 2026-11-06, with other sessions' uncommitted work in the tree).

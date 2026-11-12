# 2026-11-06 — `workday-board`: Workday career sites as employer board feeds

## Why

The 2026-11-06 coverage audit of the owner's new-grad embedded / vehicle / Linux search found that the
largest employers the pipeline cannot read are on Workday (`*.myworkdayjobs.com`): GM, NVIDIA, Intel, Micron,
Analog Devices, NXP, KLA, Cisco, HPE, Aptiv, Toyota North America and others. Their postings reached the
library only when a repository or mail happened to link one. Greenhouse, Lever and Ashby boards already had
feed formats. Workday had only per-job enrichment.

## Change

- New repository format `workday-board` (`src/auto_app/ingest/repository_workday.py`), registered with the
  other formats and validated by `load_feed_config`:
  - `location`: the careers URL `https://<tenant>.wd<N>.myworkdayjobs.com/<site>`, normalized (a locale such
    as `/en-US/`, a job path, a query or a trailing slash after the site is dropped). Another host, a missing
    site, credentials or a port are config errors.
  - `company`: required. Workday's list never names the employer. `ingest repository --format workday-board`
    on the command line, which has no company option, uses the tenant name.
  - `search` (optional): a search text or a list; one Workday search each, merged by job path.
  - `facets` (optional): a facet's parameter or label → its values as the site shows them, applied to every
    search. One unfiltered request lists the site's facets first, and an unknown facet or value fails the read,
    naming the ones available. The resolution is the collector's own (`WorkdaySource.applied_facets`).
  - `max_pages` (optional): pages of 20 per search, default 25, at most 100 (Workday's 2000-result cap).
  - `search`, `facets` and `max_pages` on another format are config errors.
- Reading: POST `/wday/cxs/<tenant>/<site>/jobs` with `limit` 20 (Workday answers 50 with HTTP 400), one
  second between requests, no redirects. Workday reports `total` on the first page only, so the total is
  taken from there: later pages report 0. Each run writes one JSON snapshot (board, UTC read date, resolved
  facet ids, each search's total, rows read and pages, the rows sorted by job path) and parses it without
  network access.
- Records:
  - Identity is the Workday job id at the end of the path (`JR-202622086`, Cisco's `2021697-1`).
  - The URL is the public job page, which is an employer job key, so the record merges with repository and
    mail copies of the same opening.
  - The requisition is the bullet field the id starts with (`2021697` for `2021697-1`).
  - Locations are the list's location text. When Workday only counts them ("4 Locations"), they are left
    empty for enrichment to fill.
  - The employment type is the list's time type when it has one (Cisco).
  - The posted date is estimated from "Posted N Days Ago" relative to the read's UTC date
    (`posted_at_precision: approx`; "30+ Days Ago" counts as 31). The text and read date are a
    `source_observation`.
- Fingerprint: `IngestService` leaves an `approx` posted date out of the content hash. Otherwise the estimate
  would change whenever the read date and the site's time zone disagree about "today", and every Workday
  posting would count as updated twice a day. The posting keeps its first estimate until a real change. No
  other format sets `approx`, so no existing hash changes.
- Descriptions: the list has none, so records import as pending. The worker screens their titles and
  locations, and its enrich step reads the CXS detail endpoint (`/wday/cxs/<tenant>/<site>/job/…`) once for
  each posting screening keeps. That path already existed for Workday links. It fills the description,
  locations and employment type, and a re-import restores the cached JD without another request. A board
  round therefore costs about one request per page, not one per posting.
- Snapshot safety: a search is complete only when it reads every posting of its total and that total is
  below 2000. Otherwise (page cap, Workday's cap, pages that end early) the run imports what it read as a
  partial snapshot and closes nothing. The issue says how far the search fell short and how to narrow it.
  An HTTP error, or a response without a `jobPostings` array and `total`, fails the read and leaves the last
  snapshot as it was.
- `ingest repository --format` help lists the board formats.
- Docs: [Step 1 §14.1, §14.6, I3](../step1-collect.md#146-employer-board-feeds),
  [Linux worker §6](../linux-worker.md#6-employer-boards), the README's Employer boards section and a
  disabled `board-workday-example` in `config/feeds.example.yaml`.

## Found on the way

The Workday collector used by `serve` (`src/auto_app/platforms/workday.py`, e.g. the NVIDIA plugin) stops
after its second page. It treats the `total: 0` of later pages as the end, so it reads at most 40 postings per
keyword. This change does not touch it.

## Live check (read-only, 2026-11-06)

Method:
- Each employer's whole US board (Intel and Cisco have no country facet: their whole board) was read through
  `read_workday_board` and parsed. Every read was complete, with no partial snapshot.
- The default `ScreeningRules()` judged the titles and locations, as the worker does at import.
- For each posting kept with a CS-role title, the enrichment `Fetcher` read the CXS detail, one request a
  second, and the posting was judged again with its JD, locations and time type.
- "US entry-level SW" counts the postings still kept with a US location.
- The counts below use screening rules v8 (the same day's US-person/ITAR update), recounted from the saved
  reads. GM had 36 under the rules before it.

| Employer (board) | Postings read | Kept by title | CS titles, JD read | US entry-level SW |
|---|---|---|---|---|
| GM (US) | 625 | 147 | 53 | 34 |
| NVIDIA (US) | 1477 | 150 | 79 | 48 |
| Intel (all) | 870 | 241 | 64 | 20 |
| Aptiv (US) | 125 | 36 | 12 | 4 |
| Cisco (all) | 1474 | 280 | 165 | 41 |

The JD-stage drops were mostly `experience_required` (GM 15, NVIDIA 31, Intel 37, Aptiv 7, Cisco 90), then
`outside_us` (Cisco 50, Intel 4) and `citizenship_required` (GM 4, Intel 9, Cisco 4, Aptiv 1).

The default rules' CS-role test is broad. Among the counted postings are:
- NVIDIA legal counsel, social media, sales and marketing roles;
- two Intel manufacturing technician roles;
- about a dozen Cisco pre-sales "Solutions Engineer" roles;
- a Cisco role in San José, Costa Rica, read as US;
- a GM "Principle Systems Engineer", where the misspelling escapes the senior rule.

Candidate feeds, each read live with exactly these settings (requests per round = pages, plus one facet
listing):

| Feed | Settings | Postings | Pages | Covers |
|---|---|---|---|---|
| `board-workday-gm` | US, six engineering/software/IT/R&D/data/special-hiring families, Regular | 434 | 22 | 32 of 34 |
| `board-workday-nvidia-ncg` | US, job type New College Graduate | 42 | 3 | 14 of 48 |
| `board-workday-nvidia-engineering` (optional) | US, Engineering/Research/IT, Regular or New College Graduate | 911 | 46 | 40 of 48 |
| `board-workday-intel` | five engineering families | 388 | 20 | 18 of 20 |
| `board-workday-aptiv` | US | 125 | 7 | 4 of 4 |
| `board-workday-cisco-early-career` | search "Full Time United States" | 57 | 3 | 12 of 41 |
| `board-workday-cisco-engineering` (optional) | Engineering, Regular | 765 | 39 | 24 of 41 (29 with the one above) |

The ready-to-paste entries are in the workspace's
`reports/2026-11-06-embedded/feeds-additions-workday.yaml`, and `load_feed_config` accepts them.

Cost of the five recommended feeds:
- About 60 list requests per ingest round, roughly 2,800 a day at the 30-minute cadence. The two optional
  feeds add about 87 per round.
- On the first import, screening keeps 312 of their postings by title. Enrichment reads one detail each,
  within its 200-request round budget.

None of this was added to the live config.

## Tests

`tests/ingest/test_repository_workday.py` (46 tests). The fixtures in `tests/fixtures/workday/` were recorded
from GM's and Cisco's sites on 2026-11-06: GM's US "firmware" search is two pages, the second reporting
`total: 0`. The facet listing is trimmed to three facets, one of them nested.

- Every page is read although later pages report total 0. The facet probe, request bodies and the pause
  before each request after the first are checked.
- Record identity, URL, location text, "N Locations", the requisition from a location-copy id, the
  approximate dates for Today, Yesterday, N days and 30+ days, and Cisco's time type. Each record's URL is an
  enrichment target for the CXS detail endpoint.
- The page cap, pages that end early and Workday's 2000 cap each give a partial snapshot.
- A nested facet, several search texts merged and an unknown facet or value.
- Malformed rows (bad paths, a path escaping the site, a blank title, a duplicate id) are isolated. Bad
  responses, HTTP errors and bad snapshots fail instead of looking like an empty board.
- Careers URL normalization and rejection.
- Feed config validation, rejected settings, and settings on another format.
- `run_config`: a partial read closes nothing, a complete read marks the closed posting missing, and the
  company comes from the config. The command line import uses the defaults and the tenant name.
- An estimated date that moves with the clock is not a change; a title change is.

Full suite: 2304 passed (`uv run pytest -q`, 2026-11-06, with other sessions' uncommitted work in the tree).

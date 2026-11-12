# 2026-11-06 — Workday collector reads every page, up to Workday's 2000-result cap

## Why

While building the [`workday-board` feed format](2026-11-06-workday-board.md), live reads showed that Workday's
CXS search (`POST /wday/cxs/<tenant>/<site>/jobs`) reports `total` on the first page only. Every later page
says `"total": 0`. For example, GM's US facet gave a total of 625 on page 1 and 0 on page 2.

The collector that `serve` uses for Workday companies (`WorkdaySource.search` in
`src/auto_app/platforms/workday.py`, used by the NVIDIA and Adobe plugins) stopped when
`offset + len(postings) >= total`. Page 2 gives 20 + 20 ≥ 0, so every search ended after its second page: at
most 40 postings per keyword, and the run still looked complete (`caught_up`, no `max_pages` stop).

## Change

- The total is taken from the first page only. Paging continues while pages are full and the next offset is
  below `min(total, MAX_RESULTS, 2000)`.
- `RESULT_CAP = 2000`: Workday lists no further (offset 2000 returns junk), whatever its total says.
  `MAX_RESULTS` (1000 per keyword) is unchanged and is still the lower limit.
- `search_truncated` keeps its meaning. It is set when paging stops at a limit while the total, or full pages
  when there is no total, say more postings are listed. The run then ends as `max_pages` with
  `caught_up=False`. A short page still means Workday has no more and is not a truncation.
- `limit` stays 20. Workday answers a larger one with HTTP 400.

Effect: a Workday search now reads up to 50 pages per keyword instead of 2. That means more listing requests
per round and, on the first round, many postings not seen before. Each new posting's detail fetch counts
against the company's hourly and daily limits, as before.

## Tests

- `tests/sources/test_platforms.py`:
  - `test_workday_reads_past_later_pages_that_report_total_zero` uses the recorded GM firmware pages
    (`gm_firmware_us_p0.json`: 20 postings, total 21; `gm_firmware_us_p1.json`: 1 posting, total 0):
    - the two recorded pages give all 21 postings;
    - a 41-posting listing, with a full middle page reporting total 0, reads offsets 0, 20 and 40 and returns
      all 41.
  - `test_workday_stops_at_its_2000_result_cap`: with `MAX_RESULTS` raised to 5000 and a total of 3000, the
    collector reads offsets 0–1980, never 2000, and sets `search_truncated`.
- The 41-posting case and the cap test fail without the change: the collector stopped after offset 20.
- The existing truncation test (`tests/collect/test_api_collector.py::test_workday_facet_hits_survive_fallback_cap`)
  still passes.

## Status

Local, not deployed. The live NVIDIA and Adobe sources pick it up at the next deployment.

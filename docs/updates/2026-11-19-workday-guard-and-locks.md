# 2026-11-19 — One Workday cool-down, lock-tolerant feed rounds, vendor boards left out

Three problems seen on the server on 11-19, fixed together.

## 1. Workday: enrichment and the feed worker share one cool-down

**Why.** Workday's job lists carry no description. So every Workday job that screening keeps needs a JD request
of its own: 6,354 enrichment candidates were waiting. A round could spend all of its 200 requests on Workday, one
second apart per host, with no pause between tenants (each tenant is its own host). The enrichment cache shows:
- 117 Workday reads refused with HTTP 403 on 11-19 (to 18:35 UTC) and 34 on 11-18;
- 201 Workday jobs currently failed with 403, across 77 tenants (globalhr 29, ngc 29, boeing 15, …);
- 2,957 good Workday reads in all, the last at 16:56 UTC on 11-19.

The refusals come in bursts across tenants: Workday blocks the address, not one site. This is also the likely
cause of the feed worker's 429s on the Workday boards (2026-11-18-workday-pacing.md): those pacing fixes only slowed
the feed worker, while enrichment kept sending the burst.

**Change.**
- **The shared cool-down.** `platforms/workday_guard.py` keeps it in `data/.workday-guard.json`, next to the
  database. The file is written atomically, so it needs no database lock.
  - A 403 or 429 from any Workday host, on either side, starts it: 1, 2, 4, 8, then 12 hours, growing with each
    strike in a row, or a longer `Retry-After`.
  - A strike while it is already on only lengthens it to a longer `Retry-After`; it does not count again.
  - A good Workday read ends it, unless that read started before the strike.
  - A missing or unreadable file means no cool-down.
- **Enrichment** (`enrich/service.py`):
  - at most `WORKDAY_REQUESTS = 10` Workday jobs a round;
  - Workday requests three seconds apart across all tenants (`fetch.PLATFORM_GAP`);
  - no more Workday requests in the round after the first 403 or 429;
  - none at all while the cool-down lasts.
  - Workday jobs held back count as `workday_deferred` and stay pending; `workday_cooling_until` gives the end of the
    cool-down.
  - A refused job's own retry is set after the cool-down, so the jobs that were not refused go first, and one listing
    that is always refused cannot keep Workday shut.
  - `FetchResult` now carries `http_status` and `retry_after`.
- **Feed worker** (`ingest/runner.py`):
  - While the cool-down lasts, every Workday feed is `deferred`, with the reason ("Workday is cooling down until … after
    HTTP 403 (enrich)").
  - A 403 from a Workday feed is now `rate_limited`, like a 429, with the feed's own backoff, and it strikes the shared
    cool-down. A 403 from any other kind of feed is still an error.
  - A good Workday read clears the cool-down.
  - Because a 429 now holds every Workday site, not just the one that answered it, the 11-18 test of per-site turns
    after a 429 was rewritten.
- Tests first: `tests/enrich/test_workday_guard.py` (guard, enrichment, fetcher) and
  `tests/ingest/test_workday_guard.py`.

At 10 Workday jobs a round and four rounds an hour, enrichment reads at most 40 Workday JDs an hour. The 6,354
waiting include all platforms; Workday's share drains over days, not hours. That is the trade for not being blocked.

## 2. A locked database fails one feed, not the round

**Why.** Twice on 11-19 a "database is locked" from `update_health` ended a whole ingest round. The likely cause is
another writer, such as enrichment's projection write, holding the lock for longer than the 5-second `busy_timeout`.

**Change.**
- Every connection now waits up to 30 seconds for the lock (`core/db/engine.BUSY_TIMEOUT_MS`). The Discord bot runs
  its database calls off its event loop (`asyncio.to_thread`), so a wait does not freeze it.
- In `run_config`, a feed that still finds the database locked, while reading or while saving its health, is
  reported as an error ("database busy …; read again next round") or `health_saved: false`, and the round goes on.
- Other database errors, such as a missing table, still stop the round.
- Tests: `tests/ingest/test_database_busy.py`.

## 3. Weekly board proposals leave out the vendors already excluded

**Why.** The first weekly board proposals (11-19) were 9 boards. Six of them were the AI-training, data and language
vendors the owner had left out of the 478 long-tail boards on 11-18. Their names do not say "annotation" or
"staffing", so the name filter missed them: TSMG, Innodata, RWS, Welo Global, Invisible Technologies (board
`agency`) and Prolific.

**Change.** `worker/board_proposals.py` also leaves out the boards on `VENDOR_BOARDS` and the companies on
`VENDOR_COMPANIES`. Test: `test_known_vendors_are_left_out_by_board_or_company`. This only affects future weeks.
Proposals #1–#9 already posted stay open on Discord: the owner chose to deploy without skipping #1–#6.

## Status

Deployed 2026-11-19 11:08 PST (2,834 full-suite pass). The deployed sources match the tested ones, and the worker,
feed, bot, agent and preparation services are active.
- **Enrichment, first round after the deploy:** one Workday request, answered 403. The shared cool-down started
  (strike 1, until 20:08 UTC), and 16 Workday jobs were held back (`workday_deferred`). Workday was still refusing
  the server at that moment.
- **Feed worker:** its round started at 19:08:13 UTC, one second before that strike, so it did not see the
  cool-down. The next round, about 30 minutes later, is the first to hold the Workday feeds back. Not yet observed.

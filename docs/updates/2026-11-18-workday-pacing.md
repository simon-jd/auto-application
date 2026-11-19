# 2026-11-18 — Workday boards take turns and back off after 429

## Why

All five Workday feeds answered every round with HTTP 429 Too Many Requests: GM, NVIDIA new college grads, Intel,
Aptiv and Cisco early career. The feed worker reported 112 failed rounds in a row (about 56 hours). One request
from the server, sent alone, was answered 200, so the server's address was not blocked; the rounds were. Every 30
minutes each round read all five sites back to back, each from its first page, one second apart. The five sites
share Workday's service. Logging in plays no part: the CXS job search is public, and accounts are for applying.

## Change

- **One Workday site per round** (`runner.WORKDAY_PER_ROUND = 1`): the most overdue, never-read sites first.
- **Each site at most every two hours** (`WORKDAY_GAP`). With five sites and a 30-minute round, each is read about
  every 2.5 hours.
- **After a 429 a site cools down.** It waits for the longer of `Retry-After` and a growing backoff (1, 2, 4, 8,
  then 12 hours). The source's health keeps `retry_after` and `rate_limited_count`, and a good read clears both.
- **Statuses.** The report says `rate_limited`, which counts toward a partial round. A feed waiting its turn says
  `deferred`, which does not, and its health stays as it was.
- **Pauses.** Two requests to one Workday site are now 3 seconds apart (`repository_workday.PAUSE_SECONDS`, was 1).
- **`run_config(now=…)`** for tests. Other formats are read every round, as before.

## Status

Deployed 2026-11-18 21:4x PST (2,773 full-suite pass). The deployed sources match the tested ones, and the feed, screening
and bot services are active.

- Last good reads before the fix: GM, Intel and Aptiv on 11-16; Cisco early career on 11-18 03:23 UTC; NVIDIA
  new college grads on 11-18 11:27 UTC.
- The old code tried all five every round (last at 05:03 UTC on 11-19), so the first turn comes about two hours
  after that. Whether the 429s are gone shows in the following rounds' source health.

## Follow-up: long-tail employer boards (`every_hours`)

To add a few hundred small Greenhouse, Ashby and Lever boards without bursts, any feed may now set
`every_hours`: a number above 0 and at most 168. Such a feed is read at most once per that many hours. Feeds with
`every_hours` share a per-round budget (`runner.EVERY_PER_ROUND = 40`): never-read feeds come first, then the
longest unread. A feed that waits is reported `deferred` with the reason, and its health stays as it was. Feeds
without the key are read every round, as before. The tests were written first (`tests/ingest/test_every_hours.py`).

**Live 2026-11-18.** Deployed with 2,782 full-suite tests passing. On the owner's go, 477 long-tail boards were added
to the server's `feeds.yaml` with `every_hours: 6`: Greenhouse, Ashby and Lever boards that had kept jobs but were
not connected. The candidate list had 485:
- 7 were left out as likely vendors (annotation, translation, staffing) or companies the owner kept skipping;
- one (RoadRunner) was already configured under another letter case.

Sources went from 147 to 624. The backup is `feeds.yaml.bak-before-longtail-20261118`. New feeds' existing postings are
not pushed to Discord (`source_since`).

Fix found along the way: `worker boards` compared configured locations exactly. It now ignores letter case,
percent-encoding and a trailing slash (`tests/worker/test_boards.py`).

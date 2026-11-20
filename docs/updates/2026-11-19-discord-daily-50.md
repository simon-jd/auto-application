# 2026-11-19 — Discord: 50 jobs a day from 8:00 Pacific, oldest first, with more on each card

## Why

The owner couldn't judge the cards: the bot sent 10 an hour, around the clock (342 waiting on 11-19). The owner wants a
fixed daily batch to decide from, and more on each card: the record with the same company (how many applications,
which jobs) and when the job was collected.

The owner's choices: **all 50 at 8:00 Pacific**, **oldest collected first**.

## Change

- **Pace** (`bot/app.py`): `DIGEST_PER_DAY = 50`, `DIGEST_AT = 08:00`, `DIGEST_ZONE = America/Los_Angeles`. Nothing is
  sent before 8:00. The first check after it sends the day's 50 in one go, and later checks top up only if fewer
  were waiting. The per-check (5) and per-hour (10) limits are gone. `digest_sent` keeps 48 hours of stamps.
  `JobBot.digest_at = None` starts the day at midnight (tests).
- **Order** (`worker/digest.py`): `pending(..., oldest_first=True)` sorts by `first_seen_at`, then job ID. The bot uses
  it. The webhook digest keeps sponsors first, then newest.
- **Closed jobs left out** (`handoff/evidence.py`, new): `feed_states`, `closed` and `gone_links` moved out of the
  queue's `_candidates`, which now calls them. `pending` uses the same rule, so a job the queue would refuse as
  `source_inactive` or `link_gone` is not pushed. In a simulation on 11-19, 222 of the 929 pushed-but-undecided jobs
  were in this state.
- **Card** (`worker/digest.py`, `worker/history.py`):
  - A new **收录** field: the first-collected date, Pacific, with 今天 or N 天前.
  - **同公司记录** lists up to 5 applied jobs. Each shows its date and the latest reply from `application_events`
    (OA / 面试 / 已拒 / offer / 已撤回).
  - The jobs you want there are listed by title, not only counted.
- **Header:** 「今日职位 N 个（按收录时间从旧到新，另有 M 个在排队）」, or 今日补充 for a top-up.
- **Tests first:** `tests/bot/test_app.py`:
  - the 50 at 8:00 Pacific, oldest first, none at 07:59 or later that day, the rest the next morning;
  - the recheck tests set `digest_at = None`.
- **Tests first:** `tests/worker/test_digest.py`: oldest-first order, closed jobs skipped, the 收录 field.
- **Tests first:** `tests/worker/test_company_history.py`: dates, the latest reply and wanted titles.

## Also on 2026-11-19

Three spacer messages were posted to the job channel (the bot's own credentials, through the REST API) so the old
cards scroll out of view. The last one ends with 「以上是旧消息 · 2026-11-19 清屏」.

## Status

Deployed to home-ubuntu at 2026-11-19 20:53 PST, after a full-suite pass (2,839 tests). The bot, worker and ingest
services are active, and the bot reconnected to the gateway with no errors. The old pace had already sent 50 cards
since 08:00 Pacific that day, so the new code sends nothing more on 11-19. The first daily batch is due at 08:00
Pacific on 11-20 and has not been observed yet.

A preview on a copy of the live database the same evening (new code): 302 jobs waiting once closed ones were left
out. The batch would take the oldest 50 (collected from 11-10). Only 4 of them had any company record, because
auto-app holds 24 recorded applications.

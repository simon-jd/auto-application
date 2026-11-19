# 2026-11-18 — Weekly board proposals on Discord, and incremental Grok feeds

## Why

The long-tail boards were added once by hand (477, the same day). New companies keep appearing in the aggregators, so
the owner asked for a weekly list to approve on Discord. Grok's LinkedIn, Handshake and Jobright files turned out to
be one-off snapshots (10-31, 11-06, 11-11). A snapshot that is never refreshed keeps its postings "seen" forever, and
the employer links Grok could capture were not used for LinkedIn and Handshake.

## Change

- **`worker/board_proposals.py`** with table `board_proposals` (migration `d1f6b2c4e7a8`).
  - Weekly: up to 10 unconnected Greenhouse, Ashby and Lever boards with kept jobs.
  - Ranking: the owner's applied and wanted jobs at the company, then kept jobs.
  - Left out: boards proposed before, likely vendors (annotation, translation, staffing), and companies skipped twice
    and never wanted.
  - `decide(add)` appends `board-<platform>-<board>` with `every_hours: 6`. Before that it makes a dated backup and
    validates the new file with `load_feed_config`. The new file replaces the old one atomically; a taken name gets
    a numeric suffix. `decide(skip)` records the board, so it is never proposed again.
- **Bot.** `post_board_proposals` in the five-minute loop generates weekly and posts each proposal once, with
  **加入（每 6 小时）** and **不加** (`bp:<id>:add|skip`, owner only).
- **Incremental feeds.** A repository feed with `incremental: true` uses format `simplify` and a local file pattern.
  Each new or changed file matching the pattern is imported once as an incomplete snapshot (`run_incremental`), so a
  later file that leaves a posting out never marks it missing. A file that fails is reported and read again next
  run. The source's health keeps the files' revisions.
- **Grok guide §10.** Daily files `/home/grokbot/feeds/daily-<site>-<date>.json`: the employer link as `url`, the site
  page as `listing_url`, flat in `feeds/` because the folder's default ACL gives postedism no `x` on new folders.
  Also the three feed entries and the Routine prompt.

## Status

Deployed 2026-11-19 (2,798 full-suite pass; schema `d1f6b2c4e7a8`); the deployed sources match the tested ones. On the
owner's go, the three daily feeds `grok-linkedin-daily`, `grok-handshake-daily` and `grok-jobright-daily` were added
to `feeds.yaml` (624 → 627 sources; backup `feeds.yaml.bak-before-grok-daily-20261119`). They read nothing until Grok
writes its first daily files; the owner is setting up the Grok Routine. The old snapshot feeds stay until the daily
files have run reliably for a week. The bot's first weekly board proposals are generated at its first loop after
the restart.

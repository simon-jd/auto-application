# Discord bot: digests and owner decisions

**Current state (2026-10-14):** updated source is deployed on Ubuntu (schema
`8d4e0f2a5b31`) and discord.py 2.7.1 is installed. The bot service is installed and passed
Linux systemd verification, but remains disabled/inactive. Only the credential `.example`
is staged; the real `runtime/credentials/bot.env` has not been created. The owner will fill
the Bot Token, User ID, Guild ID and Channel ID on Linux, then choose activation using the
[Linux connector setup checklist](linux-connectors-setup.md). No Discord message or live bot
interaction was performed. Optional Webhook alert configuration is also in that checklist.

**Historical state (2026-10-12):** local bot implementation was complete and the integrated suite
passed 1,579 tests; credentials, service installation/activation and live checks were still pending.
The existing webhook-based worker remains documented in [Linux worker §3](linux-worker.md#3-discord).

The bot has no AI chat or model calls. It posts each eligible job as a separate Discord message with its four decision buttons directly below the card. It sends up to 30 jobs per check, prioritizing visa sponsorship. The worker unit template runs `--steps screen,enrich,summary,health` so the bot owns digest delivery and notification records. Add `notify` for a webhook-only setup. Feed ingestion continues independently.

The bot checks every five minutes, after a worker round with successful screening and any rescreening, while unpaused. An enrichment-only failure leaves the round `partial` and allows the digest to proceed. The bot uses the worker's `source_since` cutoffs to exclude a newly added feed's existing jobs. Before each message it rechecks the job's eligibility and stops the check if a new worker round intervenes. It marks the job notified after Discord acknowledges the message. If the response is lost after Discord accepts it, a retry can still produce a duplicate.

## Create and configure

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an application and Bot, then copy its token privately. In OAuth2's URL Generator, select `bot` and `applications.commands`, and grant the bot Send Messages and Embed Links in the intended channel. Use the generated URL to install it in your own server. [Discord's command authorization guide](https://docs.discord.com/developers/docs/interactions/slash-commands) describes guild command authorization.
2. Enable Discord Developer Mode and copy your own user ID, the server ID and the digest channel ID. The bot enforces both the configured owner ID and guild; other users cannot operate commands or buttons.
3. On Linux create `$AUTO_APP_HOME/credentials/bot.env` as a **regular file with mode 0600** (the default production path), containing exactly these four values:

```dotenv
AUTO_APP_DISCORD_BOT_TOKEN=YOUR_PRIVATE_BOT_TOKEN
AUTO_APP_DISCORD_OWNER_ID=YOUR_NUMERIC_USER_ID
AUTO_APP_DISCORD_GUILD_ID=YOUR_NUMERIC_SERVER_ID
AUTO_APP_DISCORD_CHANNEL_ID=YOUR_NUMERIC_CHANNEL_ID
```

```bash
chmod 600 "$AUTO_APP_HOME/credentials/bot.env"
uv run auto-app bot run --db "$AUTO_APP_HOME/data/auto_app.db" \
  --credentials "$AUTO_APP_HOME/credentials/bot.env"
```

The command requires this private file at startup; loading environment variables directly is a helper for tests, not the production startup path. Do not put the token in `.env`, the repository, chat, or a systemd `EnvironmentFile`. The [bot service template](../deploy/auto-app-bot.service) uses the explicit `--credentials` path and skips startup when the file is absent. It is already installed inactive; after filling the private file, follow the [current activation commands](linux-connectors-setup.md#3-由你选择启动已配置的组件).

## Commands and buttons

| In Discord | Effect |
|---|---|
| `/jobs sponsorship? company? query?` | Show up to ten active, visible undecided/recommended jobs; known rule exclusions are hidden. Each filter is optional; `sponsorship` offers `offers`, `no` or `unknown`. |
| `/job job_id` | Show one saved job by stable ID. |
| `/status` | Show ingest and screening-worker status plus pause state. |
| `/pause hours?` | Pause screening/notification worker and bot digest. Omit/zero hours for an indefinite pause; positive hours set an expiry. |
| `/resume` | Resume the screening worker and digest at their saved schedules. |
| **Want to apply** | Record `want_to_apply` for this job. |
| **Skip** | Record `not_applied` for this job. |
| **Applied** | Record the owner's report that they applied; it never submits a form. |
| **Shouldn't recommend** | Opens a box for the reason (required, 1–300 characters). Records `not_applied` with reason `shouldn't recommend: …`, and saves the reason for the daily screening-rule summary (2026-11-18). |
| **Apply** / **Discard** (on a screening proposal) | Apply the proposed `screening.json` change, or drop it. Either decision resolves the reasons while the proposal is active. At 24 hours, the proposal expires and its unresolved reasons return to the next summary. |

Only the configured owner can use these controls. A button result updates the same manual decision read by Dot/Grok and query tools. Pressing **Applied** is appropriate only after the owner actually submitted an application elsewhere. The bot does not change filter rules on its own or run a browser: a screening change takes the owner's Apply.

Pause affects the screening/enrichment/notification worker and bot digest; it does **not** stop the repository/IMAP ingest worker. Inspect `/status` and `auto-app ingest status` separately. A fresh bot deployment should be accepted with `/status`, `/jobs sponsorship:offers`, `/jobs company:<name>`, `/job <id>`, one owner button followed by a Dot readback of the same job, and one non-owner interaction rejected. None of those live checks is recorded as complete yet; see [P1 acceptance](updates/2026-10-12-p1-coverage-and-control.md).


## Handoff outcomes and tracking (2026-10-13)

The five-minute loop also sends pending [external-agent outcomes](agent-handoff.md). Outcome delivery
is independent of whether a new-job digest exists or screening succeeded, but honors pause and the
configured guild. Each acknowledged message advances `handoff_cursor` in `<db>.bot-state.json`,
separate from worker state. Failed messages retry; a lost acknowledgment can duplicate a message.

The worker in bot mode must retain `screen,enrich,summary,health`; its separately configured webhook
owns rolling weekly summaries and disk/source/step alerts. The bot owns both new-job digests and
handoff outcomes, so worker `notify` is omitted. Pressing Applied now records a manual applied
[timeline event](application-tracking.md) along with the manual decision. No new slash event command
or AI chat was added. P2/P3 notification tests are local; actual outcome receipt is still pending.

## "Shouldn't recommend" and screening proposals (2026-11-18)

The server runs no Claude filter, so a pushed job has only passed the deterministic rules in `screening.json`. A
"Shouldn't recommend" reason goes to `screening_feedback` with the job's title, company and screening verdict.

- **The daily summary.** `deploy/screening-feedback.timer` runs it at 08:30. It starts `claude -p` only when 3 reasons
  are pending, or the oldest has waited 3 days.
- **What it produces.** It tries words with `auto-app screen-feedback preview`, which saves nothing, then saves one proposal through
  `propose`, with a note and the usual preview. Reasons that keywords can't express are closed with a note.
- **What the bot posts.** The five-minute loop posts each open proposal once (posted ids in `<db>.bot-state.json`
  under `posted_proposals`). The post shows:
  - the note;
  - the diff;
  - kept-job counts before and after;
  - up to 5 examples of newly excluded and newly kept jobs;
  - the reasons, with Apply / Discard.
- **Closed reasons.** Those posted once, in one message.

Only the owner's Apply changes the rules. On a successful Apply or Discard, or when a click finds the proposal
expired or gone, the bot removes the buttons. If another screening change holds the lock, the buttons stay
available so you can retry. If the rules revision has changed, **Apply** asks for a fresh preview and
**Discard** stays available for the current proposal.

## Claude's questions for you (2026-11-18)

When the package runner cannot go on without you, it asks with `apply_kit.py ask` (`jobq ask`, owner only). Typical
cases are a stage-2 stop (a generic posting, the track, a start before graduation), a change it did not make, or a
fact only you know. The bot posts each question once to `AUTO_APP_DISCORD_QUESTIONS_CHANNEL_ID`, or to the main
channel when that is not set. A question is not posted again while it is unanswered.

- **The message.** The job (`#number company — title`, linked to the posting), the question, its context, Claude's
  recommendation, and these buttons:
  - up to 4 answers;
  - **其他回复…**, a box for your own words (up to 1,000 characters);
  - **不投**, for a job.
  Only the configured owner can answer, and the buttons go away once an answer is saved.
- **What happens next.**
  - **不投** records `not_applied` at once.
  - Any other answer is saved in `owner_questions` (migration `c9e5a1b3f4d6`) and kicks the runner. The runner's
    `todo` lists it under `answered_questions`. Claude acts on it (SKILL.md 定时自动处理 §11), for example finishing
    the package and publishing it, and marks it done with a note.
  - The bot replies under your question with that note, for example "已处理：材料已发布，进入投递队列，Grok 下一轮会填表".
  - A published job is in the queue, and Grok's 15-minute Routine picks it up.
- **Pause.** Questions are not held back by `/pause`.

## Digest pace (2026-11-18)

The new-job digest sends at most **5 messages per five-minute check**, **10 per rolling hour** and **100 per
rolling 24 hours** (`DIGEST_PER_CHECK`, `DIGEST_PER_HOUR` and `DIGEST_PER_DAY` in `bot/app.py`).

Each card has a **同公司记录** field (`worker/history.py`) with your record at that company:
- the jobs you applied to there, with titles and dates;
- how many you want to apply to, and how many you skipped;
- the 30-day submissions against the handoff's company limit;
- a ⚠ line when you applied to, or want, a job with the same normalized title.

It is computed from the database with the handoff's `company_groups`, not by a model. The day is counted from the bot's own sends, kept as
`digest_sent` in `<db>.bot-state.json`. `notified_at` is not used for this count, because it also marks jobs the
owner chose not to see.

When the bot first went live it worked through a month of backlog: notifications had been handed to the bot since
2026-10-14, but the bot was never configured. It pushed 701 jobs in three hours. On the owner's go, 4,455 pending
jobs first seen more than 3 days earlier were marked notified without being sent. They stay in the library
(`/jobs`, `jobq search`). Their list is in `runtime/data/digest-cleared-20261118.json`; clearing `notified_at` for
those IDs sends them again.

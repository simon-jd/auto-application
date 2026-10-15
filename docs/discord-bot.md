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

The bot has no AI chat or model calls. It posts eligible new-job digests and offers buttons for manual tracking. The worker unit template runs `--steps screen,enrich,summary,health` so only the bot sends/marks digest batches (add `notify` back only for a webhook-only setup without the bot); feed ingestion continues independently. The bot polls every five minutes once a round has finished with successful screening (and rescreening, when it ran) and while unpaused. An enrichment-only failure leaves the round `partial` but does not hold the digest back, because the verdicts are current. The bot uses the worker's `source_since` cutoffs, so a newly added feed's existing jobs are not pushed. Before each batch it checks current eligibility, including manual decisions; if a new worker round intervenes, it leaves the remaining jobs for the next digest check. A successful Discord batch is marked notified; a failed batch remains pending. Discord acceptance followed by a lost response can still cause a duplicate on retry.

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

Only the configured owner can use these controls. A button result updates the same manual decision read by Dot/Grok and query tools. Pressing **Applied** is appropriate only after the owner actually submitted an application elsewhere. The bot does not change filter rules or run a browser.

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

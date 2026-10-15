# Connector review fixes — 2026-10-14

Fixes for the connector/deployment review, applied by request (findings 1, 2, 3, 6, 7; the Grok
authentication and public control-tool questions stay with the owner).

- **Worker unit runs bot-mode steps.** `deploy/auto-app-worker.service` now passes
  `--steps screen,enrich,summary,health`, matching the deployed worker; with the old default,
  adding the webhook would have made the worker post digests and handoff outcomes alongside the bot.
- **Deploy restores the Grok tunnel.** `job-search-grok-tunnel.service` `Requires` agent-public, so
  stopping agent-public stopped it; it is now part of the deploy service snapshot and started again.
- **Persistent failures alert.** Configuration errors exit 2 — the agent CLI's validation errors and a
  Discord-rejected bot token (bot credential errors already did) — and the bot/agent/agent-public units
  set `RestartPreventExitStatus=2`. Bot, agents and the Grok tunnel also stop after 10 starts in 30
  minutes. Either way the unit reaches `failed`, so `OnFailure` alerts instead of looping silently.
- **Any US location keeps a job.** Country exclusions no longer hide a job that also lists a US
  place or a structured `US` country ("Seattle, WA / Vancouver, BC"), in preferences, the query
  service's SQL filter, the handoff queue and TTL archiving. Excluding "United States" itself turns
  the exception off. The location classifier is now per place: "Toronto, ON, CA" is Canada (it was
  read as California), "Toronto, ON / Boston, MA" is US. Screening `RULES_VERSION` is now 7, so
  saved jobs are screened again.
- **Polite modals no longer hide rejections.** "while your skills may be a fit … you were not
  selected" is a rejection; conditionals (if/unless/once/whether) still cancel one.

Unit file changes take effect only after copying them to `~/.config/systemd/user/` and
`daemon-reload` on Linux ([setup](../linux-connectors-setup.md#3-由你选择启动已配置的组件)); the
deploy script syncs code, not units.

## Deployment (2026-10-15, owner-authorized)

- Local full suite before deploying: **1912 passed**. Previous host units were saved to
  `~/Desktop/job-search/checks/unit-backup-<UTC timestamp>/`; the worker drop-in
  `auto-app-worker.service.d/runtime.conf` (optional `worker.env`, same `--steps`) was kept.
- Installed worker, bot, agent, agent-public, Grok tunnel, preparation and Dot tunnel units;
  `systemd-analyze --user verify` passed. Bot, agent-public and the Grok tunnel stay disabled.
- `scripts/deploy_linux.sh home-ubuntu` (rsync) backed up the main and preparation databases,
  synced, migrated (schema `8d4e0f2a5b31`) and restarted ingest, agent, preparation and worker.
  Effective settings: `RestartPreventExitStatus=2` and 10 starts / 30 min on bot and agents; the
  worker runs `screen,enrich,summary,health`. Dot's MCP on 8767 answered `initialize` with 200.
- First round on rules v7 (06:06 UTC): `ok`, 12,358 jobs screened, 4,247 kept / 8,111 excluded
  (unchanged). Country preferences now hide 647 active jobs instead of 676: 29 jobs with a US
  location beside Canada became visible. No commit or push was performed; no Discord, Grok or
  public tunnel was started.

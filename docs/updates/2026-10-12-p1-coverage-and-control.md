# P1 coverage and control — 2026-10-12

**Steps:** 1, 2, 4  
**Status:** code and local verification complete; user-run deployment and live acceptance pending. No commit or push.

## What changed locally

- Employer-board ingest accepts `greenhouse-board`, `lever-board` and `ashby-board` public listing formats, optional `company`, and exact ATS identity reuse. `auto-app worker boards --yaml` suggests boards from saved kept jobs; disabled examples are in `config/feeds.example.yaml`. Workday stays on the existing single-posting JD enrichment path.
- IMAP mailbox ingest reads Gmail or another TLS IMAP server through a mode-600 JSON credential file. It uses EXAMINE and BODY.PEEK and reuses mailbox receipts, pagination and retry state. No real account was opened.
- A second MCP listener uses a mode-600 Bearer token, strict HTTPS public-origin/Host validation and a loopback bind intended for a named public tunnel. The existing Dot listener remains private. Optional control tools report status, pause/resume the screening worker, preview and revision-check rule changes, record filter feedback, and read a pending digest. The preview stores a proposal; activating rules requires the same change ID and unchanged revision after the user has seen and confirmed it.
- A Discord bot provides an owner-and-guild restricted digest, decision buttons and `/jobs` (including company), `/job`, `/status`, `/pause`, `/resume`. Bot mode uses worker `--steps screen,enrich`; feed ingestion continues during pause. Existing worker webhook alerts remain available. The bot has no AI conversation and submits no applications.
- Deployment artifacts add `auto-app-agent-public.service` and `auto-app-bot.service`; the deployment script recognizes both when stopping and restoring already-running services. Creating a service file is not proof of a running service.

## P0 review follow-up carried into this batch

Seven correctness issues were repaired locally: conservative Workday requisition matching; per-record Ashby errors and unlisted-job reads; worker outage threshold and stable alert fingerprint; rescreen after changed facts or partial writes; manual decisions remaining visible despite rule exclusion; employer-tenure text not being mistaken for candidate experience; and persistence of an explicit notification cutoff. The eighth candidate, full-table digest scan optimization, is **deferred**. It is not described as fixed. `ReportFindings` was not available as a callable tool in this work, so these outcomes are recorded here and in the task report rather than sent through that interface.

## Verification boundary

**Final integrated suite: 1,579 passed in 517.36 seconds (8:37)** with `.venv/bin/python -m pytest -q --tb=short`, exit 0. One dependency warning notes discord.py's `audioop` deprecation for Python 3.13; this run used Python 3.12. The 324 source, test, configuration and deployment files were frozen before testing and their exact file set and SHA-256 hashes verified afterward.

Scoped local tests also passed: P0 repair 190; auth/control/pause 152; board/IMAP/CLI 60; Discord bot 63; deployment handling 11. These are separate runs and are not added to the full-suite count. Local public-MCP tests exercised 401 for absent/incorrect credentials and rejected unknown Host values; they do not prove tunnel or Grok interoperability. No live employer requests, Gmail login, Discord message, public tunnel, SSH deployment, model call, application submission or cloud connector call was performed for P1.

Independent specification and code reviews approved Tasks 1–8 for local implementation after the following regression-backed corrections:

- IMAP date searches cover server-local date boundaries before precise UTC filtering; credential-path normalization preserves symlink rejection.
- A malformed pause file fails closed. An idle worker daemon holding its scheduling lock does not block a healthy bot digest.
- MCP digest selection receives the same explicit country-preferences file as library search. Protocol tests cover Canadian and mixed-location jobs and a missing required configuration file.
- Each bot batch rechecks pending eligibility, including intervening manual decisions. A completed new screening round invalidates the remaining delivery plan. Fake-channel tests confirm excluded or manually skipped jobs are neither sent nor marked notified.

The final independent reviewer reran both original integration reproductions successfully after the fixes, plus 59 control/service/digest tests and 63 bot/digest/CLI tests. No actionable review finding remains open. Five deployment documents' local links, CLI help, deployment-script syntax and diff whitespace checks passed. These local checks do not replace the live acceptance below.

## P1 review fixes (2026-10-13)

Three review findings were reproduced with failing tests and fixed:

- **US-only screening.** Employer boards list jobs worldwide. A new `us_only` rule (default on) excludes a
  job with reason `outside_us` when every explicit place it names is outside the US: another country, a
  region (EMEA, APAC, Europe …), a well-known foreign city, or Lever's structured country (now kept in the
  board feed's upstream metadata). Any US location keeps the job; "Remote" alone and unrecognized places
  stay. Screening signals gain `location: us | non_us | unknown`. `RULES_VERSION` 5 rescreens every job.
- **Per-source digest cutoffs.** A feed added after the first run used to announce its whole backlog. The
  worker now records `source_since` per source in its state: sources present before `notify_since` follow
  it; a later source gets the round in which the worker first saw it, so only jobs appearing afterwards are
  pushed. The worker digest, the Discord bot and the MCP digest preview all apply it. State written before
  this change has no registry and keeps the previous behavior until the next worker round records one.
- **Bot readiness.** The bot now requires a finished round with successful screening (and rescreening when
  it ran) instead of an `ok` round, so an enrichment-only outage (`partial`) no longer stops digests.

The remaining four review items were then fixed the same way (failing test first):

- **Malformed control file.** The worker no longer exits and restart-loops: it fails closed (no rounds),
  records `status: control_invalid` with `control_error`, alerts Discord once, and resumes rounds as soon
  as resume/pause rewrites the file. `/status` and `get_pipeline_status` report the invalid file instead of
  failing. `worker run` prints `control_invalid` and exits nonzero.
- **Duplicated descriptions.** Import records (`ingest_entries.payload`, `ingest_changes.payload`) now store
  a description's SHA-256 and length instead of its text; the text stays on the posting, and a changed
  description is still an update. Records without a description keep their earlier shape, so existing
  repository content hashes are unchanged. Screening reads only the three upstream labels it uses with
  `json_extract`, not whole payloads.
- **Workday requisitions.** Matching now tolerates year-prefixed and letter-suffixed ids with copy suffixes
  (`2026-12345-1`, `R0123456A-2`) and rejects two different copies (`REQ-2026-01` vs `REQ-2026-02`), which
  the previous matcher wrongly accepted.
- **Stale proposals.** Screening proposals expire after 24 hours; an expired one cannot be applied, and
  expired or unreadable proposals are pruned whenever a new one is made.

## User-run acceptance still pending

1. Review the current checkout and deploy to `home-ubuntu` only when authorized. On Ubuntu, inspect `auto-app worker boards --yaml --top 30`; select and enable board feeds in **runtime** `config/feeds.yaml` while preserving existing feeds. Check later ingest rounds and `auto-app-query context` for `board-*` sources. Measure at least 30 distinct employer boards arriving daily and compare overlapping repository records for correct deduplication. No count is yet claimed.
2. Install private `runtime/credentials/imap.json` and enable only confirmed recommendation-mail scope. Confirm a real LinkedIn/Jobright alert yields a saved job within 30 minutes, repeated rounds do not duplicate it, `messages_processed > 0`, and the source email is still unread. None of these observations exists yet.
3. Configure the second listener's token and one public HTTPS tunnel. Verify external unauthorized requests return 401, authorized requests work, and Grok can call `get_job_context` and `search_jobs`. The [xAI custom connector guide](https://docs.x.ai/grok/connectors) specifies a server URL and authentication but does not establish a static Bearer option in the UI. If it only accepts OAuth, this endpoint needs a separate OAuth plan before Grok acceptance.
4. Create and install the Discord bot. Check owner-only commands, a non-owner denial, a delivered digest, and a **Want to apply** button whose `want_to_apply` state is subsequently visible from Dot. Confirm `/pause` stops the screening worker and bot digest while feed ingestion proceeds; `/resume` restores their schedules.

Detailed operator steps: [boards and worker](../linux-worker.md#6-employer-boards), [IMAP](../mailbox-linux-setup.md), [remote MCP](../remote-mcp.md), and [Discord bot](../discord-bot.md). These are procedures, not evidence of execution.

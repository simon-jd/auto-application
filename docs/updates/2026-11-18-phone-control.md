# 2026-11-18 — Phone control: faster pickup, change requests and status, Remote Control, "Shouldn't recommend", graduation rule

Design: [specs/2026-11-18-phone-control-design.md](../superpowers/specs/2026-11-18-phone-control-design.md). Plan:
[plans/2026-11-18-phone-control.md](../superpowers/plans/2026-11-18-phone-control.md).

## Why

The owner drives the pipeline from the phone, mostly by talking to Grok Bot. Three things got in the way:

- After "apply to #a", the package runner started only at the next hour.
- A change request ("more Iceberg on #a's resume") had no way in unless a form was open.
- Grok could not answer "is #a ready yet?".

The owner also wants to talk to the server's Claude directly. Grok must not become a way to send Claude arbitrary
instructions: Grok reads web pages and mail, and the server's Claude runs as `postedism` in auto mode.

On Discord, the owner wants to say a pushed job should not have been recommended. The server runs no Claude filter (0
judgments), so the jobs it pushes have only passed the deterministic rules in `screening.json`. The owner chose
option A: reasons become screening-rule proposals, and the owner confirms them. Option B, a Claude filter, is the
fallback.

Jobs that start before the owner's graduation (May 2027) reached the queue, and only the jd-resume-tailor stage 2
stopped them.

## Change

- **Kick file** (`auto_app/handoff/kick.py`). It touches `$AUTO_APP_HOME/data/apply-queue.kick` whenever there is new
  work for the package runner: a changed `want_to_apply` from any entry (jobq, Discord, MCP, CLI), `questions`, or
  `request`. The new `deploy/jd-apply-queue.path` (`PathChanged=`) starts `jd-apply-queue.service` at once. The hourly
  timer stays as the fallback. A failed touch is ignored and never fails the request.
- **Runner rounds and state** (`deploy/jd-apply-queue.sh`).
  - When a kick arrives during a run, the runner does one more round afterwards, at most 3 rounds in all.
  - It writes `apply-kit/runner.json`:
    - while a round runs: `running`, `round`, `started_at`, `items`;
    - when it ends: `ended_at` and `exit`;
    - after an idle check: `checked_at`, keeping the last run under `last_run`.
- **`HandoffService.request_change(job_id, text, agent)`**: the owner's words, verbatim, asking for a change to a
  wanted job's materials while no form is open.
  - Text is 1–1000 characters. It is stored as a `kind: change` item in `handoff_questions`.
  - The job is parked as `needs_answers`, so no agent fills a form with the old package. An application row is
    created if the job was never claimed.
  - A second request before the first is handled joins the same round and moves `submitted_at` to now. A package
    published in between therefore cannot pass `answers-ready`.
  - It is refused when:
    - the form is claimed or held (the change then goes through `questions`, kind change);
    - the job is parked;
    - the job is not `want_to_apply`.
  - Interfaces: `jobq request ID --text TEXT` (works through sudo) and MCP `request_material_change`.
  - The skill needs no new flow: `todo` lists these jobs under `needs_answers`. A job with no package yet (folder null)
    goes through the full flow, with the texts as requirements (SKILL.md 定时自动处理 §3).
- **`HandoffService.status(job_id)`**: the job's decision, latest application, live claim (agent, until, held),
  pending questions and changes, published package (from `index.json`), the runner state (`runner.json`) and a
  one-line `summary`.
  - It never returns a token or a hash, and never returns runner logs or `_runs/` summaries.
  - Interfaces: `jobq status ID` and MCP `get_application_status`.
- **Remote Control** (`deploy/claude-remote.{sh,service}`).
  - The service runs `claude remote-control --spawn same-dir --permission-mode auto --name job-search` inside tmux
    (`tmux -L claude-remote attach -t claude-remote`), in `auto-application` with `AUTO_APP_HOME` set to the runtime. It restarts
    after a network drop.
  - The owner opens it from the Claude app (Code). The project's CLAUDE.md rules apply.
  - The first start asks "Enable Remote Control?" and for folder trust. The owner answers both in tmux.
- **"Shouldn't recommend"** (Discord). Each pushed job gets a fourth button, owner only.
  - It opens a modal asking for a reason (required, 1–300 characters).
  - The job becomes `not_applied` with reason `shouldn't recommend: …`. The reason goes to the new table
    `screening_feedback` (migration `a7c3e9f1d2b4`), with the job's title, company and screening verdict as they were.
- **Daily summary** (`deploy/screening-feedback.{sh,service,timer}`, 08:30).
  - `auto-app screen-feedback due` checks the threshold with no model: 3 or more pending reasons, or the oldest 3
    days old. Below it, no Claude session starts.
  - Otherwise `claude -p` reads `screen-feedback list` and saves one proposal with `screen-feedback propose`. The
    proposal is `ControlService.propose_screening_change` with a note; the diff, preview and note are now saved in
    the proposal file.
  - Reasons no rule can express are closed with `screen-feedback close` and a note.
  - The summary never applies anything.
- **Proposals on Discord.** The bot posts each open proposal once: the note, the diff, kept-job counts, up to 5
  examples per side, and the reasons.
  - Apply calls `apply_screening_change`. Discard calls the new `discard_screening_change`.
  - Either way the reasons are resolved.
  - Proposals expire after 24 h (unchanged). An expired or missing proposal releases its reasons back to pending, so
    the next summary proposes again.
  - No-change closures are posted once.
- **Graduation rule** (screening v9). `ScreeningRules.graduation` (`"YYYY-MM"`, default null = off) excludes a job
  whose explicit evidence ends before graduation. The reason is `start_before_graduation:YYYY-MM`.
  - What counts as evidence:
    - a title that names a class: "2026 New Grad", "New Grad (2026)", "Class of 2026", "2026 Start";
    - a required sentence with a start date ("start date: January 2027", "starting summer 2026", "a Winter 2026
      start");
    - a graduation deadline ("graduating between Dec 2025 and Jun 2026", "must graduate by August 2026");
    - a class in the description ("Class of 2026", "2026 graduates").
  - How dates are read: a season ends in its last month (Spring = May, Winter Y = February Y+1). A bare year ends in
    December. Years more than 2 before graduation are history. With several dates, the latest decides.
  - These are kept:
    - earliest dates and open ends ("as early as", "or later", "after");
    - hedged or preferred sentences;
    - "starting salary";
    - "startup".

## Review fixes (same day)

A fresh reviewer read the whole change. Fixed, each with a test that failed first:

- **A change that arrives while a package is being made is no longer lost.** `answers_ready(job_id, asked_at=...)`
  (`jobq answers-ready ID --asked-at SUBMITTED_AT`, owner CLI `--asked-at`) refuses when the round's `submitted_at`
  is not the one the package answers. `apply_kit.py answers-ready DIR --asked-at …` (now required) checks it, passes it
  on, and SKILL.md says to pass `todo`'s `submitted_at`. The kick from the late request gives the runner one more
  round, which handles it.
- **The graduation rule no longer reads process dates.** These are no longer read as the job's start date:
  - sentences about applications, interviews, postings, pay, benefits, eligibility or the law, for start words;
  - a leading "Beginning January 1, 2025, …";
  - "since starting in";
  - numeric dates (`11/18/2026`, `2026-11-10`, `Req 2025-04567`);
  - negated sentences;
  - titles of events.

  A graduation deadline now needs its connector ("graduate by", "graduating between", "graduation date of").
- **`screen-feedback preview`** shows a change's effect without saving a proposal or holding reasons. The daily
  summary tries words with it and proposes once. Before, a second `propose` with the same ids failed.
- **`status.queue_block`** reports what keeps a wanted job out of the queue, from the queue's own guardrails, and the
  summary says so. It used to say "the runner starts within the hour" for a job the runner never sees.

Deferred (minor): the runner's `>=` kick check can add one more full round when work is left after round 1. These are
also deferred:

- Discard of an expired but unpruned proposal resolves its reasons as discarded.
- The proposal buttons are removed after any reply, even a "retry" one.
- `status` drops the runner's `error` key.
- The summary runs while the worker is paused, and proposals are made about every 48 h against the 24 h expiry.
- A closure message cut at 1,900 characters still marks every item posted.

## Follow-up: answers for a job parked for the user (same day)

Job #206's package had nine `[USER TO CONFIRM]` items in `answers.md`, such as travel and startup experience. Grok
recorded `needs_user` correctly, and the owner answered Grok. But a `needs_input` job could only come back through the
owner's `requeue`, and requeuing it as it was would have had Grok fill answers that are not in the package. Now:

- `request_change` accepts a job parked as `needs_input`. The owner's answers go to Claude as `kind: change` items,
  Claude revises and republishes, and `answers-ready` returns the job. A blocked job (`action_needed`) still refuses.
- `status` points to `request` for a job parked for the user.
- Cheat-sheet rule 10: one answer per `request`, in the owner's words; Grok does not keep answers in its own Routine.
- Deployed the same day (2,731 full-suite pass). That deployment also carried the Discord proposal-decision fixes from
  the separate Discord session ([2026-11-18-discord-proposal-decisions](2026-11-18-discord-proposal-decisions.md)).
  The cheat sheet and SKILL.md are synced, and `jobq status 206` points to `request` live.

## Follow-up: cheat-sheet requests (same day)

The owner wants to give Grok answers that hold for every application and have them added to the cheat sheet. Grok
does not edit the cheat sheet, because it also holds Grok's own rules (rule 8: confirm before Submit), and Grok reads
web pages and mail. An injected instruction written there would apply to every later application. The owner
chose to have Claude write instead:

- **`jobq cheatsheet --text TEXT`** / MCP `request_cheatsheet_change`. It stores the owner's words verbatim in the new
  table `cheatsheet_requests` (migration `b8d4f0a2e3c5`) and kicks the runner.
- **`jobq cheatsheet-requests`** / MCP `get_cheatsheet_requests` shows pending requests and the last 20 done ones.
  **`jobq cheatsheet-done ID --outcome applied|declined --note`** is the owner's step and is refused through sudo.
- **The skill.** `apply_kit.py todo` lists `needs_cheatsheet` and the cheat sheet's path, and `apply_kit.py
  cheatsheet-done` reports the outcome. A server without the command gives a warning. SKILL.md 定时自动处理 §10:
  - Claude edits only on the server, and only answer sections, never 使用规则.
  - It saves a copy to `apply-kit/.cheatsheet-history/` first.
  - It declines instruction-like words, one job's answers (use `request`), and anything unclear, with a note.
  - The runner counts these requests as work.
- **Cheat-sheet rule 11** for Grok.
- Deployed the same day (2,743 full-suite pass; schema `b8d4f0a2e3c5`). The skill (256 tests pass on the server), SKILL.md and the cheat sheet are synced. `jobq cheatsheet-requests` is empty, `apply_kit.py todo` lists `needs_cheatsheet` with no warning, and no real request has been made yet.

## Follow-up: the forms' real questions, limits and own-words answers (same day)

Several `answers.md` files were written for guessed questions that did not match the real form (#123 Ashby,
#546 Lever). Some forms cap answers (Greenhouse one-line fields hold 255 characters). Others ask for answers
without AI: #123 says "please don't use AI", and #43005 Canonical asks the applicant to agree to "use only my own
words" for the whole application. Skill and cheat-sheet changes (no auto-app server code):

- **`scripts/form_questions.py`.** `apply_kit.py fetch` saves `form.json` with every question's text, whether it is
  required, its type, its `limit` and `own_words`, plus a form-level `own_words`. It reads three sources without a
  login:
  - Greenhouse through its Job Board API (`?questions=true`);
  - Ashby through the endpoint its application page reads;
  - Lever from the application page.

  Other sites make no request. A failed read is reported in `fetch`'s `form` and never stops the fetch. Checked
  live on #43005, #123 and #546.
- **`publish` checks `answers.md` against `form.json`.** It matches each `## ` heading to a question by its text.
  It refuses an answer over the question's limit (words or characters), and any written answer where the form
  wants the applicant's own words. Those answers carry `[本人填写]`.
- **SKILL.md.** The generation subagent writes every open question from `form.json` within its limits and marks
  own-words questions `[本人填写]`. For an auto-app job whose form cannot be read (Workday), it no longer guesses:
  `answers.md` holds one line, and Grok hands back all open questions with page and limit. URL-mode use keeps
  `WebFetch` and inference.
- **Cheat-sheet rule 2 for Grok.**
  - When `answers.md` says the questions are not yet known, Grok hands them all back.
  - Grok writes visible limits into `limit`. After pasting, it checks the field holds the whole answer; an
    over-limit or cut-off answer goes back with its limit, and Grok does not trim it itself.
  - For `[本人填写]` items, Grok lists them to the owner after `hold` and pastes the owner's own words unchanged.
- Synced to home-ubuntu the same day: the skill's 276 tests pass there, and the cheat sheet is live through
  `jobq guide`. Packages made before this have no `form.json`.
- **Redone the same evening.** #123 and #43005 got `form.json`. Their `answers.md` now has only `[本人填写]` items:
  #123 has two questions that say "please don't use AI", and #43005's whole form is own-words. Both were republished:
  #123 through `answers-ready --asked-at`, #43005 through the owner's `requeue`. #546 got `form.json` and an owner change
  request; the runner started on the kick. The old answers are kept as `answers.md.bak-before-form-20261118`.
- **#546 redone by the runner** (published 21:47). Its three real open questions are answered, and the 150-word one
  has 141 words. **#604 IMC and #749 Five Rings** were published on the owner's go once the Discord bot was set up.
  Their real forms have no essay questions, so their guessed `answers.md` was replaced by notes. Fix found doing it:
  EU Greenhouse boards (`job-boards.eu.greenhouse.io`) are read through `boards-api.greenhouse.io`, because
  `boards-api.eu.greenhouse.io` does not resolve. The test was written first.
- **Routine pacing** ([grok-bot.md §9.2](../grok-bot.md)) is now every 15 minutes with at most 3 per run. A run
  ends at once while an earlier run is still filling a form (`blocked.claimed` above Grok's own held forms), or when
  6 held forms already wait for the owner.

## Follow-up: frozen submitted packages and apply-kit backups (2026-11-19)

The seven submitted jobs had packages only in `apply-kit/jobs/<job>/`, which a later `publish --replace` overwrites,
and the nightly backup covered only the databases.

- **Freezing.** `handoff/archive.py` copies a submitted job's package files once to `apply-kit/submitted/<job>/`, with
  `frozen.json`. It runs on the `submitted` outcome and on the owner's `applied`, and never fails the record.
  `status.package.submitted_copy` shows it. The owner's `auto-app handoff freeze-submitted` backfills jobs applied
  before this, marked `backfilled`.
- **Backup.** `ops backup` also writes `apply_kit-<UTC>.tar.zst` (regular files only, no symlinks), with its
  manifest entry, `verify_tree` and its own retention. `ops verify` accepts it.

## Follow-up: documents Grok can upload (2026-11-19)

Some forms ask for files that are in no job's package, such as an unofficial transcript. `apply-kit/documents/` on
the server holds them:

- `jobq doc` lists name, bytes and sha256, and `jobq doc NAME` prints one file. MCP has `list_documents` and
  `get_document`. No token is needed.
- Names match `[A-Za-z0-9][A-Za-z0-9._-]{0,99}`. Only regular files are served, no symlinks, at most 10 MiB each.
- The cheat sheet's Transcript row now points to `jobq doc neu-unofficial-transcript.pdf`. Grok uploads it only where
  the form asks for a transcript, and the pre-submit list names every document uploaded, with its sha256.
- The nightly apply-kit archive includes the folder.

While deploying the frozen copies, it turned out the nightly backup had never been installed on the server: there was
no `auto-app-backup.service` or timer. Both are now installed. The first run on 2026-11-19 wrote
`auto_app-…db.zst` (253 MB), the preparation ledger and `apply_kit-…tar.zst` (287 files, verified). The timer runs at
03:30. `freeze-submitted` backfilled the jobs applied so far: 12 folders under `apply-kit/submitted/`, marked
`backfilled`.

## Status

Deployed to home-ubuntu 2026-11-18 10:42 PST (`scripts/deploy_linux.sh`, full suite 2,721 passed; schema `a7c3e9f1d2b4`).

- `jd-apply-queue.path`, `screening-feedback.timer` and `claude-remote.service` are installed and active. The skill's
  `SKILL.md`, `apply_kit.py` (251 tests pass on the server) and the cheat sheet are synced.
- Remote Control waits for the owner's folder-trust answer: `ssh -t home-ubuntu tmux -L claude-remote attach -t claude-remote`.
- The Discord bot was never configured on the server: there is no `bot.env`, and the service is disabled. Neither
  "Shouldn't recommend" nor any digest reaches Discord yet. Handoff: [discord-dev-handoff.md](../discord-dev-handoff.md).
- `graduation` preview on live data: 183 newly excluded, 0 newly kept (kept 14,248 → 14,065). 75 of the 183 are from
  title evidence and 108 from descriptions only; none are decided jobs. A sample of 25 description-only exclusions had
  24 correct and 1 false positive, an award list named "Class of 2026". On the owner's go the same day, proposal `ddfa8f75…`
  was applied: `runtime/config/screening.json` now has `"graduation": "2027-05"` (revision `85a3752ec5024a9c`; the
  previous file is kept as `screening.json.bak-before-graduation-20261118`). The worker applies it at its next round.

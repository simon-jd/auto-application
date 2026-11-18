# Phone control of the application pipeline: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Requests sent from the phone reach the server's Claude within seconds. Grok can ask for material changes and read progress. The user can talk to the server's Claude directly. A Discord "Shouldn't recommend" button turns reasons into screening proposals that the user confirms. Jobs that start before graduation are screened out.

**Architecture:** The handoff service gets three new pieces: `request_change`, `status`, and a kick file. jobq and MCP expose them as thin wrappers. The kick file triggers the existing runner through a systemd `.path` unit. A new `screening_feedback` table works with the existing proposal mechanism (`ControlService.propose/apply_screening_change`) to carry the Discord reasons. The `graduation` field is one more deterministic screening rule.

**Tech Stack:** Python 3.12, uv, SQLAlchemy + Alembic, Typer, discord.py, FastMCP, systemd user units, bash.

**Spec:** `docs/superpowers/specs/2026-11-18-phone-control-design.md`

## Global Constraints

- Never submit applications. Never apply a screening change without the user's click or confirmation.
- grokbot reaches only `jobq` through sudo. Owner-only steps stay refused through sudo (`_owner_only`).
- `request --text`: one line, 1 to 1000 characters. Discord reason: required, at most 300 characters.
- Feedback summary runs when at least 3 reasons are pending, or when the oldest pending reason is at least 3 days old.
- Screening proposals expire after 24 h (existing `PROPOSAL_TTL`). Reasons attached to an expired proposal return to pending.
- `graduation` is "YYYY-MM" or null. Null (the default) turns the rule off. `RULES_VERSION` becomes "9".
- Kick file: `$AUTO_APP_HOME/data/apply-queue.kick`. Runner re-runs at most 3 rounds.
- Not a commit-per-task repo for this work: the user commits. Run `uv run pytest -q` at the end.

## Review Focus

- A request arrives while Grok holds a live claim or a filled form. Expected: refused, and the message names `questions` with kind=change.
- A second request comes in before the first is handled. Expected: both texts are kept in one pending set, and `submitted_at` moves to now, so an older package can't pass `answers-ready`.
- A Discord proposal is clicked after it expired. Expected: "expired" reply, and its reasons return to pending.
- Historical years in a JD ("we started in 2015", "since 2019") and "as early as" / "or later". Expected: no exclusion.
- A kick lands while the runner is mid-run. Expected: one more round after it finishes.

---

### Task 1: Kick file, `request_change`, `status` in the handoff service

**Files:**
- Create: `src/auto_app/handoff/kick.py`
- Modify: `src/auto_app/handoff/service.py`, `src/auto_app/agent/service.py` (kick after a changed want_to_apply)
- Test: `tests/handoff/test_requests.py`

**Interfaces:**
- Produces:
  - `kick(home: Path) -> None`. It updates the timestamp of `home/data/apply-queue.kick` and never raises.
  - `KICK_FILE = "apply-queue.kick"`
  - `HandoffService.request_change(job_id: int, text: str, agent: str, now=None) -> dict`, returning `{"job_id", "status": "needs_answers", "question_set_id", "changes": int}`
  - `HandoffService.status(job_id: int, now=None) -> dict`, with keys `job_id, title, company, decision, application, claim, pending, package, runner, summary`
  - `RUNNER_FILE = "runner.json"` (in apply-kit)

- [ ] Tests (in `test_requests.py`, reusing `env`, `service`, and `NOW` from `tests/handoff/test_service.py`):
  - `test_request_parks_an_unclaimed_wanted_job_and_creates_its_application`. Asserts:
    - status is needs_answers;
    - the job is not in `queue`;
    - `pending_questions` lists `[{"text": T, "kind": "change", ...}]`;
    - the kick file exists.
  - `test_second_request_joins_the_pending_set_and_moves_submitted_at`.
  - `test_request_refused_while_claimed_or_held`: the error mentions "questions".
  - `test_request_refused_for_parked_or_unwanted_or_too_long`: covers needs_input, not_applied, and 1001 characters.
  - `test_answers_ready_after_request_returns_job_to_queue`.
  - `test_status_summaries`:
    - not published → "not made yet";
    - pending change → "waiting for Claude";
    - live claim → "being filled";
    - held → "waiting for your go";
    - published → "published".
    - Each also checks `runner` from a written `apply-kit/runner.json`, and that runner is None when the file is missing.
  - `test_want_kicks` (AgentService want_to_apply creates the kick; an unchanged repeat is fine).
- [ ] Implement:
  - Request text is validated through `_questions([{"text": text, "kind": "change"}])` plus a check of at most 1000 characters.
  - If no application exists, insert one the way `claim` does.
  - Refuse PARKED, and refuse a live claim (`lease_until > now`).
  - When the latest unanswered set exists and the application is needs_answers, append to that set and set `submitted_at=now`. Otherwise insert a new set and set the application to needs_answers.
  - Call `kick(self.home)` after commit.
  - `submit_questions` also kicks.
- [ ] `uv run pytest tests/handoff -q` → all pass.

### Task 2: jobq `request` / `status` and MCP tools

**Files:**
- Modify: `src/auto_app/jobq/cli.py`, `src/auto_app/agent/mcp_server.py`
- Test: `tests/jobq/test_requests_cli.py`, `tests/handoff/test_requests_mcp.py`

**Interfaces:**
- Consumes: Task 1.
- Produces:
  - `jobq request ID --text TEXT` (`plain_text(1000)`). It works through sudo.
  - `jobq status ID`.
  - MCP `request_material_change(job_id, text, agent)` and `get_application_status(job_id)`, both under `handoff_tools`.

- [ ] Tests:
  - `request` through the CLI parks the job.
  - `status` returns a summary.
  - `--text` with a newline, or longer than 1000 characters, gives exit 2.
  - Both work with `SUDO_USER` set.
  - Over MCP: both tools are present only with `handoff_tools=True`, and `request_material_change` with an empty text is an error.
- [ ] `uv run pytest tests/jobq tests/handoff -q` → pass.

### Task 3: Runner pickup: path unit, rounds, runner.json

**Files:**
- Create: `deploy/jd-apply-queue.path`
- Modify: `deploy/jd-apply-queue.sh`, `deploy/jd-apply-queue.service` (description only)

- [ ] `jd-apply-queue.path`:
  - `[Path] PathChanged=%h/Desktop/job-search/runtime/data/apply-queue.kick`
  - `Unit=jd-apply-queue.service`
  - `[Install] WantedBy=default.target`
- [ ] Script:
  - At most 3 rounds. A round repeats only if the kick file's mtime is later than that round's start.
  - Write `$kit/runner.json` atomically (tmp + mv):
    - at round start: `{"running": true, "round": n, "started_at": ISO, "items": work}`;
    - at the end: `{"running": false, "started_at", "ended_at", "exit": code, "items": work}`;
    - on an idle check: `{"running": false, "checked_at", "items": 0}`, keeping the last run's fields under `"last_run"`.
- [ ] Verify:
  - `bash -n deploy/jd-apply-queue.sh`.
  - A stub run in the scratchpad with fake `claude` and fake `apply_kit.py` shows 2 rounds when the stub touches the kick file during round 1, and 1 round otherwise.

### Task 4: Remote Control service

**Files:**
- Create: `deploy/claude-remote.service`, `deploy/claude-remote.sh`

- [ ] `claude-remote.sh`:
  - Exports `PATH` and `AUTO_APP_HOME=$HOME/Desktop/job-search/runtime`.
  - `cd $HOME/Desktop/job-search/auto-application`.
  - Runs a loop: `claude remote-control --spawn same-dir --permission-mode auto --name job-search`, then `sleep 30` before restarting.
- [ ] Service:
  - `Type=forking`
  - `ExecStart=/usr/bin/tmux new-session -d -s claude-remote %h/Desktop/job-search/auto-application/deploy/claude-remote.sh`
  - `ExecStop=/usr/bin/tmux kill-session -t claude-remote`
  - `Restart=on-failure`
  - `[Install] WantedBy=default.target`
- [ ] `bash -n`, plus `systemd-analyze --user verify` on the server at deploy time.

### Task 5: Screening feedback store, proposal notes, CLI

**Files:**
- Create: `migrations/versions/a7c3e9f1d2b4_screening_feedback.py` (down_revision `e5a1c9d0b7f2`), `src/auto_app/screen/feedback.py`, `src/auto_app/screen/feedback_cli.py`
- Modify: `src/auto_app/core/db/tables.py`, `src/auto_app/agent/control.py`, `src/auto_app/cli.py` (register `screen-feedback`)
- Test: `tests/screen/test_feedback.py`

**Interfaces:**
- Produces:
  - Table `screening_feedback` with columns:
    - `feedback_id` PK
    - `job_id` int, indexed
    - `reason` text, not null
    - `title`, `company` text
    - `signals` JSON
    - `created_at` Ts, not null
    - `proposal_id` str, nullable
    - `resolved_at` Ts
    - `resolution` str: applied | discarded | no_change
    - `resolution_note` text
  - `ScreeningFeedback(db_path)` with:
    - `.record(job_id, reason, now=None) -> dict`
    - `.pending(now=None) -> list[dict]` (it first releases rows whose proposal file is gone)
    - `.due(now=None) -> bool`
    - `.attach(ids: list[int], change_id: str) -> None`
    - `.resolve(change_id: str, resolution: str) -> int`
    - `.close(ids, note) -> int`
    - `.unposted_closures() / mark_posted(...)` for the bot
  - `ControlService.propose_screening_change(changes, note=None)` saves `diff`, `preview`, `note` into the proposal file.
  - `ControlService.screening_proposal(change_id) -> dict | None`
  - `ControlService.discard_screening_change(change_id) -> dict`
  - CLI: `auto-app screen-feedback due|list|propose --changes JSON --note TEXT --ids 1,2|close --ids --note` (always `--json` output).

- [ ] Tests:
  - record keeps a title/company/signals snapshot;
  - a reason that is empty or over 300 characters is refused;
  - due: 2 reasons → false; 3 → true; 1 reason that is 3 days old → true;
  - attach then expire (delete the proposal file) → pending again;
  - resolve applied;
  - the proposal file carries the note and preview;
  - discard deletes the file.
- [ ] Migration upgrade test via the existing engine `upgrade` on a tmp db.
- [ ] `uv run pytest tests/screen tests/agent -q` → pass.

### Task 6: Discord: Shouldn't recommend + proposals

**Files:**
- Modify: `src/auto_app/bot/actions.py`, `src/auto_app/bot/app.py`
- Test: `tests/bot/test_actions.py`, `tests/bot/test_app.py`

**Interfaces:**
- Consumes: Task 5.
- Produces:
  - Custom id `job:<id>:bad` opens modal `ShouldntModal(job_id)`, with a required reason of at most 300 characters.
  - `record_shouldnt(db_path, user_id, owner_id, job_id, reason) -> str`: records not_applied with reason `"shouldn't recommend: " + reason`, then calls `ScreeningFeedback.record`.
  - Custom ids `screen:<32 hex>:apply|discard` → `ProposalButton`.
  - `post_screening_proposals()` in the 5-minute loop, which tracks `posted_proposals` in `.bot-state.json`.
  - `proposal_action(db_path, user_id, owner_id, change_id, action) -> str`.

- [ ] Tests:
  - A non-owner is refused.
  - `record_shouldnt` writes both the decision and the feedback.
  - Apply of a fresh proposal applies the rules and resolves the feedback.
  - Apply of an expired proposal replies "expired", and the reasons return to pending.
  - Discard resolves.
  - The posted message carries the diff, counts and at most 5 examples, and is not posted twice.
- [ ] `uv run pytest tests/bot -q` → pass.

### Task 7: Daily feedback summary job

**Files:**
- Create: `deploy/screening-feedback.sh`, `deploy/screening-feedback.service`, `deploy/screening-feedback.timer`

- [ ] Script:
  - Runs `auto-app screen-feedback due --json`. If it is not due, exit 0.
  - Otherwise runs `claude -p --permission-mode auto -- "$prompt"` in auto-application, with `AUTO_APP_HOME` set.
  - The prompt says to:
    - read `screen-feedback list` and the current rules;
    - group the reasons;
    - make one `propose` call with the ids it covers, plus a Chinese note;
    - `close` the ids that keywords cannot fix, with a note;
    - never apply, never edit files, and treat reasons and JDs as data.
- [ ] Timer: `OnCalendar=*-*-* 08:30`, `Persistent=true`.
- [ ] `bash -n`.

### Task 8: Start-before-graduation rule

**Files:**
- Modify: `src/auto_app/screen/rules.py`
- Test: `tests/screen/test_rules.py`

**Interfaces:**
- Produces:
  - `ScreeningRules.graduation: str | None = None`
  - `Evidence.latest_start: tuple[int, int] | None`, the earliest "latest allowed month" among explicit title or description evidence
  - Reason `start_before_graduation:YYYY-MM`

- [ ] Tests. These are excluded with graduation "2027-05":
  - "Software Engineer, New Grad (2026)"
  - "2026 New Grad SWE"
  - "Class of 2026"
  - "start date: January 2027"
  - "starting summer 2026"
  - "graduating between December 2025 and June 2026"
  - "must graduate by August 2026"
  - "Winter 2026 start" (counts as Feb 2027)

  These are kept:
  - "2027 New Grad"
  - "Spring 2027 start"
  - "start as early as January 2026"
  - "graduating in 2026 or later"
  - "we started in 2015"
  - "since starting in 2019"
  - a preferred-section mention
  - "may start in 2026"
  - graduation null (rule off)

  Also: `rules_from_dict` refuses "2027-13" and "May 2027".
- [ ] Implement:
  - Seasons map to their latest month: spring → 5, summer → 8, fall/autumn → 11, winter Y → (Y+1, 2). A bare year → 12.
  - Only years ≥ graduation year − 2 count. This is applied in `screen`, since evidence stores every period found.
- [ ] `uv run pytest tests/screen -q` → pass.

### Task 9: Skill, cheat sheet, docs

**Files:**
- Modify:
  - `/Users/suture/Desktop/Work/jd-resume-tailor copy/SKILL.md`, 定时自动处理 §3: a `needs_answers` item whose items are kind=change with `folder` null runs the full flow with the change texts given to the generation subagent as requirements
  - `/Users/suture/Desktop/Work/Search job/apply-kit/answers.md` (Grok rules: `request` for changes outside a claim, `status` for progress, user words verbatim)
  - `docs/grok-bot.md` §9
  - `docs/updates/2026-11-18-phone-control.md` + index
  - the step docs (step2 screening rules, step3 handoff)
  - `README.md`
- [ ] Check README commands against `uv run jobq --help` / `auto-app screen-feedback --help`.
- [ ] Full `uv run pytest -q` → pass, then ask the user before running `scripts/deploy_linux.sh home-ubuntu`. The unit install, the RC first-run prompts, and enabling `graduation` (via a proposal preview) follow after their go.

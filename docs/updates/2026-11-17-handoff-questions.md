# 2026-11-17 — Handoff: open questions go back through the server, and a filled form can wait

## Why

On 2026-11-16 the Palantir form (#14143) had open questions that the package's `answers.md` did not cover. By the
cheat sheet's rule, Grok stopped and pasted them to the owner. The owner then had to ask Claude Code to write and
republish the answers, and then tell Grok. Nobody passed the message on, and the Mac slept overnight, so the filled
form waited for about 16 hours.

The owner does not want to carry messages between the agents. Claude Code now runs on a schedule on the Mac
(the jd-resume-tailor skill's "定时自动处理"). The server is the one place that both Grok (`jobq`) and a future
ChatGPT Dots client (MCP) can reach, so questions and "answers are published" now go through it.

The other gap is the user's go before Submit. That can come hours after the form was filled, but the 30-minute lease
would put the job back in the queue for another fill.

## Change

- **One check, at the pre-submit list** (the owner's decision the same day). Claude Code publishes the materials
  without waiting, and the owner reviews once, when the agent lists the filled form before Submit. A change the owner
  asks for there goes back through the same channel: an item with `"kind": "change"` carries the owner's words
  verbatim. Claude Code revises, republishes and marks it ready, and the agent fills the form again.
- **New table `handoff_questions`** (migration `e5a1c9d0b7f2`). Each row is one round of questions an agent handed
  back: job, application, agent, the questions, `submitted_at` and `answered_at`.
- **`HandoffService.submit_questions(token, questions)`.**
  - Each item is `{"text", "required", "limit", "page", "kind"}`; only `text` is needed. `kind` is `question`
    (the default) or `change` (the owner's change request, verbatim). Limits: at most 60 questions,
    text of 1–4000 characters, no control characters apart from line breaks and tabs, at most 64 KiB in all.
    Unknown fields are refused, and nothing is dropped silently.
  - It uses up a live claim and records a `needs_input` attempt that names the agent.
  - The application becomes `needs_answers`. The queue leaves the job out with reason `waiting_for_answers`.
- **`pending_questions()`** lists each waiting job's latest unanswered round, oldest first.
- **`answers_ready(job_id)`** is the owner's step, taken once the answers are published.
  - It needs a `want_to_apply` job that is waiting for answers.
  - The application returns to `queued` and the round gets its `answered_at`.
- **`packet` now includes `open_questions`**: the latest round with its `answered_at`, or null. An agent can see
  which questions the new `answers.md` was written for.
- **`hold(token)`**: the form is filled and waits for the user's go.
  - The claim lasts `hold_hours`, a new `handoff.json` key: integer 1–1000, default 24. The application status
    becomes `ready`.
  - The same agent name can claim the held job again and get a fresh token (`"resumed": true`), for example from a
    new chat once the user says "submit". The hold's end is kept, and the old token stops working.
  - Anyone else still sees the job as claimed. When the hold ends, the job returns to the queue.
  - `claim` now always returns `resumed`.
- **`jobq` (Grok)** gains four commands:
  - `questions [TOKEN]`: stdin carries the token line, then the JSON list. With a TOKEN argument, stdin is only the
    JSON.
  - `answers-needed`.
  - `answers-ready ID`: refused when `SUDO_USER` is set, which is how grokbot reaches `jobq`. Claude Code calls it
    over ssh as postedism.
  - `hold [TOKEN]`.
- **MCP handoff tools (Dots, once enabled)** gain `submit_open_questions` and `hold_application`. Marking answers
  ready stays off MCP.
- **The owner's `auto-app handoff` CLI** gains `hold`, `questions TOKEN FILE`, `answers-needed` and `answers-ready`.
- **Mac, jd-resume-tailor `scripts/apply_kit.py`** (outside this repository):
  - `todo` lists `drafts` (queued jobs whose output folder exists but is unpublished) and `needs_answers`.
  - New `answers-ready DIR` checks two things before calling the server: the package was published after the
    questions arrived, and the server lists the job as waiting.
  - Against a server without `answers-needed`, `todo` warns and lists none, so the Mac side works before this is
    deployed.

Compatibility: no existing command, output field or default changed meaning. `claim` adds a field, `packet` adds
`open_questions`, the queue adds the `waiting_for_answers` reason, and the Dot listener gains no tools (handoff tools
stay opt-in and require authentication).

## Tests

- New service tests in `tests/handoff/test_questions.py`, `jobq` tests in `tests/jobq/test_questions_cli.py` and MCP
  tests in `tests/handoff/test_questions_mcp.py`, plus one test for the owner's CLI in `tests/handoff/test_cli.py`.
- apply_kit: 115 tests.

## State

Deployed to home-ubuntu on 2026-11-17 with `scripts/deploy_linux.sh` (rsync mode). Before deploying, the full suite
passed: 2,556 tests. The script took pre-deploy backups of the main and preparation databases, and the schema is now
`e5a1c9d0b7f2` with the `handoff_questions` table. Checks run after the deploy:

- `jobq answers-needed` answers `{"items": []}`.
- `jobq answers-ready` called with `SUDO_USER` set is refused as the owner's step.
- On a job that is not waiting for answers, `jobq answers-ready` is refused.
- The ingest, agent, preparation and worker services and the private tunnel are active.
- The Mac's `apply_kit.py todo` no longer warns.

No question round has been run against a real form yet.

Grok's cheat sheet (`apply-kit/answers.md`, rules 2 and 8) was updated the same day; each new command there falls
back to the old way while `jobq` answers `invalid choice`. A Grok Routine is still for the owner to set up: the
proposed text is in [the Grok guide §9.2](../grok-bot.md#92-grok-routine-的建议提示词). Dot still has no handoff
tools: its private listener has no authentication, and `--handoff-tools` requires it.

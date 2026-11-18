# 2026-11-18 — Claude's questions for the owner on Discord

## Why

The package runner on home-ubuntu stops for decisions only the owner can make: a generic posting, the track, a start
before graduation, an AI ban, a role that is not for new grads. It wrote those questions only into its summary files
on the server. The owner never saw them: six jobs waited a whole day. The owner asked for one Discord channel that
gets every question needing a reply, with its context. After the reply, Claude goes on, and the job then goes to
Grok.

## Change

- **Table `owner_questions`** (migration `c9e5a1b3f4d6`) with `auto_app/handoff/asks.py` (`OwnerQuestions`). A row
  holds the question, its context, up to 4 options, a recommended index, the Discord message, the answer, and the
  outcome (`answered` / `skipped`). It also records when Claude handled the answer, Claude's note, and when that
  note was reported.
  - An identical open question for the same job is not saved twice.
  - An answer kicks the runner.
  - "不投" records `not_applied` at once.
- **jobq (owner only, refused through sudo).** `ask` (a JSON object on stdin), `answered` and
  `question-done ID --note`.
- **Bot.**
  - Optional `AUTO_APP_DISCORD_QUESTIONS_CHANNEL_ID` in `bot.env`; without it, the main channel is used.
  - Each question is posted once as an embed with option buttons, **其他回复…** (a box) and **不投**, all owner only.
    The buttons are removed once an answer is saved.
  - Claude's note is posted as a reply under the question.
  - The five-minute loop does this (`post_owner_questions`), and it is not held back by a pause.
- **Skill.**
  - `apply_kit.py ask`, `question-done` and `skip`. `skip` is only for an owner's explicit "don't apply" in an answer.
  - `todo` lists `answered_questions`.
  - `Jobq.call` can pass stdin.
  - SKILL.md 定时自动处理: §7 asks on Discord with the context, §9 allows `skip` for an owner's "no", and §11 handles
    answers.
  - The runner counts answered questions as work, and its prompt names the `skip` exception.

## Status

Live on home-ubuntu on 2026-11-18, with schema `c9e5a1b3f4d6`. The deployed sources match the tested ones file by
file; the full suite passed with 2,767. The skill is synced, and its tests pass on the server.

- The owner set `AUTO_APP_DISCORD_QUESTIONS_CHANNEL_ID` to #job-tailor and restarted the bot at 14:52.
- Six backlog questions were saved as questions 1–6, for #30226 Databricks, #231 Meyer, #31650 Quantiq,
  #657 / #8237 Notion and #752 InstaLILY.
- #30226's context says that #30271, the conflict it stopped for, has since been taken down.

How-to: [discord-questions-howto.md](../discord-questions-howto.md).

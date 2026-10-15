# Tracking after applying

P3 adds a saved application timeline and conservative mailbox status recognition. It records evidence
reported manually, through handoff, or from a scoped mailbox; it does not submit applications. The
implementation is local. Real mailbox authorization, Linux activation and Discord receipt remain
pending. See [the dated record](updates/2026-10-13-p3-tracking-and-operations.md).

## Timeline

Kinds are `applied`, `assessment` (OA/coding challenge), `interview`, `rejected`, `offer`, and `withdrawn`.
Sources are `manual`, `agent`, `email`, and `system`. Events list by occurrence time then event ID.
Manual status changes to `applied` also write a manual applied event; [handoff](agent-handoff.md)
`submitted` writes an agent applied event atomically with the application/decision. An event alone
does not change the job's manual decision: adding `interview` or `rejected` preserves `applied`.

This includes `job mark --as applied` and the TUI inbox's manual-applied action.
The application, decision and event are atomic; retries do not create another
application/event. Weekly summaries include legacy applied records only when
an existing submission/decision timestamp establishes the occurrence time;
they do not fabricate timeline events or dates. Negated or conditional rejection
sentences are not rejection evidence; genuine "not moving forward" notices still are.

```bash
# Fictional IDs; these commands write to the selected runtime database.
uv run auto-app events add 123 interview --at 2026-10-13T12:00:00Z --note "phone screen"
uv run auto-app events list 123
```

`events` uses `$AUTO_APP_HOME/data/auto_app.db` and emits JSON. Omitted `--at` uses now; naive ISO times
use UTC. The service deduplicates same-job/kind/source events within ±24 hours and separately
deduplicates mail-update IDs. Assessments and interviews from distinct email updates are never merged
by time: one application can have a phone screen and an onsite invitation on the same day. Repeated input returns the existing event without replacing its note.
SQLite writer serialization protects this check across callers.

`get_job` / query job detail returns `events`; the read-only query remains compatible with old
databases, where that list is empty. Base MCP now includes
`record_application_event(job_id, kind, occurred_at=None, note=None)` for manual timeline recording,
alongside `record_application_status`. Refreshing a connected tool schema and successful cloud
readback are separate live checks; local registration does not establish them.

Timeline and mailbox history has no cascading job foreign key. Jobs use SQLite AUTOINCREMENT so
deleted job IDs are never reused for unrelated opportunities. History may outlive an archived or
deleted job; recording or assigning a new event still requires a job currently in the library.

## Mailbox updates

`kind: mailbox` accepts `purpose: jobs` (default) or `purpose: updates`, for Gmail, Outlook and IMAP.
Use separate source entries for recommendation ingestion and application updates. The example
[feeds configuration](../config/feeds.example.yaml) keeps every mailbox disabled. Enabling a source
requires owner-reviewed scope and private credentials; this work did not connect to real mail.

```yaml
application-mail:
  enabled: false
  kind: mailbox
  provider: imap
  purpose: updates
  credentials: ../credentials/imap.json
  folder: INBOX
  senders: [recruiting@example.com]
  lookback_days: 14
  page_size: 50
  max_pages: 4
```

`updates` routes MIME content to deterministic status phrases instead of creating jobs. Concrete
application receipt, rejection, assessment request, interview invitation and offer phrases can be
recognized. Bare status words, promotional subjects, negations and quoted reply history do not
establish an event. Only sender, subject, kind, receipt identity, timestamps and match metadata
persist; the updates path does not retain body text or raw MIME blobs. Malformed MIME is receipted
as unclassified rather than retried every run. Mail is read through existing
read-only API/IMAP operations, without marking it read.
A conditional anywhere before or inside a rejection ("if … not selected") cancels it; a polite modal inside
the rejection sentence ("your skills may fit other roles") does not.

Auto-match considers only jobs whose current decision is `applied`. It requires an exact normalized
organization identity in the From name. Both sides drop legal suffixes (Inc, Corporation, LLC …), a
leading "The", a trailing "via <platform>" and an optional recruiting/careers/jobs/hiring/HR/people/
talent (acquisition) department or "team" — "The NVIDIA Corporation Recruiting Team" matches NVIDIA —
but the remaining words must equal the employer exactly,
or a labelled `Employer:`, `Company:` or `Organization:` field in the subject/first 500 unquoted body
characters. Ordinary prose mentioning a company, prepositions and generic ATS sender domains do not
identify the employer. Exactly one employer must match. If multiple applied jobs exist there, exactly one full normalized
title phrase must match the subject. Ambiguous evidence stays `unmatched` for owner review; it is
never attached by guesswork. Generic ATS mail may deliberately remain unmatched until manual assignment.

Successful mail receipt, update and event share a transaction. Message identity includes provider,
account and message identity (including IMAP UIDVALIDITY); retries do not duplicate history. Purpose
is part of the checkpoint scope, so a changed purpose revisits messages within the bounded lookback.
Changing scope or purpose can therefore rediscover previously seen mail. Explicit manual review:

```bash
uv run auto-app events unmatched
uv run auto-app events assign 7 123
uv run auto-app events dismiss 8
```

`assign` atomically associates an unmatched update and records its email event. `dismiss` affects only
an unmatched update; neither command edits or deletes the email. With `--control-tools`, MCP exposes
`list_unmatched_updates`, `assign_update(update_id, job_id)`, and `dismiss_update(update_id)`.
[Mailbox setup](mailbox-linux-setup.md) and [MCP controls](remote-mcp.md) cover runtime configuration.

## Rolling weekly summary

The default worker includes `summary` and `health`. With a configured webhook, summary sends on the
first due round, then at least seven days after the last acknowledged summary; it is not scheduled
for Monday. It counts events in the preceding seven days, distinct jobs ever recorded as applied,
latest active assessment/interview states, and the eligible handoff queue size when available.
The worker saves `summary_at` before posting and restores it if the post fails, so failed sends
retry next round while a delivered summary is never resent because a later state write failed. No webhook means
a skipped summary. In bot mode keep `--steps screen,enrich,summary,health` and the worker webhook for
summary/health; the bot owns new-job and handoff-outcome delivery. See [operations](operations.md).

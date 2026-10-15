# P3 tracking and operations — 2026-10-13

Local implementation of the approved [P3 plan](../superpowers/plans/2026-10-12-p3-tracking-and-operations.md):
application timelines, conservative mailbox update recognition, unmatched review, rolling weekly
Discord summary, disk health, verified SQLite backup/retention, private backup-pull script,
service-failure hooks and optional Git deployment. Interfaces:
[tracking](../application-tracking.md), [operations](../operations.md).

Events support applied/assessment/interview/rejected/offer/withdrawn, preserving manual/agent/email/
system sources and chronological history. Applied decisions and handoff submissions write events
atomically. Same-kind/source events within ±24 hours and repeated mail receipts are deduplicated.
`purpose: updates` bypasses job ingestion/raw mail storage; body text stays in memory. Concrete
status evidence and unique applied-job matching are required. Exact From organization/department or
labelled employer/company/organization fields establish attribution; prose/prepositions and generic
ATS domains do not. Unmatched status mail deliberately requires manual assignment; ambiguity is reviewed through
`events unmatched/assign/dismiss` or the corresponding opt-in MCP controls. Mailbox examples stay
disabled and no real authorization was performed.

The worker defaults to `screen,enrich,notify,summary,health`. Summary is due seven days after the
last acknowledged delivery, not on a fixed weekday. It reports the preceding seven days, historical
distinct applied jobs, active assessment/interview states and eligible handoff queue size. Health
reports low disk below 2 GiB. In bot mode `screen,enrich,summary,health` retains webhook summary/health
while the bot owns new-job and outcome delivery.

Decisions and costs:

- SQLite job IDs use AUTOINCREMENT so retained event/mail history can never attach to a reused
  deleted ID; IDs remain consumed permanently. Historical rows have no cascading job foreign key.
- Both the main database and optional preparation ledger are backed up. Every new compressed copy
  is restore-verified, exceeding the monthly roadmap check with additional validation time.
- SQLite snapshots include rows only. Preparation attachments, resumes and screenshots need a
  separate artifact policy; a restored ledger alone cannot restore referenced files.
- Retention independently preserves newest, 14 recent daily representatives and 8 older weekly
  representatives for each database. Atomic private publication/manifest prevents partial copies
  from appearing as completed archives.
- Private SSH pull leaves older Mac copies intact; local disk retention is the owner's responsibility.
- Rsync remains the default deployment. Optional Git requires a clean checkout and pre-existing
  remote bare repo; its successful marker follows dependency sync, migration and service checks.
  Failed deployments retain the recorded service state for deliberate recovery.
- Mac TUI, browser/model scheduling and launchd are development tooling; Linux remains the intended
  primary runtime.

Final local verification on 2026-10-13: **1792 passed, 1 warning in 534.05 seconds**
(`.venv/bin/python -m pytest -q --tb=short`). The warning is the existing discord.py `audioop`
deprecation. Independent specification/quality reviews passed, including conservative mail matching,
non-reused job identity, cross-interface atomic writes, notification retry/pagination and private
backup/deployment boundaries. All 388 code/test/script/configuration fingerprints and modes remained
unchanged throughout the final run. Shell syntax and whitespace checks passed.

The initial interrupted run and subsequent complete run exposed old base/preparation MCP tool-list
assertions; the latter was 1789 passed / 3 failed. Those assertions were corrected to exact current
inventories without changing production code or relaxing behavior checks. Both transport variants
now additionally verify event persistence/idempotence. The final result is a fresh complete run,
not combined counts from earlier scopes. Documentation validation also covered 27 CLI help checks,
190 local file links, example parsing, disabled mailbox scopes and private runtime ignore rules.

Real mailbox authorization, timer/service operation, off-host backup/restore, Discord receipt,
seven-day cadence and deployment acceptance remain pending. No deployment, production backup,
network activation, real mail access, external message, application, commit or push was performed.
Changes remain in the existing local checkout for review.

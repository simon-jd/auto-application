# P2/P3 review fixes — 2026-10-13

Fixes for the eight findings of the P1–P3 code review. Interfaces:
[handoff contract](../agent-handoff.md), [tracking](../application-tracking.md).

- **Reported submissions are always recorded.** `record_attempt` no longer re-runs eligibility or the
  daily limit: an outcome reports what already happened, and refusing a real submission left the job
  `want_to_apply` and re-queued it for a second application. Claim and packet still enforce every
  guard before the agent acts. A late `submitted` is accepted while nobody has re-claimed the job.
  `skipped` changes the decision only while the job is still `want_to_apply`; a submission on an
  archived or duplicate job records the applied event without a decision change.
- **Narrower lock-time scans.** Eligibility evidence (screenings, claims, applications, locations,
  postings, source entries) is read only for the judged jobs — every wanted job for `queue`, the one
  job for `claim` and `packet` — and `_usage` reads four job columns instead of whole rows. The
  repost check no longer queries per warning.
- **Local-day quota.** The daily limit uses the host's local calendar day, or an optional IANA
  `timezone` in `handoff.json`; it previously reset at 00:00 UTC (mid-evening in the US).
- **Requeue** refuses a job that is no longer active `want_to_apply`.
- **Employer sender matching** normalizes both sides: legal suffixes, a leading "The", a trailing
  "via <platform>" and a recruiting/careers/jobs/hiring/HR/people/talent department or "team" are
  dropped; the rest must still equal the employer exactly, so prose senders stay unmatched. Stored
  senders now quote display names containing commas or dots, which previously could not be parsed back.
- **Malformed update mail** (for example a missing multipart terminator) is receipted unclassified
  instead of being retried every run as a source issue.
- **Distinct interview/assessment emails** are separate events; only their message identity
  deduplicates them. Other kinds keep the ±24-hour same-source window.
- **Weekly summary** saves `summary_at` before posting and restores it if the post fails, so a later
  state-write failure cannot resend a delivered summary every round.

Tests: updated handoff tests that asserted the old outcome revalidation; new cases for late
submissions, re-claimed tokens, skips after your own decision, requeue, the configured local day,
guard timezone validation, ordinary employer sender names, malformed update mail, same-day interview
emails and summary mark ordering. No deployment, commit or push was performed.

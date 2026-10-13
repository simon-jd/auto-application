# Priority public repository feeds — 2026-10-10

**Step:** 1, 4. **Scope:** SpeedyApply, ApplyGuy, Ricsign and Jobright public repositories; retain Simplify and Canada exclusions. Mailbox authorization is a separate activation step.

## Implementation

`kind: repository` accepts `format: simplify|speedyapply|applyguy|ricsign|jobright`; omitted format remains Simplify. The single-input CLI has `--format` with the same values. Unknown formats fail before opening a database. No runtime dependency or database migration was added.

The example configuration adds eight inputs:

- SpeedyApply SWE and AI: US internships in `README.md`, US new-grad in `NEW_GRAD_USA.md` (four sources).
- ApplyGuy: `data/new-grad-jobs.json`, canonical employer `listingUrl`.
- Ricsign: `data/v1/jobs.json`, stable `uid` and employer `apply_url`.
- Jobright SWE New Grad and Internship: their public `master/README.md` tables (two sources). Data Analysis repositories remain later priority.

JSON feeds retain qualification/source metadata and date precision. Ricsign explicit dead/closed links are inactive; blocked checks and clearance requirements do not establish closure. Markdown feeds use stable job links/IDs, handle company carry-forward, HTML links and locations, and union repeated identity variants. Their status and publication date remain unknown when the tables do not provide explicit evidence.

Relative ages, yearless dates, raw row layout and export/verification clocks are saved under `metadata.source_observation`. Full observation payloads and source revisions update, but these fields do not trigger semantic-change history or reassessment. Stable fields still do. Exact employer identities merge across sources while each observation remains; sparse Jobright detail links cannot be matched to an employer job by title alone.

Country preferences still read `runtime/config/preferences.json` on Linux. Any Canadian location in a merged job excludes that whole job from default lists; original records and history remain available for audit. `San Jose, CA` remains eligible for display. No new recommendation or automatic application behavior was enabled.

## Verification record

Final Ubuntu full regression: **775 passed, 1 skipped in 273.55 seconds**. The skip is the existing web frontend check without a built `web/dist`. Both final fixes were deployed from the tested source; a real normal SIGTERM stop completed in **0.227 seconds**, followed by a successful restart (`active`, `enabled`, worker lock held). The next scheduled run was preserved at `2026-10-11T06:30:46Z`. The post-fix database audit returned the same counts and no integrity, identity or Canada-filter errors.

Local public-snapshot parser probes read eight new feeds with no row issues. Regression checks cover legacy defaults, explicit format/CLI/config selection, stale validator recovery, three-round stable IDs and decisions, exact cross-source merges, distinct requisitions, Canada versus California, and observation-only refreshes.

Eight new feeds were installed alongside the two Simplify feeds on `home-ubuntu`. All ten sources returned `ok` in three real network rounds:

| Round | New source entries | New jobs | Semantic updates |
|---|---:|---:|---:|
| 1 | 4,384 | 2,379 | 0 |
| 2 | 0 | 0 | 0 |
| 3 | 0 | 0 | 0 |

The `2026-10-11T06:00:46Z` audit counted 10,219 jobs and 12,225 postings. Canada excluded 537 jobs, leaving 9,682 visible by default. All jobs are still not evaluated: these totals are ingestion records, not confirmed suitable opportunities. All 7,841 pre-existing posting-to-job identities were preserved. There were no existing manual decisions in production; non-empty decisions and review state were preserved in regression fixtures instead. The audit found no same-source duplicate identities, no exact employer identities split across active jobs, no foreign-key errors, and no excluded jobs leaking into the default list. There are 1,748 jobs observed by multiple sources. Jobright platform URLs without employer identity remain separately discoverable rather than being guessed into title-based matches.

An independent replay of frozen real snapshots into a production database copy also ran three times: 2,379 / 0 / 0 new jobs, with stable identities and semantic-change history after the first replay.

Runtime configuration: `/home/postedism/Desktop/job-search/runtime/config/feeds.yaml`; country preferences: `runtime/config/preferences.json`; database: `runtime/data/auto_app.db`. The user systemd unit is `auto-app-ingest.service`, enabled with a 1,800-second interval. Backups, full test logs, frozen/live round reports, and database audits are under `/home/postedism/Desktop/job-search/checks/priority-repositories-2026-10-10`; non-secret result copies are under the workspace's `reports/2026-10-10-priority-repositories`.

Deployment exposed an old service stop timeout. A deterministic regression demonstrated that nested SIGTERM could deadlock the previous `Event.set()` handler; the replacement handler sets a boolean and checks it at most 0.25 seconds apart while idle. The specific production timeout stack was not captured, so that causal link remains unproved. Final review also found Canada evidence could be lost from non-primary postings during archival; archive snapshots now preserve every posting's raw locations, which both query filtering and screening read. Legacy archives without the new field remain readable; no migration or retrospective backfill was added.

## Mailbox activation

The user confirmed an organizational Microsoft 365 mailbox and only LinkedIn, Jobright and Simplify recommendation emails. OAuth creation was subsequently stopped at the user's request; no OAuth applications, tokens or forwarding rules were created. Mailbox feeds remain disabled. Alternatives under consideration are personal Gmail IMAP with an app password, or Proton Mail Bridge with a paid Mail plan. Generic IMAP is not implemented. Existing `.eml` ingestion remains usable as a manual fallback. See [mailbox setup and alternatives](../mailbox-linux-setup.md).

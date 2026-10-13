# home-ubuntu deployment and repeated checks — 2026-10-10

**Step:** 1, 4  **Status:** repository ingestion service deployed and verified; Gmail/Outlook authorization pending

## Deployment

The user requested `home-ubuntu` and a new desktop `job-search` base directory. The running deployment is:

```text
/home/postedism/Desktop/job-search/
  auto-application/   source snapshot, migrations, uv.lock, Linux .venv
  runtime/           config/feeds.yaml, credentials/, data/auto_app.db
  checks/            source manifest, test logs, isolated validation DBs
```

The source snapshot includes the current uncommitted implementation, verified against a 359-file SHA-256 manifest. It excludes private Mac configuration, tokens, databases, browser profiles and the Mac venv. Ubuntu dependencies were installed with `uv sync --frozen --python 3.12`; the resulting Python is 3.12.13. The source's original local base is `008bf87a47eb8b379216ba5eebe07197a5a6d46e`; this remote source snapshot has no `.git` history. No commit or push was made.

The host already had uv, Google Chrome, a running user systemd manager and `Linger=yes`. Its login shell is fish, so deployment scripts explicitly use `ssh home-ubuntu bash -s`.

## Active service and inputs

`auto-app-ingest.service` is installed in the remote user's `~/.config/systemd/user/`, enabled and active. It runs the feed worker every **1800 seconds after a completed round**, using the runtime config and database. Gmail, Outlook and saved-mail examples remain disabled; the two enabled public sources are Simplify New Grad and Summer 2027 Internships.

```bash
ssh home-ubuntu systemctl --user status auto-app-ingest.service --no-pager
ssh home-ubuntu /home/postedism/Desktop/job-search/auto-application/.venv/bin/auto-app ingest status --db /home/postedism/Desktop/job-search/runtime/data/auto_app.db
ssh home-ubuntu python3 -S /home/postedism/Desktop/job-search/auto-application/scripts/job_view.py --db /home/postedism/Desktop/job-search/runtime/data/auto_app.db context
```

Stop/start or restart the worker using `systemctl --user stop|start|restart auto-app-ingest.service` on the host. Query access remains through the existing read-only CLI/script; no network API was exposed by this deployment. Automated applying remains off.

## Observed verification

- First real import: **36,872 source observations**, including 29,082 inactive upstream records; **7,790 postings**, merged into **7,789 active jobs**, all not evaluated and pending enrichment. New Grad: 19,687 source records / 3,126 new jobs; Internships: 17,185 / 4,663. No parser issues.
- Real import rounds 1–3 succeeded in **25.27s / 12.57s / 12.88s**. Rounds 2 and 3 received unchanged snapshots for both feeds and created **zero** new jobs. Counts, source history and existing job links stayed stable. SQLite integrity and foreign keys passed.
- Three additional isolated CLI rounds passed on Ubuntu: job counts **1 → 1 → 2**, unchanged imports did not duplicate, updates preserved the original job ID, `want_to_apply` and its reason. A deliberately failed first source did not block the healthy source. Source history was `created → updated`; read-only commands did not change logical database contents.
- `systemd-analyze --user verify` returned success. Its only diagnostic concerns an existing unrelated `spice-vdagent.service` unit; the generated ingestion unit had no diagnostic.
- Service activation, actual PID replacement on restart, unchanged last/next run times across restart, worker-lock release on stop, final active/enabled state, and zero consecutive failures were verified. The service is left running at 1800 seconds.
- A separate temporary systemd worker completed **three automatic rounds** at a **2-second test interval**, retaining exactly one job/posting/source entry/change. It shut down cleanly and was collected by systemd. The production service remained active at 1800 seconds throughout; its schedule was not shortened.
- The standard-library-only `python3 -S scripts/job_view.py ... context` command was also executed successfully against the actual runtime database.
- Full Ubuntu regression: **639 passed, 1 skipped in 271.87s**. Tests used an isolated home under `checks/` and installed test browsers there. The existing frontend smoke skips because `web/dist` has not been built. No checks were weakened.

The remote `checks/` directory retains the source checksum manifest, individual import reports, `public-multiround-summary.json`, `cli-validation-summary.json`, `service-validation.json`, `systemd-multiround-summary.json`, the journal and test logs. It contains synthetic test databases separately from the runtime job library.

## Remaining activation

Gmail and Outlook still need the user's OAuth application configuration, login and mailbox scope; follow the [setup guide](../mailbox-linux-setup.md). The successful public-feed checks do not establish mailbox connectivity. JD enrichment and broad feasibility screening remain later implementation work.

Automatic approval rejected an optional transfer of full Git history because it could contain sensitive historical data. That transfer was abandoned; only the approved source snapshot was deployed. No alternate history-transfer route was attempted.

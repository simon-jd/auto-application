# Linux deployment and repeated validation — 2026-10-10

User authorized `ssh home-ubuntu`, a new desktop `job-search` directory, and multiple validation rounds. Resume was explicitly requested after the SSH repair. No commit/push or application submission is part of this task.

## Target and layout

- Host: `home-ubuntu`, user `postedism`.
- Root: `/home/postedism/Desktop/job-search`.
- `auto-application/`: current source snapshot including uncommitted implementation and migrations; Linux venv installed from `uv.lock`.
- `runtime/`: feeds configuration, private credential directory and production job database.
- `checks/`: deployment manifest, logs and isolated validation databases.
- User service: `auto-app-ingest.service`, interval 1800 seconds after each completed round.
- Gmail/Outlook remain disabled until actual OAuth authorization and mailbox scope are configured.

## Execution checklist

- [x] Reconnect and inspect host, desktop location, existing files and user systemd.
- [x] Create the new directory and transfer an archive containing only tracked/unignored project files. Verify every file against its SHA-256 manifest.
- [x] Install locked dependencies in a Linux Python 3.12 environment and run the test suite on the actual target host.
- [x] Run three isolated CLI rounds: first import, duplicate import, changed/new jobs; preserve job identity and manual decisions, verify failure isolation and read-only inspection.
- [x] Ingest both configured public repository feeds repeatedly into the runtime database; inspect revisions, counts, repeated imports and queries.
- [x] Generate/validate/install the user service; confirm running state, persisted next-run time, restart recovery and clean stop/restart. Keep the validated service running at 1800 seconds.
- [x] Record final results and activation boundaries in deployment docs and progress index.

## Observed evidence

- SSH now authenticates. The remote login shell is fish; nontrivial remote commands use `bash -s` explicitly.
- Host reports Linux x86_64, uv 0.12.23, system Python 3.14.4 and Google Chrome 155.0.8059.39. User systemd is running and `Linger=yes`.
- The target directory did not exist before this run. It was created without modifying other desktop projects.
- Initial source archive: 359 files; SHA-256 `3da2c0625423aa0d311fdb98c05a2432f4cb86799a4408fc9acb6b1d24b5b004`. All 359 extracted files matched the source manifest. Later documentation/fix syncs must be recorded separately.
- No private Mac configuration, browser profiles, tokens, databases or Mac venv were transferred.
- Dependencies installed as Python 3.12.13 from the locked project. Full Ubuntu regression: **639 passed, 1 skipped in 271.87s**; web/dist is unbuilt, causing the existing frontend smoke skip.
- Three real public-feed rounds: 25.27s, 12.57s, 12.88s. Runtime contains 7,789 jobs, 7,790 postings and 36,872 observations; later rounds created zero duplicate jobs and preserved IDs. SQLite integrity and foreign keys passed.
- Three isolated CLI rounds: 1 → 1 → 2 jobs, original ID/manual decision retained, failed source isolated, read-only logical database snapshot unchanged.
- Production user service is enabled/active with 1800-second interval. Restart replaced its PID and preserved last/next run; stop released its lock. A separate temporary systemd worker ran three automatic rounds at a 2-second test interval, kept exactly one job and stopped cleanly; production was unchanged.
- Standalone Python `-S` query worked against runtime DB. Mailbox authorization is still pending.
- Automatic approval rejected optional Git-history transfer as potentially containing sensitive historical data. It was abandoned; deployment remains a source snapshot without .git, and the unused local bundle was removed. No workaround, commit or push was performed.
- Detailed results and operating commands: `docs/updates/2026-10-10-linux-deployment.md`.

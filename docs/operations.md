# Linux operations and backups

Linux is the primary runtime; Mac is for development and private backup retrieval. On 2026-10-14,
the source was deployed and main/preparation backups both passed restore verification on Linux.
Backup timer activation and a real backup pull are separate operator actions; Discord remains
disabled pending owner credentials. See the [current deployment record](updates/2026-10-14-review-deployment.md)
and historical [P3 implementation record](updates/2026-10-13-p3-tracking-and-operations.md).

## Verified SQLite backups

`auto-app ops backup` uses SQLite online backup to capture a consistent database while writers run.
It backs up `$AUTO_APP_HOME/data/auto_app.db` and, if present,
`$AUTO_APP_HOME/preparation/preparation.sqlite3`. The optional ledger does not need to exist. Each
new copy passes integrity and restore-schema checks before retention runs; failures exit nonzero.
Main copies are opened by read-only QueryService; preparation copies use a read-only compatible
`preparation_runs` schema/count check, without creating a ledger or running migrations.

Since 2026-11-18 it also archives the **apply-kit folder** next to the runtime (`$AUTO_APP_HOME/../apply-kit`: the
published packages, the frozen copies of submitted jobs' packages and the cheat sheet), when it exists, as
`apply_kit-<UTC>.tar.zst`. Only regular files and folders go in; symlinks are left out and never followed. The archive
gets its manifest entry (sha256, size, file count) and `verify_tree` reads every member back (nothing is
extracted), and it is pruned on its own schedule like each database. `ops verify` takes either kind of archive.

```bash
uv run auto-app ops backup
uv run auto-app ops backup --dest /srv/job-search/runtime/backups --keep-daily 14 --keep-weekly 8
uv run auto-app ops verify /srv/job-search/runtime/backups/auto_app-20261013T033000Z.db.zst
uv run auto-app ops prune --dest /srv/job-search/runtime/backups --keep-daily 14 --keep-weekly 8
```

Archives use `auto_app-YYYYmmddTHHMMSSZ.db.zst` / `preparation-YYYYmmddTHHMMSSZ.db.zst` (UTC).
The directory is mode 0700 and files mode 0600. Complete compressed output is published atomically;
same-second collisions fail without replacing an existing archive. `manifest.jsonl` records path,
SHA-256, bytes, timestamp and quick-check outcome through a private atomic rewrite. Verification
checks corruption/schema compatibility; new archives include a zstd frame checksum, and verification
requires a complete single frame. When a same-directory manifest contains the archive's basename,
verification also checks every matching SHA-256 and optional size (including after off-host copies).
Malformed/conflicting manifest metadata fails closed. Legacy archives without a manifest remain
readable but have no manifest hash proof. The manifest is not an external authenticity signature.

Default retention independently keeps the latest archive for each database, one representative per
day in the last 14 calendar days, and representatives from up to 8 older ISO weeks. This is not a
guarantee of 22 files if backups were missed. Pruning only removes recognized regular archive files;
unrelated files and symlinks are untouched. Backup/prune reject a symlink destination directory.

The main JSON entry includes `verify`; an existing ledger adds `preparation.verify`. Both must have
`ok: true`. Every new compressed backup receives this restore verification, exceeding the roadmap's
monthly check at the cost of additional validation time. There is no separate monthly timer.

These backups include database rows only. Resumes, evidence screenshots, preparation run
PDFs/screenshots/attachments, credentials and profile files require a separate owner-chosen artifact
backup policy. A restored ledger can reference files that are absent. Do not describe a SQLite copy
as a complete backup of runtime files.

For a future restore drill, verify the archive first and restore into an isolated temporary runtime
before inspecting it. A production restore requires all writers stopped, preservation of the current
database, and consistent handling of WAL/SHM sidecars. It is a separate deliberate operator action.

## Nightly timer and private off-host copy

The [backup service](../deploy/auto-app-backup.service) runs `ops backup` with a private umask; the
[timer](../deploy/auto-app-backup.timer) targets 03:30 in the host's local time, with up to ten minutes
of random delay. `Persistent=true` catches up a missed timer. Paths assume
`~/Desktop/job-search/{auto-application,runtime}`; review copied units before installation.

```bash
# Future installation on Linux, after reviewing paths and the alert environment:
mkdir -p ~/.config/systemd/user
cp deploy/auto-app-backup.service deploy/auto-app-backup.timer deploy/auto-app-alert@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now auto-app-backup.timer
systemctl --user list-timers auto-app-backup.timer
```

[pull_backups.sh](../scripts/pull_backups.sh) retrieves archives over the existing private SSH path:

```bash
# Future operation on the Mac; requires the existing SSH host configuration:
scripts/pull_backups.sh home-ubuntu
DEST="$HOME/Backups/job-search" scripts/pull_backups.sh home-ubuntu
```

It copies from `Desktop/job-search/runtime/backups/`, enforces local directory/file permissions
0700/0600 and does not delete older Mac copies. The default destination is `~/Backups/job-search`.
No public backup endpoint or cloud upload is introduced. Local retention of pulled copies remains
the owner's responsibility. An optional Mac launchd retrieval job is development/backup tooling:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.job-search.pull-backups</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string>
    <string>/ABSOLUTE/PATH/auto-application/scripts/pull_backups.sh</string>
    <string>home-ubuntu</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>DEST</key><string>/ABSOLUTE/PATH/Backups/job-search</string>
    <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>5</integer><key>Minute</key><integer>0</integer>
  </dict>
</dict></plist>
```

Replace both absolute-path placeholders before saving a plist in `~/Library/LaunchAgents/` and
loading it with `launchctl bootstrap gui/$(id -u) PATH_TO_PLIST`. No launchd job was installed here.

## Health and service failure alerts

Default worker steps are `screen,enrich,notify,summary,health`; enrichment triggers rescreening when
needed. `health` checks free space on the database filesystem and reports error below **2 GiB**.
Worker step/source failures use the existing webhook alert path with acknowledgment and 12-hour
problem deduplication. Weekly summaries use a rolling seven-day interval and advance their cursor
only after acknowledgment; see [tracking](application-tracking.md).

Service templates and generated ingest units use `OnFailure=auto-app-alert@%n.service`. The alert
template has no recursive failure hook. `ops alert --unit UNIT` sends a minimal unit name/time/local
journal-inspection command; it does not upload journal logs or environment values. Invalid units,
missing webhook and failed delivery exit nonzero. The template loads the private worker environment
from runtime credentials. A deliberate future smoke test will send a real message:

```bash
systemctl --user start auto-app-alert@test.service
```

Alert installation, notification receipt and live service-failure acceptance are still pending.
OnFailure handles a failed unit; it does not prove successful round processing or catch every
graceful service stop. Monitor worker/ingest state separately.

## Deployment modes and recovery

[deploy_linux.sh](../scripts/deploy_linux.sh) retains **rsync as default**, copying the current checkout
including uncommitted files while excluding private runtime/config/artifacts. Optional Git mode
requires a clean checkout and resolves the specified local ref before any remote contact.

```bash
# Future deployment only:
scripts/deploy_linux.sh home-ubuntu
DEPLOY_MODE=git scripts/deploy_linux.sh home-ubuntu release-tag
# One-time remote bare-repository setup is separate and must precede Git mode:
ssh home-ubuntu git init --bare Desktop/job-search/repo.git
```

The script inspects and stops installed ingestion, MCP, preparation, screening, public MCP and bot
units; it remembers which should resume. It takes private online pre-deployment snapshots of the
main database and any existing preparation ledger before updating code. Those snapshots are in
`runtime/data/` and `runtime/preparation/`, outside compressed nightly retention. Git mode pushes the
resolved commit to the remote bare repository's `deploy` branch, checks it out and uses `clean -fd`
with fixed private/runtime exclusions independent of the selected ref's `.gitignore`. Before checkout,
Git mode rejects tracked private/runtime paths in either the incoming tree or current index, and a
config-directory symlink; cleanup exclusions alone cannot protect tracked files. Both modes then sync locked dependencies, run the explicit main DB
migrations and resume only the recorded services, checking their status. Git writes `runtime/.deployed`
with commit/time only after these phases succeed. A failed deployment retains the service snapshot
for recovery; inspect it and the pre-deploy copies before restarting against changed code.

The script updates an existing installation; it does not initialize the bare repo, install/enable
new units, authorize mailboxes, select a public tunnel or set credentials. No rollback or live-host
success is implied by local fake-SSH tests. Additional procedure and live gates:
[Linux worker](linux-worker.md#5-deployment-and-pending-live-acceptance).

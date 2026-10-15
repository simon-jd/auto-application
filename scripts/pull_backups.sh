#!/usr/bin/env bash
# Copy private host backups off-host; never delete older local copies.
set -euo pipefail
BACKUP_HOST="${1:-home-ubuntu}"
BACKUP_DEST="${DEST:-$HOME/Backups/job-search}"
if [[ ! "$BACKUP_HOST" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.@-]*$ ]]; then
  echo 'Expected a plain SSH host.' >&2
  exit 2
fi
mkdir -p "$BACKUP_DEST"
chmod 700 "$BACKUP_DEST"
rsync -a --safe-links --chmod=D700,F600 "$BACKUP_HOST:Desktop/job-search/runtime/backups/" "$BACKUP_DEST/"

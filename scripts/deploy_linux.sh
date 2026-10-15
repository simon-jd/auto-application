#!/usr/bin/env bash
# Update an existing Linux installation. See docs/linux-worker.md §5 before running.
set -euo pipefail

DEPLOY_HOST="${1:-home-ubuntu}"
DEPLOY_BASE="${BASE:-Desktop/job-search}"
if [[ ! "$DEPLOY_HOST" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.@-]*$ ]] ||
   [[ ! "$DEPLOY_BASE" =~ ^/?[a-zA-Z0-9_][a-zA-Z0-9_./-]*$ ]] ||
   [[ "$DEPLOY_BASE" == / || "/$DEPLOY_BASE/" == *'/../'* || "/$DEPLOY_BASE/" == *'/./'* ]]; then
  echo "Expected a plain SSH host and a BASE path without spaces or traversal." >&2
  exit 2
fi
cd "$(dirname "$0")/.."
DEPLOY_MODE="${DEPLOY_MODE:-rsync}"
DEPLOY_REF="${2:-HEAD}"
DEPLOY_COMMIT="none"
if [[ "$DEPLOY_MODE" == git ]]; then
  # All local validation precedes the first remote contact.
  if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
    echo "Refusing to deploy: the working tree has uncommitted changes (commit or stash them first)." >&2
    exit 2
  fi
  if [[ ! "$DEPLOY_REF" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_./@{}~^+-]*$ ]]; then
    echo "Expected a plain git ref without shell syntax." >&2
    exit 2
  fi
  DEPLOY_COMMIT="$(git rev-parse --verify --end-of-options "$DEPLOY_REF^{commit}")"
  [[ "$DEPLOY_COMMIT" =~ ^[a-f0-9]{40,64}$ ]] || exit 2
elif [[ "$DEPLOY_MODE" != rsync ]]; then
  echo "DEPLOY_MODE must be rsync or git" >&2
  exit 2
fi

# A failed update must not restart services against partly copied code or a failed migration.
trap 'echo "Deployment did not finish; services may remain stopped. Inspect runtime/.deploy-services and the pre-deploy backup before recovery." >&2' ERR

ssh "$DEPLOY_HOST" bash -s -- "$DEPLOY_BASE" prepare <<'REMOTE'
set -euo pipefail
umask 077
DEPLOY_BASE="$1"
[[ "$DEPLOY_BASE" = /* ]] || DEPLOY_BASE="$HOME/$DEPLOY_BASE"
DEPLOY_DB="$DEPLOY_BASE/runtime/data/auto_app.db"
DEPLOY_STATE="$DEPLOY_BASE/runtime/.deploy-services"
test -d "$DEPLOY_BASE/auto-application"
test -f "$DEPLOY_DB"
if test -e "$DEPLOY_STATE"; then
  echo "An earlier deployment needs recovery: $DEPLOY_STATE" >&2
  exit 1
fi
# noclobber also prevents two deployments from replacing each other's service snapshot.
(set -o noclobber; : > "$DEPLOY_STATE")
DEPLOY_SERVICES=()
STOP_SERVICES=()
# The Grok tunnel Requires agent-public: stopping agent-public stops it too, so it must be in the
# snapshot to be started again. Every state is read before anything is stopped.
for service in auto-app-ingest.service auto-app-agent.service auto-app-preparation.service auto-app-worker.service auto-app-agent-public.service auto-app-bot.service job-search-grok-tunnel.service; do
  # is-active returns nonzero for activating/restart-pending units and for inspection errors.
  # Inspect explicitly so neither case can be mistaken for a safely stopped service.
  unit_state="$(systemctl --user show --property=LoadState --property=ActiveState --property=SubState "$service")"
  load_state=""; active_state=""; sub_state=""
  while IFS='=' read -r key value; do
    case "$key" in
      LoadState) load_state="$value" ;;
      ActiveState) active_state="$value" ;;
      SubState) sub_state="$value" ;;
    esac
  done <<< "$unit_state"
  case "$load_state" in
    not-found) continue ;;
    loaded|masked) ;;
    *) echo "Cannot determine service load state: $service" >&2; exit 1 ;;
  esac
  case "$active_state" in
    active|activating|reloading|refreshing|deactivating|inactive|failed|maintenance) ;;
    *) echo "Cannot determine service activity: $service" >&2; exit 1 ;;
  esac
  # Explicit stop also cancels a pending automatic restart. Already-stopping services
  # are drained, but their prior stop intent is preserved instead of restarting them.
  STOP_SERVICES+=("$service")
  if [[ "$active_state" = active || "$active_state" = activating ||
        "$active_state" = reloading || "$active_state" = refreshing || "$sub_state" = auto-restart ]]; then
    DEPLOY_SERVICES+=("$service")
    printf '%s\n' "$service" >> "$DEPLOY_STATE"
  fi
done
if ((${#STOP_SERVICES[@]})); then
  echo "phase: stop"
  systemctl --user stop "${STOP_SERVICES[@]}"
fi
echo "phase: backup"
python3 - "$DEPLOY_DB" "$DEPLOY_BASE/runtime/preparation/preparation.sqlite3" <<'PY'
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

for filename in sys.argv[1:]:
    db = Path(filename)
    if filename != sys.argv[1] and not db.exists():
        continue
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    backup = db.with_name(f"{db.stem}.{stamp}.pre-deploy.db")
    # The shell's private umask applies before SQLite creates this file.
    source = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    destination = sqlite3.connect(backup)
    try:
        source.backup(destination)
        if destination.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("database backup failed integrity check")
    finally:
        destination.close()
        source.close()
    backup.chmod(0o600)
    print("backup:", backup)
PY
REMOTE

if [[ "$DEPLOY_MODE" == git ]]; then
  echo "phase: push"
  git push --force "$DEPLOY_HOST:$DEPLOY_BASE/repo.git" "$DEPLOY_COMMIT:refs/heads/deploy"
  ssh "$DEPLOY_HOST" bash -s -- "$DEPLOY_BASE" "$DEPLOY_COMMIT" checkout <<'REMOTE'
set -euo pipefail
umask 077
DEPLOY_BASE="$1"
DEPLOY_COMMIT="$2"
[[ "$DEPLOY_BASE" = /* ]] || DEPLOY_BASE="$HOME/$DEPLOY_BASE"
test -f "$DEPLOY_BASE/runtime/.deploy-services"
test "$(git --git-dir="$DEPLOY_BASE/repo.git" rev-parse refs/heads/deploy)" = "$DEPLOY_COMMIT"
python3 - "$DEPLOY_BASE" "$DEPLOY_COMMIT" <<'PY'
import fnmatch
from pathlib import Path, PurePosixPath
import subprocess
import sys

base, commit = Path(sys.argv[1]), sys.argv[2]
git = ["git", f"--git-dir={base / 'repo.git'}", f"--work-tree={base / 'auto-application'}"]
private_dirs = {
    ".venv", ".aws", ".codex", ".agents", ".superpowers", "data", "profile", "filters",
    "resumes", "evidence", "preparation", "runtime", "backups", "credentials", "browser-profiles",
}
private_files = [
    ".env", ".env.*", "*.db", "*.db-*", "*.db.zst", "*.sqlite3", "*.sqlite3-*", "*.pem", "*.key",
    "*.worker-state.json", "*.worker-control.json", "*.bot-state.json", "*.worker.lock",
]

def private(path):
    parts = PurePosixPath(path).parts
    if any(part in private_dirs for part in parts):
        return True
    if parts[0] == "config":
        return len(parts) != 2 or not fnmatch.fnmatchcase(parts[1], "*.example.*")
    return any(part != ".env.example" and any(fnmatch.fnmatchcase(part, pattern)
               for pattern in private_files) for part in parts)

# clean exclusions cannot protect paths already tracked by Git. Check both the
# incoming tree and current index before checkout can overwrite or remove them.
if (base / "auto-application/config").is_symlink():
    raise SystemExit("Refusing Git checkout: config is a private/runtime symlink that checkout may replace.")
for arguments in (["ls-tree", "-rz", "--name-only", commit], ["ls-files", "-z"]):
    paths = subprocess.check_output([*git, *arguments]).split(b"\0")
    for raw in paths:
        if raw and private(raw.decode("utf-8", "surrogateescape")):
            raise SystemExit("Refusing Git checkout: a tracked path overlaps private/runtime files; "
                             "move runtime files outside the checkout and remove them from Git first.")

print("phase: checkout", flush=True)
subprocess.run([*git, "checkout", "-f", "deploy"], check=True)
# These fixed exclusions survive rollback to refs with older .gitignore rules.
# Preserve untracked config wholesale; tracked example updates still use checkout.
preserved_dirs = private_dirs | {
    "config", ".git", ".claude", ".idea", ".vscode", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", "build", "dist", "htmlcov", "reports", "out", "logs",
}
excludes = sorted(preserved_dirs) + private_files + [
    "*.pyc", ".DS_Store", "*.egg-info/", ".coverage*",
]
subprocess.run([*git, "clean", "-fd", *(argument for pattern in excludes for argument in ("-e", pattern))],
               check=True)
PY
REMOTE
else
rsync -az --checksum --delete --safe-links \
  --exclude '.git/' --exclude '.venv/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude '.pytest_cache/' --exclude '.mypy_cache/' --exclude '.ruff_cache/' \
  --exclude '.superpowers/' --exclude '.claude/' --exclude '.codex/' --exclude '.agents/' --exclude '.aws/' \
  --exclude '.idea/' --exclude '.vscode/' --exclude '.DS_Store' --exclude 'node_modules/' \
  --exclude 'build/' --exclude 'dist/' --exclude '*.egg-info/' --exclude '.coverage*' --exclude 'htmlcov/' \
  --exclude 'reports/' --exclude 'out/' --exclude 'logs/' --exclude 'backups/' --exclude 'runtime/' \
  --include '.env.example' --exclude '.env' --exclude '.env.*' \
  --exclude 'data/' --exclude '*.db' --exclude '*.db-*' --exclude 'profile/' --exclude 'filters/' \
  --exclude 'resumes/' --exclude 'evidence/' --exclude 'preparation/' \
  --exclude '*.sqlite3' --exclude '*.sqlite3-*' \
  --exclude 'credentials/' --exclude 'browser-profiles/' --exclude '*.pem' --exclude '*.key' \
  --include '/config/' --include '/config/*.example.*' --exclude '/config/*' \
  ./ "$DEPLOY_HOST:$DEPLOY_BASE/auto-application/"

fi

ssh "$DEPLOY_HOST" bash -s -- "$DEPLOY_BASE" "$DEPLOY_COMMIT" finish <<'REMOTE'
set -euo pipefail
umask 077
DEPLOY_BASE="$1"
[[ "$DEPLOY_BASE" = /* ]] || DEPLOY_BASE="$HOME/$DEPLOY_BASE"
DEPLOY_STATE="$DEPLOY_BASE/runtime/.deploy-services"
test -f "$DEPLOY_STATE"
cd "$DEPLOY_BASE/auto-application"
UV_BIN="$(command -v uv || true)"
UV_BIN="${UV_BIN:-$HOME/.local/bin/uv}"
"$UV_BIN" sync --frozen --python 3.12
echo "phase: migrate"
.venv/bin/python - "$DEPLOY_BASE/runtime/data/auto_app.db" <<'PY'
import sys
from pathlib import Path
from auto_app.core.db.engine import make_engine, upgrade

engine = make_engine(Path(sys.argv[1]), configure_journal=False)
try:
    upgrade(engine)
finally:
    engine.dispose()
PY
systemctl --user daemon-reload
DEPLOY_SERVICES=()
while IFS= read -r service; do
  case "$service" in
    auto-app-ingest.service|auto-app-agent.service|auto-app-preparation.service|auto-app-worker.service|auto-app-agent-public.service|auto-app-bot.service|job-search-grok-tunnel.service)
      DEPLOY_SERVICES+=("$service") ;;
    *) echo "Invalid service snapshot; inspect $DEPLOY_STATE" >&2; exit 1 ;;
  esac
done < "$DEPLOY_STATE"
if ((${#DEPLOY_SERVICES[@]})); then
  echo "phase: start"
  systemctl --user start "${DEPLOY_SERVICES[@]}"
  systemctl --user --no-pager --lines=0 status "${DEPLOY_SERVICES[@]}"
fi
rm "$DEPLOY_STATE"
if [[ "$2" != none ]]; then
  # A deployment marker means sync, migration and service resume succeeded.
  DEPLOY_MARKER="$(mktemp "$DEPLOY_BASE/runtime/.deployed.XXXXXX")"
  printf '%s %s\n' "$2" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DEPLOY_MARKER"
  mv -f "$DEPLOY_MARKER" "$DEPLOY_BASE/runtime/.deployed"
fi
REMOTE
trap - ERR

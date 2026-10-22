#!/bin/bash
# Sets up the low-privilege grokbot account for the Grok Bot desktop app on home-ubuntu (docs/grok-bot.md §7).
# Run as root, in two steps:
#   sudo bash /home/postedism/Desktop/job-search/auto-application/deploy/grokbot/setup.sh base
#   (download the Grok Bot Linux .deb, then: sudo apt install ./<file>.deb)
#   sudo bash /home/postedism/Desktop/job-search/auto-application/deploy/grokbot/setup.sh app
# Safe to run again. It stops at the first step that fails and says why. It never changes the postedism account,
# its files or its services, and of the nftables ruleset it only creates or replaces the table inet grokbot_egress.
if [ -z "${BASH_VERSION:-}" ]; then echo "setup.sh: run it with bash: sudo bash $0 base|app" >&2; exit 2; fi
set -Eeuo pipefail
umask 022
PATH=/usr/sbin:/usr/bin:/sbin:/bin
LC_ALL=C
export PATH LC_ALL
unset BASH_ENV ENV

OWNER=postedism
BOT=grokbot
BOT_HOME=/home/grokbot
WRAPPER=/home/postedism/Desktop/job-search/auto-application/deploy/jobq
LAUNCHER=/usr/local/libexec/grokbot-app
RECORDER_DIR=/usr/local/libexec/grokbot-bin
LINK_RECORDER="$RECORDER_DIR/xdg-open"
# The app-*.scope units in "systemctl list-units --type=scope --no-legend --plain" output; a failed unit's line starts
# with a "*" marker.
APP_SCOPES_AWK='{ u = ($1 ~ /\.scope$/) ? $1 : $2 } u ~ /^app-.*\.scope$/ { print u }'
UNITS="grokbot-display.service grokbot-app.service grokbot-vnc.service"

here="$(cd "$(dirname "$0")" && pwd -P)"
cd /
current="start"

die() { printf 'setup.sh: %s\n' "$*" >&2; exit 1; }
step() { current="$1"; printf '\n== %s\n' "$1"; }
# Unchecked failures (set -e) name the step; command substitutions stay quiet so a message is printed once.
trap '[ "${BASH_SUBSHELL:-0}" -gt 0 ] || printf "setup.sh: step \"%s\" failed (line %s); nothing after it was done.\n" "$current" "$LINENO" >&2' ERR

# Run a command as grokbot with a minimal environment. Files inside /home/grokbot are always written this way,
# never by root, so links that grokbot placed there cannot point root's writes elsewhere.
as_bot() { runuser -u "$BOT" -- env -i PATH="$PATH" HOME="$BOT_HOME" LC_ALL=C "$@" </dev/null; }
# bot_write MODE DEST < SOURCE: root opens SOURCE, grokbot writes DEST (through DEST.new, then a rename).
bot_write() {
  runuser -u "$BOT" -- env -i PATH="$PATH" LC_ALL=C \
    sh -c 'umask 077 && cat > "$2.new" && chmod "$1" "$2.new" && mv -f "$2.new" "$2"' sh "$1" "$2"
}
owner_of() { stat -c '%U' -- "$1"; }
# True when neither the group nor others may write the path.
not_shared_writable() { local mode; mode="$(stat -c '%a' -- "$1")" && [ $(( 0$mode & 022 )) -eq 0 ]; }
need_files() { local f; for f in "$@"; do [ -f "$here/$f" ] || die "missing $here/$f"; done; }
# Stops every app-*.scope in grokbot's user manager and prints which: Electron moves the Grok Bot app's main process
# into such a transient scope, outside grokbot-app.service, and an app a link opened (the ChatGPT app) runs in another.
stop_app_scopes() {
  local scopes
  scopes="$(systemctl --user --machine="$BOT@" list-units --type=scope --no-legend --plain 'app-*.scope' |
    awk "$APP_SCOPES_AWK" | tr '\n' ' ')"
  if [ -n "$scopes" ]; then
    systemctl --user --machine="$BOT@" stop 'app-*.scope'
    echo "stopped $BOT's app scopes: ${scopes% }"
  else
    echo "no app scope of $BOT was running"
  fi
}
# True when an AppArmor profile with this name is loaded.
profile_loaded() {
  [ -r /sys/kernel/security/apparmor/profiles ] &&
    awk -v name="$1" '{ sub(/ \([^()]*\)$/, "") } $0 == name { found = 1 } END { exit !found }' \
      /sys/kernel/security/apparmor/profiles
}

base() {
  step "base 1/6: checks"
  [ "$(id -u)" -eq 0 ] || die "run it as root: sudo bash $0 base"
  need_files grokbot-jobq.sudoers jobq-client egress.nft grokbot-egress.service
  id -u "$OWNER" >/dev/null 2>&1 || die "user $OWNER does not exist"
  [ -x /usr/sbin/nft ] || die "/usr/sbin/nft not found: install the nftables package (sudo apt-get install nftables), then run this again"
  [ -x /usr/bin/sudo ] || die "/usr/bin/sudo not found"
  local cmd
  for cmd in useradd usermod passwd loginctl visudo systemctl install apt-get runuser stat getent; do
    command -v "$cmd" >/dev/null 2>&1 || die "command not found: $cmd"
  done
  [ -f "$WRAPPER" ] && [ ! -L "$WRAPPER" ] || die "$WRAPPER is missing or is a symlink: deploy auto-application first (scripts/deploy_linux.sh)"
  [ -x "$WRAPPER" ] || die "$WRAPPER is not executable"
  local path
  for path in "$WRAPPER" "$(dirname "$WRAPPER")"; do
    case "$(owner_of "$path")" in "$OWNER"|root) ;; *) die "$path does not belong to $OWNER or root" ;; esac
    not_shared_writable "$path" || die "$path is writable by its group or by others; as $OWNER run: chmod go-w '$path'"
  done
  local home_mode
  home_mode="$(stat -c '%a' -- "/home/$OWNER")"
  [ $(( 0$home_mode & 007 )) -eq 0 ] ||
    die "/home/$OWNER is open to other users (mode $home_mode); the isolation needs 750 or stricter: chmod 750 /home/$OWNER"
  echo "ok: $WRAPPER"

  step "base 2/6: account $BOT"
  if id -u "$BOT" >/dev/null 2>&1; then
    echo "user $BOT already exists"
  else
    useradd --create-home --user-group --shell /bin/bash "$BOT"
    echo "created user $BOT"
  fi
  local uid
  uid="$(id -u "$BOT")"
  [ "$uid" -ne 0 ] || die "$BOT has UID 0"
  [ "$(getent passwd "$BOT" | cut -d: -f6)" = "$BOT_HOME" ] || die "the home directory of $BOT is not $BOT_HOME"
  [ -d "$BOT_HOME" ] && [ ! -L "$BOT_HOME" ] || die "$BOT_HOME is missing or is a symlink"
  [ "$(owner_of "$BOT_HOME")" = "$BOT" ] || die "$BOT_HOME does not belong to $BOT"
  passwd -l "$BOT" >/dev/null
  usermod -G "" "$BOT"
  chmod 750 "$BOT_HOME"
  # useradd gives new users subordinate UIDs/GIDs. With them, grokbot could run processes under other UIDs
  # (rootless containers sharing the host network), which the egress rule (meta skuid "grokbot") does not
  # cover: remove every range grokbot has.
  local file opt owner start count
  for file in /etc/subuid /etc/subgid; do
    [ -f "$file" ] || continue
    case "$file" in /etc/subuid) opt=--del-subuids ;; *) opt=--del-subgids ;; esac
    while IFS=: read -r owner start count || [ -n "$owner" ]; do
      case "$owner" in "$BOT"|"$uid") ;; *) continue ;; esac
      case "$start" in ''|*[!0-9]*) continue ;; esac  # malformed lines are left to the check below
      case "$count" in ''|*[!0-9]*) continue ;; esac
      usermod "$opt" "$start-$((10#$start + 10#$count - 1))" "$BOT"
    done < "$file"
    ! grep -Eq "^($BOT|$uid):" "$file" || die "$file still lists ranges for $BOT; remove those lines (usermod $opt), then run this again"
  done
  echo "$BOT: UID $uid, password locked, no supplementary groups, no subordinate IDs, $BOT_HOME mode 750"

  step "base 3/6: sudo rule and /usr/local/bin/jobq"
  [ -d /etc/sudoers.d ] || die "/etc/sudoers.d does not exist"
  visudo -cf "$here/grokbot-jobq.sudoers" || die "visudo rejected $here/grokbot-jobq.sudoers"
  # sudo skips file names that contain a dot: the copy only takes effect with the rename.
  install -m 0440 -o root -g root "$here/grokbot-jobq.sudoers" /etc/sudoers.d/.grokbot-jobq.new
  mv -f /etc/sudoers.d/.grokbot-jobq.new /etc/sudoers.d/grokbot-jobq
  install -m 0755 -o root -g root "$here/jobq-client" /usr/local/bin/jobq
  echo "installed /etc/sudoers.d/grokbot-jobq and /usr/local/bin/jobq"

  step "base 4/6: egress firewall (nftables table inet grokbot_egress)"
  nft -c -f "$here/egress.nft" || die "nft rejected $here/egress.nft"
  install -d -m 0755 -o root -g root /etc/grokbot
  install -m 0644 -o root -g root "$here/egress.nft" /etc/grokbot/egress.nft
  install -m 0644 -o root -g root "$here/grokbot-egress.service" /etc/systemd/system/grokbot-egress.service
  local dropin="/etc/systemd/system/user@$uid.service.d"
  install -d -m 0755 -o root -g root "$dropin"
  cat > "$dropin/10-grokbot-egress.conf" <<'EOF'
# Written by deploy/grokbot/setup.sh: without its egress limits, grokbot's user services do not start.
[Unit]
Requires=grokbot-egress.service
After=grokbot-egress.service
EOF
  systemctl daemon-reload
  systemctl enable --now grokbot-egress.service
  # After an earlier run the unit is already active: reload applies the current file (one nft transaction).
  systemctl reload grokbot-egress.service
  nft list table inet grokbot_egress >/dev/null || die "the table inet grokbot_egress is not loaded"
  # Linger starts grokbot's user manager now and at every boot; enabled only once the limits are in place.
  loginctl enable-linger "$BOT"
  systemctl start "user@$uid.service"
  echo "grokbot-egress.service is active; linger is on for $BOT"

  step "base 5/6: packages xvfb and x11vnc"
  # NEEDRESTART_MODE=l: needrestart (if installed) only lists services to restart and restarts none.
  NEEDRESTART_MODE=l apt-get install -y xvfb x11vnc ||
    die "apt-get could not install xvfb and x11vnc (x11vnc is in the universe component); run 'sudo apt-get update' and then this step again"

  step "base 6/6: next"
  cat <<EOF
Base setup is done. Next:
  1. Download the Grok Bot Linux .deb (x64) from the official Grok Bot website.
  2. Install it:  sudo apt install ./<file>.deb
  3. Then run:    sudo bash $here/setup.sh app
EOF
}

# Prints the Exec= value of the [Desktop Entry] group of a .desktop file whose Name= contains "Grok Bot".
desktop_exec() {
  awk '
    /^\[/ { entry = ($0 == "[Desktop Entry]"); next }
    entry && name == "" && /^Name[ \t]*=/ { name = $0; sub(/^Name[ \t]*=[ \t]*/, "", name) }
    entry && cmd == "" && /^Exec[ \t]*=/ { cmd = $0; sub(/^Exec[ \t]*=[ \t]*/, "", cmd) }
    END { if (index(name, "Grok Bot") > 0 && cmd != "") print cmd }
  ' "$1"
}

# Sets exe: the program that the Grok Bot .desktop entry starts, as an absolute path without symlinks.
find_grok_program() {
  local file value prog from=""
  exe=""
  for file in /usr/share/applications/*.desktop; do
    [ -f "$file" ] || continue
    value="$(desktop_exec "$file")"
    [ -n "$value" ] || continue
    case "$value" in
      \"*) prog="${value#\"}"; prog="${prog%%\"*}" ;;  # Exec="/opt/Grok Bot/grok-bot" %U
      *) prog="${value%%[[:space:]]*}" ;;               # Exec=/opt/grok/grok-bot %U, or a name in PATH
    esac
    case "$prog" in
      /*) ;;
      */*) die "$file: Exec= has a relative path" ;;
      *) prog="$(command -v -- "$prog")" || die "$file: the program in Exec= is not in PATH" ;;
    esac
    prog="$(readlink -f -- "$prog")" || die "$file: cannot resolve the program in Exec="
    if [ -z "$exe" ]; then
      exe="$prog"; from="$file"
    elif [ "$prog" != "$exe" ]; then
      die "$from and $file both have a Name with \"Grok Bot\" but start different programs"
    fi
  done
  if [ -z "$exe" ]; then
    local others
    others="$(grep -il '^Name[[:space:]]*=.*grok' /usr/share/applications/*.desktop 2>/dev/null | tr '\n' ' ' || true)"
    die "no .desktop file in /usr/share/applications has a Name containing \"Grok Bot\"; install the .deb first (sudo apt install ./<file>.deb)${others:+. Files that mention grok: $others}"
  fi
  # The path goes into a shell script and is compared with AppArmor profiles: allow only plain characters.
  case "$exe" in
    *[!A-Za-z0-9._+/\ -]*) die "$from: unexpected characters in the program path; allowed are letters, digits, space and ._+-/" ;;
  esac
  # Never act on a shared program such as env or sh: the launcher would start the wrong thing, and the AppArmor
  # step below would change profiles that every user of that program depends on.
  case "$(dirname "$exe")" in
    /bin|/sbin|/usr/bin|/usr/sbin|/usr/local/bin|/usr/local/sbin)
      die "$from starts $exe, a program in a system bin directory; expected the app's own program (for example under /opt)" ;;
  esac
  [ -f "$exe" ] && [ -x "$exe" ] || die "$from: $exe is not an executable file"
  local path
  for path in "$exe" "$(dirname "$exe")"; do
    [ "$(owner_of "$path")" = root ] && not_shared_writable "$path" ||
      die "$path must belong to root and must not be writable by its group or by others"
  done
  echo "Grok Bot program: $exe (from $from)"
}

app() {
  step "app 1/5: checks and the Grok Bot program"
  [ "$(id -u)" -eq 0 ] || die "run it as root: sudo bash $0 app"
  need_files user/grokbot-display.service user/grokbot-app.service user/grokbot-vnc.service userns-profiles.awk \
    grokbot-open-url
  id -u "$BOT" >/dev/null 2>&1 || die "user $BOT does not exist: run setup.sh base first"
  local uid
  uid="$(id -u "$BOT")"
  systemctl is-active --quiet grokbot-egress.service || die "grokbot-egress.service is not active: run setup.sh base first"
  systemctl is-active --quiet "user@$uid.service" ||
    die "the user manager of $BOT (user@$uid.service) is not running: run setup.sh base first"
  local cmd
  for cmd in /usr/bin/Xvfb /usr/bin/x11vnc; do [ -x "$cmd" ] || die "$cmd not found: run setup.sh base first"; done
  command -v openssl >/dev/null 2>&1 || die "command not found: openssl"
  find_grok_program

  step "app 2/5: $LAUNCHER and the link recorder"
  # A link the app (or a command Grok runs) opens is recorded for the owner, never opened: with no defaults for
  # grokbot, the system's opener started an installed app on the hidden display instead (docs/grok-bot.md §7.3).
  # The same recorder under the browser launchers' names, which would otherwise start a browser through alternatives.
  local opener
  install -d -m 0755 -o root -g root /usr/local/libexec "$RECORDER_DIR"
  for opener in xdg-open x-www-browser www-browser sensible-browser; do
    install -m 0755 -o root -g root "$here/grokbot-open-url" "$RECORDER_DIR/$opener.new"
    mv -f "$RECORDER_DIR/$opener.new" "$RECORDER_DIR/$opener"
  done
  cat > "$LAUNCHER.new" <<EOF
#!/bin/sh
# Written by deploy/grokbot/setup.sh app. --no-sandbox: the app gets no user namespaces (docs/grok-bot.md §7.2).
# --password-store=basic: grokbot has no desktop keyring, so the app keeps its secrets in a file.
# PATH and BROWSER: the link recorder comes first, so a link is recorded in the app's journal, not opened (§7.3).
export PATH=/usr/local/libexec/grokbot-bin:/usr/local/bin:/usr/bin:/bin
export BROWSER=/usr/local/libexec/grokbot-bin/xdg-open
exec "$exe" --no-sandbox --password-store=basic "\$@"
EOF
  chown root:root "$LAUNCHER.new"
  chmod 0755 "$LAUNCHER.new"
  mv -f "$LAUNCHER.new" "$LAUNCHER"
  echo "wrote $LAUNCHER, and the link recorder as xdg-open, x-www-browser, www-browser and sensible-browser in $RECORDER_DIR"

  step "app 3/5: AppArmor (no user namespaces for the app)"
  # The app runs with --no-sandbox. A profile that allows user namespaces for it would also cover every command that
  # Grok's Shell tool starts, so such profiles are disabled; profiles of other programs are never touched.
  if [ ! -d /etc/apparmor.d ]; then
    echo "no /etc/apparmor.d (AppArmor is not installed): nothing to do"
  else
    command -v apparmor_parser >/dev/null 2>&1 || die "command not found: apparmor_parser"
    # The userns profile that an earlier version of this script wrote: unload it, then delete it.
    local old=/etc/apparmor.d/grokbot-electron
    if [ -e "$old" ]; then
      ! profile_loaded grokbot-electron || apparmor_parser -R "$old"
      rm -f "$old"
      [ ! -L /etc/apparmor.d/disable/grokbot-electron ] || rm -f /etc/apparmor.d/disable/grokbot-electron
      echo "removed $old, written by an earlier version of setup.sh"
    elif profile_loaded grokbot-electron; then
      printf '%s' grokbot-electron > /sys/kernel/security/apparmor/.remove
      echo "unloaded the profile grokbot-electron, written by an earlier version of setup.sh"
    fi
    # Profiles that attach to the app's program and allow user namespaces, for example one the .deb installed.
    local file base names name loaded status found=""
    for file in /etc/apparmor.d/*; do
      [ -f "$file" ] || continue
      base="$(basename "$file")"
      case "$base" in *.dpkg-*|*~|*.orig|*.rej|*.rpmnew|*.rpmsave) continue ;; esac
      names="$(awk -v prog="$exe" -f "$here/userns-profiles.awk" "$file")" && status=0 || status=$?
      [ "$status" -ne 1 ] || continue  # 1: no such profile in this file
      [ "$status" -eq 0 ] || die "could not read $file with $here/userns-profiles.awk (status $status)"
      found=1
      if [ ! -e "/etc/apparmor.d/disable/$base" ] && [ ! -L "/etc/apparmor.d/disable/$base" ]; then
        install -d -m 0755 -o root -g root /etc/apparmor.d/disable
        ln -s "$file" "/etc/apparmor.d/disable/$base"
      fi
      loaded=""
      while IFS= read -r name; do
        [ -z "$name" ] || ! profile_loaded "$name" || loaded=1
      done <<< "$names"
      if [ -n "$loaded" ]; then
        apparmor_parser -R "$file" || die "apparmor_parser could not unload $file"
        echo "disabled $file (it allowed user namespaces for $exe): linked into /etc/apparmor.d/disable/ and unloaded"
      else
        echo "$file allows user namespaces for $exe and is already disabled"
      fi
    done
    [ -n "$found" ] || echo "no AppArmor profile allows user namespaces for $exe"
  fi

  step "app 4/5: user services, link defaults and the VNC password"
  local unit_dir="$BOT_HOME/.config/systemd/user" vnc_dir="$BOT_HOME/.vnc" unit password="" out
  as_bot mkdir -p "$unit_dir"
  for unit in $UNITS; do
    bot_write 0644 "$unit_dir/$unit" < "$here/user/$unit"
  done
  # Programs that ask the mime defaults instead of PATH (gio open, portals) get the recorder too.
  as_bot mkdir -p "$BOT_HOME/.local/share/applications" "$BOT_HOME/.config"
  bot_write 0644 "$BOT_HOME/.local/share/applications/grokbot-open-url.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Record link for the owner
Exec=/usr/local/libexec/grokbot-bin/xdg-open %u
NoDisplay=true
MimeType=x-scheme-handler/http;x-scheme-handler/https;
EOF
  bot_write 0644 "$BOT_HOME/.config/mimeapps.list" <<'EOF'
[Default Applications]
x-scheme-handler/http=grokbot-open-url.desktop
x-scheme-handler/https=grokbot-open-url.desktop
EOF
  echo "wrote grokbot's link defaults: http and https links go to $LINK_RECORDER"
  as_bot mkdir -p "$vnc_dir"
  as_bot chmod 700 "$vnc_dir"
  if as_bot test -s "$vnc_dir/passwd"; then
    echo "VNC password unchanged (for a new one, delete $vnc_dir/passwd and run setup.sh app again)"
  else
    # VNC passwords are 8 characters long.
    password="$(openssl rand -base64 12 | cut -c 1-8)"
    out="$(as_bot x11vnc -norc -storepasswd "$password" "$vnc_dir/passwd.new" 2>&1)" ||
      die "x11vnc -storepasswd failed: $out"
    as_bot chmod 600 "$vnc_dir/passwd.new"
    as_bot mv -f "$vnc_dir/passwd.new" "$vnc_dir/passwd"
    printf '\nVNC password (shown only this once): %s\n' "$password"
    printf 'Do not paste this line, or any other output of setup.sh app, into a chat (Claude, Grok or any other).\n'
    printf 'Type the password into the VNC viewer yourself. check.sh output holds no secrets and is safe to share.\n\n'
  fi
  systemctl --user --machine="$BOT@" daemon-reload
  # A running app keeps its old PATH: stop it, every app scope (its main process, and apps that links opened), and
  # start it again. An app that is not running is started by enable --now below, after leftover scopes are stopped.
  if systemctl --user --machine="$BOT@" is-active --quiet grokbot-app.service; then
    systemctl --user --machine="$BOT@" stop grokbot-app.service
    stop_app_scopes
    systemctl --user --machine="$BOT@" start grokbot-app.service
    echo "restarted the Grok Bot app: it now records links instead of opening them"
  else
    stop_app_scopes
    echo "grokbot-app.service is not running: enable --now starts it with the link recorder"
  fi
  # shellcheck disable=SC2086 # UNITS is a list of unit names
  systemctl --user --machine="$BOT@" enable --now $UNITS
  # A running x11vnc may keep the old password: restart it after a new one.
  [ -z "$password" ] || systemctl --user --machine="$BOT@" try-restart grokbot-vnc.service
  # shellcheck disable=SC2086
  echo "states ($UNITS): $(systemctl --user --machine="$BOT@" is-active $UNITS | tr '\n' ' ' || true)"

  step "app 5/5: VNC from the Mac"
  cat <<EOF
On the Mac, forward the port (keep this running):
  ssh -N -L 5990:127.0.0.1:5990 home-ubuntu
then open vnc://127.0.0.1:5990 (Finder > Go > Connect to Server, or: open vnc://127.0.0.1:5990)
and sign in to Grok Bot: click its sign-in, then fetch the link on the Mac and open it there (docs/grok-bot.md §7.3):
  ssh home-ubuntu 'journalctl _UID=$uid --since -15min -o cat | sed -n "s/^grokbot-open-url: //p" | LC_ALL=C grep -xE "https://cursor\.com/[A-Za-z0-9._~%?&=/:+-]*" | tail -n 1'
Then check the boundaries:  sudo bash $here/check.sh
Every run of setup.sh app restarts the Grok Bot app if it was running. That ends what Grok had in progress (an open
claim, a half-filled form) and any sign-in in progress: click sign-in again afterwards. The display keeps running,
and so does VNC unless this run made a new VNC password (then VNC was restarted). If their units changed, restart
them with
  sudo systemctl --user --machine=$BOT@ restart grokbot-display.service
and run setup.sh app again.
EOF
}

[ "$#" -eq 1 ] || { echo "usage: sudo bash $0 base|app" >&2; exit 2; }
case "$1" in
  base) base ;;
  app) app ;;
  *) echo "usage: sudo bash $0 base|app" >&2; exit 2 ;;
esac

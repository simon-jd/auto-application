#!/bin/bash
# Acceptance probes for the grokbot account (docs/grok-bot.md §7). Run as root on home-ubuntu:
#   sudo bash /home/postedism/Desktop/job-search/auto-application/deploy/grokbot/check.sh
# What grokbot can do is probed as grokbot (runuser, minimal environment, no terminal input). Root itself only reads:
# grokbot's sudo rights (sudo -n -l -U grokbot), whether the egress table is loaded, whether the runtime database
# exists and whether the runtime holds a profile, resumes or answer bank, which AppArmor profiles are loaded, the app
# scopes in grokbot's user manager, the default route and the listening sockets (ip, ss), and whether each target
# answers root's own connection (TCP, and one HTTPS request over IPv6; root's traffic is not filtered): a target that
# root cannot reach is reported as not tested, never passed.
# Read-only: it installs nothing and changes no settings.
# One line per item: "PASS <name>", "FAIL <name> -- <detail>" or "REPORT <name>: <value>".
# Exit status 1 if any item FAILed. No "set -e": a probe that fails is a result to print, not a reason to stop.
if [ -z "${BASH_VERSION:-}" ]; then echo "check.sh: run it with bash: sudo bash $0" >&2; exit 2; fi
set -uo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
LC_ALL=C
export PATH LC_ALL
unset BASH_ENV ENV

BOT=grokbot
WRAPPER=/home/postedism/Desktop/job-search/auto-application/deploy/jobq
RUNTIME=/home/postedism/Desktop/job-search/runtime
RUNTIME_DB="$RUNTIME/data/auto_app.db"
TRACE=https://www.cloudflare.com/cdn-cgi/trace
# At most this many IPv4 loopback listeners are probed.
SWEEP_CAP=40
# "ADDRESS:PORT" of each IPv4 listener on 127.0.0.0/8 in "ss -H -4 -ltn" output, once, in order: without the DNS stub
# (port 53 on 127.0.0.53 and 127.0.0.54, which rule 2 lets grokbot use) and VNC (port 5990, grokbot's own listener).
LOOPBACK_AWK='
  { a = $4; sub(/%[^:]*:/, ":", a) }
  a !~ /^127\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+$/ || a == "127.0.0.53:53" || a == "127.0.0.54:53" || a ~ /:5990$/ { next }
  !seen[a]++ { print a }'
# The first gateway ("via ADDRESS") of a default route in "ip -4 route show default" output.
GATEWAY_AWK='
  $1 == "default" || $1 == "nexthop" { for (i = 2; i < NF; i++) if ($i == "via") { print $(i + 1); exit } }'
# The app-*.scope units other than Grok Bot's own (app-grok-bot-*) in "systemctl list-units --type=scope --no-legend
# --plain" output, i.e. other GUI apps running as grokbot; a failed unit's line starts with a "*" marker.
OTHER_APP_SCOPES_AWK='{ u = ($1 ~ /\.scope$/) ? $1 : $2 } u ~ /^app-.*\.scope$/ && u !~ /^app-grok-bot-/ { print u }'

failed=0
pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s%s\n' "$1" "${2:+ -- $2}"; failed=$((failed + 1)); }
report() { printf 'REPORT %s: %s\n' "$1" "$2"; }
# Output of programs run as grokbot is shown only as one short line of printable characters.
printable() { tr -cd '[:print:]' | cut -c 1-200; }
# The same for multi-line output: its non-empty lines joined with " | ".
oneline() { awk 'NF { $1 = $1; printf "%s%s", (n++ ? " | " : ""), $0 }' | tr -cd '[:print:]' | cut -c 1-400; }
# Reads the output of "sudo -l -U grokbot". After the "User grokbot may run the following commands on HOST:" line,
# each entry starts on an indented line with "(" (the run-as user); further indented lines continue a wrapped entry.
# Prints "rules N" and then each entry with single spaces, or "unparsable" for anything else.
# (tests/ops/test_grokbot_files.py runs this program on sample outputs.)
SUDO_RULES_AWK='
  !found && /^User grokbot may run the following commands on .*:[ \t]*$/ { found = 1; next }
  !found || /^[ \t]*$/ { next }
  /^[ \t]+\(/ { n++; $1 = $1; rule[n] = $0; next }
  /^[ \t]+[^ \t]/ && n > 0 { $1 = $1; rule[n] = rule[n] " " $0; next }
  { bad = 1 }
  END {
    if (!found || bad || n == 0) { print "unparsable"; exit }
    print "rules " n
    for (i = 1; i <= n; i++) print rule[i]
  }'

as_bot() { runuser -u "$BOT" -- env -i PATH="$PATH" HOME="/home/$BOT" LC_ALL=C "$@" </dev/null; }
# True when the command fails as grokbot.
refused() { ! as_bot "$@" >/dev/null 2>&1; }
# tcp_refused HOST PORT: true when grokbot cannot open a TCP connection to HOST:PORT within 3 seconds.
tcp_refused() { refused timeout 3 bash -c 'exec 3<>"/dev/tcp/$1/$2"' probe "$1" "$2"; }
# tcp_open_as_root HOST PORT: true when root can open it, i.e. something listens there.
tcp_open_as_root() { timeout 3 bash -c 'exec 3<>"/dev/tcp/$1/$2"' probe "$1" "$2" </dev/null >/dev/null 2>&1; }
# probe_tcp HOST PORT LABEL: PASS only when root can connect and grokbot cannot.
probe_tcp() {
  local name="no TCP connection to $1:$2 ($3)"
  case "$1" in *:*) name="no TCP connection to [$1]:$2 ($3)" ;; esac
  if ! tcp_open_as_root "$1" "$2"; then
    report "$name" "not listening, not tested"
  elif tcp_refused "$1" "$2"; then
    pass "$name"
  else
    fail "$name" "connected"
  fi
}
# private_ipv4 ADDRESS: true for a private (10/8, 172.16/12, 192.168/16), CGNAT (100.64/10) or link-local (169.254/16)
# address, which rule 4 of egress.nft rejects.
private_ipv4() {
  case "$1" in
    10.*|192.168.*|169.254.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) return 0 ;;
    100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*) return 0 ;;
  esac
  return 1
}
# probe_gateway ADDRESS: rule 4 on the default gateway. Packets to it leave through the LAN interface, not lo, so
# rule 3 does not stop them. Probes the first of TCP 53, 80 and 443 that root can reach.
probe_gateway() {
  local port
  case "$1" in
    ''|*[!0-9.]*)
      report "no TCP connection to the default gateway" "no IPv4 default gateway (ip -4 route show default), not tested"
      return ;;
  esac
  if ! private_ipv4 "$1"; then
    report "no TCP connection to the default gateway $1" "not a private address, not tested"
    return
  fi
  for port in 53 80 443; do
    if tcp_open_as_root "$1" "$port"; then probe_tcp "$1" "$port" "the default gateway"; return; fi
  done
  report "no TCP connection to $1:53, :80 or :443 (the default gateway)" "not listening, not tested"
}
# sweep_loopback < LISTENERS: the IPv4 loopback listeners (ADDRESS:PORT lines), at most SWEEP_CAP of them: root
# connects first, then grokbot must be refused. Prints one summary line, plus a FAIL line naming what grokbot reached.
sweep_loopback() {
  local name="no TCP connection to any IPv4 loopback listener" target found=0 tested=0 silent=0 reached="" counts
  while read -r target; do
    [ -n "$target" ] || continue
    found=$((found + 1))
    [ "$found" -le "$SWEEP_CAP" ] || continue
    if ! tcp_open_as_root "${target%:*}" "${target##*:}"; then
      silent=$((silent + 1))
    else
      tested=$((tested + 1))
      tcp_refused "${target%:*}" "${target##*:}" || reached="$reached $target"
    fi
  done
  counts="$found listeners, $tested tested, $silent not reachable by root"
  [ "$found" -le "$SWEEP_CAP" ] || counts="$counts, $((found - SWEEP_CAP)) beyond the first $SWEEP_CAP not probed"
  if [ "$found" -eq 0 ]; then
    report "$name" "no listener besides the DNS stub and VNC, not tested"
  elif [ -n "$reached" ]; then
    report "IPv4 loopback listeners" "$counts"
    fail "$name" "grokbot connected to:$reached"
  elif [ "$tested" -gt 0 ]; then
    pass "$name ($counts)"
  else
    report "$name" "$counts, not tested"
  fi
}
# probe_ipv6: rule 5 (all IPv6) on a public IPv6 address. Only when root's own request over IPv6 works must the same
# request as grokbot fail.
probe_ipv6() {
  local name="no IPv6 connection for grokbot (curl -6 $TRACE)" code
  if ! command -v curl >/dev/null 2>&1; then
    report "$name" "curl is not installed, not tested"
    return
  fi
  code="$(curl -q -6 -sS -o /dev/null -w '%{http_code}' --max-time 5 "$TRACE" </dev/null 2>/dev/null | printable)"
  if [ "$code" != 200 ]; then
    report "$name" "no IPv6 route, not tested (root gets HTTP ${code:-none})"
    return
  fi
  code="$(as_bot curl -q -6 -sS -o /dev/null -w '%{http_code}' --max-time 5 "$TRACE" 2>/dev/null | printable)"
  if [ "$code" = 200 ]; then fail "$name" "grokbot gets HTTP 200 over IPv6"; else pass "$name"; fi
}
# no_applicant_data RUNTIME: the server keeps no profile, resumes or answer bank (spec §2); jobq packet would hand
# grokbot a runtime profile's applicant fields. Checked as root.
no_applicant_data() {
  local name="the runtime has no profile/, resumes/ or answer bank" path found=""
  if [ ! -d "$1" ]; then
    fail "$name" "runtime not found: $1"
    return
  fi
  for path in "$1/profile" "$1/resumes" "$1/profile/answers.yaml"; do
    if [ -e "$path" ] || [ -L "$path" ]; then found="$found $path"; fi
  done
  if [ -n "$found" ]; then
    fail "$name" "found:$found (jobq packet would hand grokbot the applicant data; keep it on the Mac)"
  else
    pass "$name"
  fi
}
# True when an AppArmor profile with this name is loaded.
profile_loaded() {
  [ -r /sys/kernel/security/apparmor/profiles ] &&
    awk -v name="$1" '{ sub(/ \([^()]*\)$/, "") } $0 == name { found = 1 } END { exit !found }' \
      /sys/kernel/security/apparmor/profiles
}

[ "$(id -u)" -eq 0 ] || { echo "check.sh: run it as root: sudo bash $0" >&2; exit 2; }
here="$(cd "$(dirname "$0")" && pwd -P)"
cd /
id -u "$BOT" >/dev/null 2>&1 || { fail "account $BOT exists" "run setup.sh base first"; exit 1; }
# Every "refused" result below means something only if commands really run as grokbot.
[ "$(as_bot id -un 2>/dev/null)" = "$BOT" ] || { fail "probes run as $BOT" "runuser -u $BOT failed"; exit 1; }

# jobq works for grokbot, through /usr/local/bin/jobq and sudo, and refuses path arguments.
out="$(as_bot /usr/local/bin/jobq new --limit 1 2>/dev/null)"
code=$?
case "$code:$out" in
  0:*'"jobs"'*) pass "jobq new --limit 1 returns JSON with jobs" ;;
  *) fail "jobq new --limit 1 returns JSON with jobs" "exit $code" ;;
esac
as_bot /usr/local/bin/jobq queue >/dev/null 2>&1
code=$?
if [ "$code" -eq 0 ]; then pass "jobq queue"; else fail "jobq queue" "exit $code"; fi
as_bot /usr/local/bin/jobq new --db x >/dev/null 2>&1
code=$?
if [ "$code" -eq 2 ]; then pass "jobq new --db x is refused with exit 2"; else fail "jobq new --db x is refused with exit 2" "exit $code"; fi

# The owner's files.
if refused ls /home/postedism && refused test -x /home/postedism; then
  pass "grokbot cannot list or enter /home/postedism"
else
  fail "grokbot cannot list or enter /home/postedism"
fi
if [ ! -f "$RUNTIME_DB" ]; then
  fail "grokbot cannot read the runtime database" "runtime database not found: $RUNTIME_DB"
elif refused head -c 1 "$RUNTIME_DB"; then
  pass "grokbot cannot read the runtime database"
else
  fail "grokbot cannot read the runtime database"
fi
no_applicant_data "$RUNTIME"

# sudo: exactly one rule, the wrapper as postedism without a password; nothing else as postedism or root.
# Root lists grokbot's rights (no password needed with sudo-rs, the server's sudo, or with classic sudo).
name="sudo -l -U grokbot lists one NOPASSWD rule: the jobq wrapper as postedism"
listing="$(sudo -n -l -U grokbot 2>/dev/null)"
parsed="$(printf '%s\n' "$listing" | awk "$SUDO_RULES_AWK")"
if [ "$parsed" = "$(printf 'rules 1\n(postedism) NOPASSWD: %s' "$WRAPPER")" ]; then
  pass "$name"
elif [ "$parsed" = unparsable ]; then
  [ -n "$listing" ] || listing="$(sudo -n -l -U grokbot 2>&1 >/dev/null)"
  fail "$name" "unparsable output: $(printf '%s\n' "${listing:-none}" | oneline)"
else
  fail "$name" "$(printf '%s\n' "$parsed" | oneline)"
fi
if refused sudo -n -u postedism id; then pass "sudo -n -u postedism id is refused"; else fail "sudo -n -u postedism id is refused"; fi
if refused sudo -n id; then pass "sudo -n id is refused"; else fail "sudo -n id is refused"; fi

# Container managers would give root.
writable=""
for sock in /run/docker.sock /var/run/docker.sock /var/snap/lxd/common/lxd/unix.socket /var/lib/lxd/unix.socket \
  /var/lib/incus/unix.socket; do
  refused test -w "$sock" || writable="$writable $sock"
done
if [ -z "$writable" ]; then pass "docker and lxd sockets are not writable"; else fail "docker and lxd sockets are not writable" "writable:$writable"; fi

# Network: local services, the LAN, the tailnet and IPv6 are refused; public HTTPS works.
# First a connection that must work, so that the refusals below are not just a broken probe. The host also has
# IPv6 addresses: getting through within 3 seconds shows that refused IPv6 fails fast and IPv4 is used instead.
if tcp_refused www.cloudflare.com 443; then
  fail "TCP probe works: www.cloudflare.com:443 is reachable" "the refusals below prove nothing"
else
  pass "TCP probe works: www.cloudflare.com:443 is reachable"
fi
# Rule 3 (any address of this host; such packets leave through lo): the named local services, every IPv4 loopback
# listener, the host's LAN address and IPv6 loopback.
probe_tcp 127.0.0.1 8767 "auto-app MCP"
probe_tcp 127.0.0.1 8768 "tunnel client"
sweep_loopback <<< "$(ss -H -4 -ltn 2>/dev/null | awk "$LOOPBACK_AWK")"
lan_ip="$(as_bot ip -4 route get 1.1.1.1 2>/dev/null |
  awk '{ for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit } }')"
case "$lan_ip" in ''|*[!0-9.]*) lan_ip="" ;; esac
if [ -n "$lan_ip" ]; then
  probe_tcp "$lan_ip" 6379 "Redis on the LAN address"
  probe_tcp "$lan_ip" 1433 "SQL Server on the LAN address"
else
  fail "no TCP connection to the LAN address" "no src address in: ip -4 route get 1.1.1.1"
fi
# IPv6 loopback: a port that really listens on loopback, for example sshd on [::]:22.
v6_port=""
for port in $(ss -H -6 -ltn 2>/dev/null | awk '{ a = $4 } a ~ /^(\*|\[::\]|\[::1\]):[0-9]+$/ { sub(/.*:/, "", a); print a }' |
              head -n 10); do
  if tcp_open_as_root ::1 "$port"; then v6_port="$port"; break; fi
done
if [ -n "$v6_port" ]; then
  probe_tcp ::1 "$v6_port" "IPv6 loopback"
else
  report "no TCP connection to [::1] (IPv6 loopback)" "no IPv6 listener reachable on [::1], not tested"
fi
# Rule 4 (private, CGNAT and link-local IPv4): targets reached through the LAN or the tailnet interface, which rule 3
# does not cover: the default gateway, Tailscale's DNS resolver and the Mac on the tailnet.
probe_gateway "$(ip -4 route show default 2>/dev/null | awk "$GATEWAY_AWK")"
probe_tcp 100.100.100.100 53 "Tailscale's DNS resolver"
probe_tcp 100.104.170.71 22 "the Mac on the tailnet"
# Rule 5 (all IPv6) on a public address; the [::1] probe above is stopped by rule 3 already.
probe_ipv6
# Public IPv4 matches no rule and is allowed.
if command -v curl >/dev/null 2>&1; then
  http="$(as_bot curl -q -sS --max-time 20 -o /dev/null -w '%{http_code}' "$TRACE" 2>/dev/null | printable)"
  if [ "$http" = 200 ]; then
    pass "public HTTPS works (www.cloudflare.com/cdn-cgi/trace returns 200)"
  else
    dns="$(as_bot awk '$1 == "nameserver" { printf "%s ", $2 }' /etc/resolv.conf 2>/dev/null | printable)"
    fail "public HTTPS works (www.cloudflare.com/cdn-cgi/trace returns 200)" "HTTP ${http:-none}; resolv.conf nameservers: ${dns:-none}"
  fi
else
  fail "public HTTPS works (www.cloudflare.com/cdn-cgi/trace returns 200)" "curl is not installed"
fi
if as_bot systemctl is-active --quiet grokbot-egress.service; then pass "grokbot-egress.service is active"; else fail "grokbot-egress.service is active"; fi
# The unit stays "active" after "nft flush ruleset": check the table itself.
if nft list table inet grokbot_egress >/dev/null 2>&1; then
  pass "nftables table inet grokbot_egress is loaded"
else
  fail "nftables table inet grokbot_egress is loaded" "reload it: sudo systemctl reload grokbot-egress.service"
fi

# The account itself, and VNC on loopback only.
groups="$(as_bot id -Gn 2>/dev/null | printable)"
if [ "$groups" = "$BOT" ]; then pass "grokbot has no supplementary groups"; else fail "grokbot has no supplementary groups" "groups: $groups"; fi
# Subordinate IDs would let grokbot run processes as other UIDs, which the egress rule does not match.
uid="$(id -u "$BOT")"
if as_bot grep -Eq "^($BOT|$uid):" /etc/subuid /etc/subgid 2>/dev/null; then
  fail "grokbot has no subordinate UIDs or GIDs" "see /etc/subuid and /etc/subgid"
else
  pass "grokbot has no subordinate UIDs or GIDs"
fi
mode="$(as_bot stat -c '%a' -- "/home/$BOT" 2>/dev/null | printable)"
if [ "$mode" = 750 ]; then pass "/home/grokbot has mode 750"; else fail "/home/grokbot has mode 750" "mode ${mode:-unknown}"; fi
linger="$(as_bot loginctl show-user "$BOT" --property=Linger --value 2>/dev/null | printable)"
if [ "$linger" = yes ]; then pass "linger is enabled for grokbot"; else fail "linger is enabled for grokbot" "Linger=${linger:-unknown}"; fi
listeners="$(as_bot ss -Hltn 'sport = :5990' 2>/dev/null | awk '{ print $4 }')"
other=""
while read -r addr; do
  case "$addr" in ''|127.0.0.1:5990|'[::1]:5990') ;; *) other="$other $addr" ;; esac
done <<< "$listeners"
if [ -n "$other" ]; then
  fail "VNC port 5990 listens on loopback only" "also on:$(printf '%s' "$other" | printable)"
elif [ -n "$listeners" ]; then
  pass "VNC port 5990 listens on loopback only"
else
  report "VNC port 5990" "nothing listens (setup.sh app not run yet, or grokbot-vnc.service is down)"
fi

# AppArmor: the app runs with --no-sandbox; no loaded profile should allow user namespaces for its program.
name="loaded AppArmor profiles that allow user namespaces for the Grok Bot app"
prog="$(sed -n 's/^exec "\([^"]*\)".*/\1/p' /usr/local/libexec/grokbot-app 2>/dev/null | head -n 1)"
if [ -z "$prog" ]; then
  report "$name" "not checked (no /usr/local/libexec/grokbot-app: setup.sh app has not run)"
elif [ ! -r /sys/kernel/security/apparmor/profiles ]; then
  report "$name" "none (AppArmor is not enabled)"
elif [ ! -f "$here/userns-profiles.awk" ]; then
  report "$name" "not checked (missing $here/userns-profiles.awk)"
else
  found="" unread=""
  # The profile an earlier setup.sh wrote, if it is still loaded but its file is gone.
  [ -e /etc/apparmor.d/grokbot-electron ] || ! profile_loaded grokbot-electron ||
    found=" grokbot-electron (written by an earlier setup.sh)"
  for file in /etc/apparmor.d/*; do
    [ -f "$file" ] || continue
    names="$(awk -v prog="$prog" -f "$here/userns-profiles.awk" "$file" 2>/dev/null)" && status=0 || status=$?
    [ "$status" -ne 1 ] || continue
    if [ "$status" -ne 0 ]; then unread="$unread $file"; continue; fi
    while IFS= read -r p; do
      [ -z "$p" ] || ! profile_loaded "$p" || found="$found $p ($file)"
    done <<< "$names"
  done
  if [ -n "$unread" ]; then
    report "$name" "not checked (could not read:$(printf '%s' "$unread" | printable))"
  else
    report "$name" "$(printf '%s' "${found:- none}" | printable | sed 's/^ //') (for $(printf '%s' "$prog" | printable))"
  fi
fi

# Other GUI apps running as grokbot: each runs in a transient app-*.scope of grokbot's user manager, Grok Bot in
# app-grok-bot-*. One that a link opened before the link recorder existed (the ChatGPT app) would show up here.
name="apps running as grokbot besides Grok Bot (app-*.scope)"
scopes="$(systemctl --user --machine="$BOT@" list-units --type=scope --no-legend --plain 2>/dev/null)" && status=0 ||
  status=$?
if [ "$status" -ne 0 ]; then
  report "$name" "not checked (grokbot's user manager did not answer)"
else
  others="$(printf '%s\n' "$scopes" | awk "$OTHER_APP_SCOPES_AWK" | tr '\n' ' ' | printable)"
  others="${others% }"
  report "$name" "${others:-none}"
fi

# What grokbot can still see (reported, not blocked).
count="$(as_bot ps -ww -u postedism -o args= 2>/dev/null | grep -Eic 'token|secret|password|api[_-]?key')"
report "postedism command lines matching token|secret|password|api[_-]?key (count only)" "${count:-0}"
if command -v tailscale >/dev/null 2>&1; then
  if as_bot timeout 10 tailscale status >/dev/null 2>&1; then
    report "tailscale status as grokbot" "works (device names and tailnet addresses are visible)"
  else
    report "tailscale status as grokbot" "fails"
  fi
else
  report "tailscale status as grokbot" "tailscale is not installed"
fi
if command -v lpstat >/dev/null 2>&1; then
  # CUPS_SERVER: ask the local scheduler, whatever grokbot's ~/.cups/client.conf says.
  lp="$(as_bot env CUPS_SERVER=/run/cups/cups.sock timeout 10 lpstat -r 2>&1 | head -n 1 | printable)"
  report "lpstat -r as grokbot" "${lp:-no output}"
else
  report "lpstat -r as grokbot" "lpstat is not installed"
fi
files=""
for dir in /tmp /var/tmp /dev/shm; do
  n="$(as_bot find "$dir" -xdev -type f -user postedism -perm -004 2>/dev/null | wc -l | tr -d ' ')"
  files="$files $dir=${n:-0}"
done
report "files owned by postedism that others can read" "${files# }"

if [ "$failed" -gt 0 ]; then echo "check.sh: $failed FAIL" >&2; exit 1; fi
echo "check.sh: no FAIL" >&2

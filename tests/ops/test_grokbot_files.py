import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

G = Path(__file__).resolve().parents[2] / "deploy" / "grokbot"
WRAPPER = "/home/postedism/Desktop/job-search/auto-application/deploy/jobq"


def test_sudoers_allows_only_the_jobq_wrapper_as_postedism():
    rules = [l for l in (G / "grokbot-jobq.sudoers").read_text().splitlines() if l and not l.startswith(("#", "Defaults"))]
    assert rules == [f"grokbot ALL=(postedism) NOPASSWD: {WRAPPER}"]
    assert WRAPPER in (G / "jobq-client").read_text() and "sudo -n -u postedism" in (G / "jobq-client").read_text()


def test_egress_rules_order_dns_before_the_loopback_reject_and_refuse_ipv6():
    text = (G / "egress.nft").read_text()
    assert text.index("delete table inet grokbot_egress") < text.index("chain output")
    assert 'meta skuid "grokbot" jump from_grokbot' in text
    chain = text[text.index("chain from_grokbot"):]
    order = ['oifname "lo" tcp sport 5990 ct state established accept', "127.0.0.53, 127.0.0.54",
             'oifname "lo" reject', "100.64.0.0/10", "meta nfproto ipv6 reject"]
    assert [chain.index(s) for s in order] == sorted(chain.index(s) for s in order)
    # Only VNC's replies pass as established: a generic rule would let a grokbot listener answer local clients.
    assert "ct state established,related accept" not in text and chain.count("ct state") == 1
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"):
        assert net in chain


FROM_GROKBOT = [  # spec §6.2 rules 1-5, in order; what no rule matches (public IPv4) meets the policy accept
    'oifname "lo" tcp sport 5990 ct state established accept',
    'ip daddr { 127.0.0.53, 127.0.0.54 } meta l4proto { tcp, udp } th dport 53 accept',
    'oifname "lo" reject with icmpx admin-prohibited',
    'ip daddr { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, '
    '240.0.0.0/4 } reject with icmpx admin-prohibited',
    'meta nfproto ipv6 reject with icmpx admin-prohibited',
]


def test_the_grokbot_chain_is_exactly_these_rules_with_these_actions_in_this_order():
    # check.sh cannot tell every rule apart on every host, so the text itself is pinned: a changed action (for example
    # rule 4 ending in "accept"), a reordered, missing or extra rule all fail here.
    text = (G / "egress.nft").read_text()
    assert text.count("chain from_grokbot {") == 1
    body = text.split("\tchain from_grokbot {\n", 1)[1].split("\n\t}\n", 1)[0]
    assert body.split("\n") == ["\t\t" + rule for rule in FROM_GROKBOT]


def test_user_manager_requires_the_egress_unit_and_app_runs_hidden():
    setup = (G / "setup.sh").read_text()
    assert "Requires=grokbot-egress.service" in setup and "After=grokbot-egress.service" in setup
    assert "--password-store=basic" in setup and 'usermod -G ""' in setup and "visudo -cf" in setup
    assert "-nolisten tcp" in (G / "user" / "grokbot-display.service").read_text()
    vnc = (G / "user" / "grokbot-vnc.service").read_text()
    assert "-localhost" in vnc and "-rfbport 5990" in vnc and "-rfbauth" in vnc
    assert "DISPLAY=:90" in (G / "user" / "grokbot-app.service").read_text()


def test_check_script_probes_every_boundary():
    check = (G / "check.sh").read_text()
    for probe in ("8767", "8768", "6379", "1433", "100.104.170.71", "sudo -n -u postedism", "--db", "REPORT"):
        assert probe in check


@pytest.mark.parametrize("script", ["setup.sh", "check.sh", "jobq-client"])
def test_scripts_parse(script):
    assert subprocess.run(["bash", "-n", str(G / script)]).returncode == 0


def test_sudoers_defaults_parse_under_sudo_rs_and_classic_sudo():
    # The server's active sudo is sudo-rs, whose visudo rejects settings it does not know, such as requiretty.
    text = (G / "grokbot-jobq.sudoers").read_text()
    assert "requiretty" not in text
    assert [line for line in text.splitlines() if line.startswith("Defaults")] == [
        'Defaults:grokbot env_reset, secure_path="/usr/sbin:/usr/bin:/sbin:/bin"']


def sudo_rules(listing):
    program = re.search(r"SUDO_RULES_AWK='([^']*)'", (G / "check.sh").read_text()).group(1)
    return subprocess.run(["awk", program], input=listing, capture_output=True, text=True, check=True).stdout


def test_check_lists_grokbot_sudo_rights_from_root_and_accepts_only_the_wrapper():
    check = (G / "check.sh").read_text()
    assert "sudo -n -l -U grokbot" in check and "as_bot sudo -n -l" not in check
    rule = f"(postedism) NOPASSWD: {WRAPPER}"
    head = "User grokbot may run the following commands on home-ubuntu:\n"
    classic = ("Matching Defaults entries for grokbot on home-ubuntu:\n    env_reset, secure_path=/usr/sbin\\:/usr/bin\n\n"
               + head + f"    {rule}\n")
    assert sudo_rules(classic) == f"rules 1\n{rule}\n"
    assert sudo_rules(head + f"    (postedism) NOPASSWD:\n        {WRAPPER}\n") == f"rules 1\n{rule}\n"  # wrapped
    assert sudo_rules(head + f"    {rule}\n    (root) NOPASSWD: ALL\n").startswith("rules 2\n")
    for unparsable in ("", "User grokbot is not allowed to run sudo on home-ubuntu.\n", head, head + f"{rule}\n",
                       head + f"    {rule}\nsomething else\n"):
        assert sudo_rules(unparsable) == "unparsable\n", unparsable


def test_setup_says_not_to_paste_the_vnc_password_into_a_chat():
    # The plan has the owner paste check.sh output to Claude; the line with the password must never travel that way.
    setup = (G / "setup.sh").read_text()
    shown = setup.index("VNC password (shown only this once)")
    warning = setup[shown:setup.index("fi\n", shown)]
    assert "Do not paste this line, or any other output of setup.sh app, into a chat" in warning
    assert "check.sh output holds no secrets and is safe to share" in warning


def test_app_runs_without_the_chromium_sandbox_and_setup_writes_no_userns_profile():
    setup = (G / "setup.sh").read_text()
    launcher = re.search(r'^exec "\$exe" .*$', setup, re.M).group(0)
    assert "--no-sandbox" in launcher and "--password-store=basic" in launcher
    assert "userns," not in setup and "flags=(unconfined)" not in setup
    assert "/etc/apparmor.d/disable" in setup and "apparmor_parser -R" in setup


@pytest.mark.parametrize("profile, names", [
    ('abi <abi/4.0>,\ninclude <tunables/global>\n\nprofile grok-bot "/opt/Grok Bot/grok-bot" flags=(unconfined) {\n'
     '  userns,\n\n  # Site-specific additions\n  include if exists <local/grok-bot>\n}\n', ["grok-bot"]),
    ('profile grokbot-electron "/opt/Grok Bot/grok-bot" flags=(unconfined) { userns, }\n', ["grokbot-electron"]),
    ('"/opt/Grok Bot/grok-bot" {\n  audit userns create,\n}\n', ["/opt/Grok Bot/grok-bot"]),
    ('profile other /usr/bin/other flags=(unconfined) {\n  "/opt/Grok Bot/grok-bot" ix,\n  userns,\n}\n', []),
    ('profile grok-bot "/opt/Grok Bot/grok-bot" {\n  deny userns,\n  # userns,\n}\n', []),
    ('profile helper "/opt/Grok Bot/grok-bot-helper" flags=(unconfined) {\n  userns,\n}\n', []),
])
def test_userns_profile_detection_only_matches_profiles_of_the_app(tmp_path, profile, names):
    path = tmp_path / "profile"
    path.write_text(profile)
    run = subprocess.run(["awk", "-v", "prog=/opt/Grok Bot/grok-bot", "-f", str(G / "userns-profiles.awk"), str(path)],
                         capture_output=True, text=True)
    assert (run.returncode, run.stdout.splitlines()) == ((0, names) if names else (1, []))


def test_check_passes_only_boundaries_it_exercised():
    check = (G / "check.sh").read_text()
    for probe in ("not listening, not tested", "ss -H -6 -ltn", "runtime database not found",
                  "nft list table inet grokbot_egress", "loaded AppArmor profiles that allow user namespaces"):
        assert probe in check
    assert "probe_tcp ::1 6379" not in check


# ---- check.sh: rule 4 and rule 5 probes, the loopback sweep, no applicant data in the runtime ------------------------
# The functions below are taken from check.sh and run by bash with stand-ins for the commands that need root or
# grokbot: tcp_open_as_root and tcp_refused answer from the lists ROOT_REACHES and BOT_REACHES ("HOST:PORT ..."),
# curl and as_bot from ROOT_CODE and BOT_CODE. Every call is logged to $CALLS.

def check_functions(*names):
    lines = (G / "check.sh").read_text().splitlines()
    found = []
    for name in names:
        start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
        end = start if lines[start].endswith("}") else next(i for i in range(start, len(lines)) if lines[i] == "}")
        found += lines[start:end + 1]
    return "\n".join(found)


def check_setting(name):
    return re.search(rf"^{name}=.*$", (G / "check.sh").read_text(), re.M).group(0)


STAND_INS = r'''
failed=0
listed() { case " $1 " in *" $2 "*) return 0 ;; esac; return 1; }
tcp_open_as_root() { echo "root $1:$2" >> "$CALLS"; listed "$ROOT_REACHES" "$1:$2"; }
tcp_refused() { echo "grokbot $1:$2" >> "$CALLS"; ! listed "$BOT_REACHES" "$1:$2"; }
curl() { echo "root curl $*" >> "$CALLS"; printf '%s' "$ROOT_CODE"; }
as_bot() { echo "grokbot $*" >> "$CALLS"; [ "$1" != curl ] || printf '%s' "$BOT_CODE"; }
'''


BASH = shutil.which("bash")  # an absolute path: the runs below may have no programs on PATH


def run_check(body, *, programs=True):
    """Runs `body` after the check.sh functions it needs; returns (printed lines, failed count, logged calls).

    programs=False runs it with an empty PATH directory, so that no program (curl among them) is found."""
    with tempfile.TemporaryDirectory() as temp:
        calls = Path(temp) / "calls"
        calls.write_text("")
        script = "\n".join([f'CALLS="{calls}"', STAND_INS, check_setting("SWEEP_CAP"), check_setting("TRACE"),
                            check_functions("pass", "fail", "report", "printable", "probe_tcp", "private_ipv4",
                                            "probe_gateway", "sweep_loopback", "probe_ipv6", "no_applicant_data"),
                            body, 'echo "failed=$failed"', 'echo "--- calls"',
                            'while IFS= read -r line; do echo "$line"; done < "$CALLS"'])
        env = {"PATH": "/usr/bin:/bin" if programs else temp, "LC_ALL": "C"}
        result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env, timeout=60)
    assert result.returncode == 0 and not result.stderr, result.stderr
    printed, logged = result.stdout.split("--- calls\n")
    *lines, failed = printed.splitlines()
    return lines, int(failed.removeprefix("failed=")), logged.splitlines()


def sweep(listeners, root="", bot=""):
    targets = "\n".join(listeners)
    return run_check(f'ROOT_REACHES="{root}"; BOT_REACHES="{bot}"\nsweep_loopback <<< "{targets}"')


SWEEP = "no TCP connection to any IPv4 loopback listener"


def test_loopback_sweep_passes_when_grokbot_is_refused_by_every_listener_root_reached():
    listeners = ["127.0.0.1:8767", "127.0.0.1:3001", "127.0.1.1:631"]
    lines, failed, calls = sweep(listeners, root=" ".join(listeners))
    assert lines == [f"PASS {SWEEP} (3 listeners, 3 tested, 0 not reachable by root)"] and failed == 0
    assert calls == [f"{who} {t}" for t in listeners for who in ("root", "grokbot")]  # root first, every time


def test_loopback_sweep_names_every_listener_grokbot_reached_in_one_fail_line():
    listeners = ["127.0.0.1:8767", "127.0.0.1:3001", "127.0.1.1:631"]
    lines, failed, _ = sweep(listeners, root=" ".join(listeners), bot="127.0.0.1:3001 127.0.1.1:631")
    assert lines == ["REPORT IPv4 loopback listeners: 3 listeners, 3 tested, 0 not reachable by root",
                     f"FAIL {SWEEP} -- grokbot connected to: 127.0.0.1:3001 127.0.1.1:631"] and failed == 1


def test_loopback_sweep_never_passes_what_it_did_not_test():
    # Root reaches none: grokbot is not even tried, and its "connections" cannot turn into a PASS or a FAIL.
    lines, failed, calls = sweep(["127.0.0.1:8767", "127.0.0.1:3001"], bot="127.0.0.1:8767 127.0.0.1:3001")
    assert lines == [f"REPORT {SWEEP}: 2 listeners, 0 tested, 2 not reachable by root, not tested"] and failed == 0
    assert calls == ["root 127.0.0.1:8767", "root 127.0.0.1:3001"]
    lines, failed, calls = sweep([])
    assert lines == [f"REPORT {SWEEP}: no listener besides the DNS stub and VNC, not tested"] and calls == []


def test_loopback_sweep_probes_at_most_the_cap():
    assert check_setting("SWEEP_CAP") == "SWEEP_CAP=40"
    listeners = [f"127.0.0.1:{port}" for port in range(3000, 3045)]
    lines, failed, calls = sweep(listeners, root=" ".join(listeners))
    assert lines == [f"PASS {SWEEP} (45 listeners, 40 tested, 0 not reachable by root, 5 beyond the first 40 "
                     "not probed)"] and failed == 0
    assert len(calls) == 80 and "root 127.0.0.1:3040" not in calls


SS_4_LTN = """\
LISTEN 0      4096   127.0.0.53%lo:53        0.0.0.0:*
LISTEN 0      4096      127.0.0.54:53        0.0.0.0:*
LISTEN 0      128        127.0.0.1:8767      0.0.0.0:*
LISTEN 0      128        127.0.0.1:8767      0.0.0.0:*
LISTEN 0      511        127.0.1.1:631       0.0.0.0:*
LISTEN 0      5          127.0.0.1:5990      0.0.0.0:*
LISTEN 0      511          0.0.0.0:6379      0.0.0.0:*
LISTEN 0      10      192.168.1.50:22        0.0.0.0:*
LISTEN 0      10        127.0.0.53:5353      0.0.0.0:*
LISTEN 0      10         127.0.0.1:53        0.0.0.0:*
"""


def test_the_sweep_takes_ipv4_loopback_listeners_but_not_the_dns_stub_or_vnc():
    program = re.search(r"LOOPBACK_AWK='([^']*)'", (G / "check.sh").read_text()).group(1)
    out = subprocess.run(["awk", program], input=SS_4_LTN, capture_output=True, text=True, check=True).stdout
    # 127.0.0.1:53 is not the stub: rule 2 lets grokbot reach port 53 on 127.0.0.53 and 127.0.0.54 only.
    assert out.splitlines() == ["127.0.0.1:8767", "127.0.1.1:631", "127.0.0.53:5353", "127.0.0.1:53"]


@pytest.mark.parametrize("routes, gateway", [
    ("default via 192.168.1.1 dev enp3s0 proto dhcp src 192.168.1.50 metric 100\n"
     "default via 10.0.0.1 dev wlp2s0 proto dhcp metric 600\n", "192.168.1.1"),
    ("default dev wg0 scope link\ndefault via 192.168.1.1 dev eth0\n", "192.168.1.1"),
    ("default nhid 12 via 192.168.0.1 dev eth0 proto static\n", "192.168.0.1"),
    ("default proto static metric 100\n\tnexthop via 192.168.1.1 dev eth0 weight 1\n"
     "\tnexthop via 192.168.2.1 dev eth1 weight 1\n", "192.168.1.1"),
    ("default dev wg0 scope link\n", ""), ("", "")])
def test_the_default_gateway_is_the_first_via_of_a_default_route(routes, gateway):
    program = re.search(r"GATEWAY_AWK='([^']*)'", (G / "check.sh").read_text()).group(1)
    out = subprocess.run(["awk", program], input=routes, capture_output=True, text=True, check=True).stdout
    assert out.splitlines() == ([gateway] if gateway else [])


def test_only_private_cgnat_and_link_local_gateways_are_rule_4_targets():
    inside = ["10.0.0.1", "172.16.0.1", "172.31.255.254", "192.168.1.1", "100.64.0.1", "100.100.100.100",
              "100.127.255.254", "169.254.1.1"]
    outside = ["8.8.8.8", "172.15.0.1", "172.32.0.1", "192.169.0.1", "100.63.0.1", "100.128.0.1", "1.10.0.1",
               "101.64.0.1"]
    body = "for a in " + " ".join(inside + outside) + '; do private_ipv4 "$a" && echo "$a"; done'
    assert run_check(body)[0] == inside


def gateway(address, root="", bot=""):
    return run_check(f'ROOT_REACHES="{root}"; BOT_REACHES="{bot}"\nprobe_gateway "{address}"')


def test_the_gateway_probe_uses_the_first_of_53_80_and_443_that_root_reaches():
    lines, failed, calls = gateway("192.168.1.1", root="192.168.1.1:80 192.168.1.1:443")
    assert lines == ["PASS no TCP connection to 192.168.1.1:80 (the default gateway)"] and failed == 0
    assert calls[:2] == ["root 192.168.1.1:53", "root 192.168.1.1:80"] and calls[-1] == "grokbot 192.168.1.1:80"
    lines, failed, _ = gateway("192.168.1.1", root="192.168.1.1:53 192.168.1.1:443", bot="192.168.1.1:53")
    assert lines == ["FAIL no TCP connection to 192.168.1.1:53 (the default gateway) -- connected"] and failed == 1


def test_the_gateway_probe_reports_what_it_could_not_test():
    lines, failed, calls = gateway("192.168.1.1", bot="192.168.1.1:53")
    assert lines == ["REPORT no TCP connection to 192.168.1.1:53, :80 or :443 (the default gateway): "
                     "not listening, not tested"] and failed == 0
    assert not [c for c in calls if c.startswith("grokbot")]
    for address in ("", "inet6"):
        assert gateway(address)[0] == ["REPORT no TCP connection to the default gateway: no IPv4 default gateway "
                                       "(ip -4 route show default), not tested"]
    lines, _, calls = gateway("203.0.113.1", root="203.0.113.1:53")
    assert lines == ["REPORT no TCP connection to the default gateway 203.0.113.1: not a private address, "
                     "not tested"] and calls == []


IPV6 = "no IPv6 connection for grokbot (curl -6 https://www.cloudflare.com/cdn-cgi/trace)"


def test_the_ipv6_probe_needs_root_to_get_through_over_ipv6_first():
    lines, failed, calls = run_check('ROOT_CODE=200; BOT_CODE=000\nprobe_ipv6')
    assert lines == [f"PASS {IPV6}"] and failed == 0
    assert calls == ["root curl -q -6 -sS -o /dev/null -w %{http_code} --max-time 5 "
                     "https://www.cloudflare.com/cdn-cgi/trace",
                     "grokbot curl -q -6 -sS -o /dev/null -w %{http_code} --max-time 5 "
                     "https://www.cloudflare.com/cdn-cgi/trace"]
    lines, failed, _ = run_check('ROOT_CODE=200; BOT_CODE=200\nprobe_ipv6')
    assert lines == [f"FAIL {IPV6} -- grokbot gets HTTP 200 over IPv6"] and failed == 1
    lines, failed, calls = run_check('ROOT_CODE=000; BOT_CODE=200\nprobe_ipv6')
    assert lines == [f"REPORT {IPV6}: no IPv6 route, not tested (root gets HTTP 000)"] and failed == 0
    assert len(calls) == 1  # grokbot is not tried
    lines, failed, _ = run_check('unset -f curl\nprobe_ipv6', programs=False)
    assert lines == [f"REPORT {IPV6}: curl is not installed, not tested"] and failed == 0


APPLICANT = "the runtime has no profile/, resumes/ or answer bank"


def test_check_fails_when_the_runtime_holds_a_profile_resumes_or_an_answer_bank(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "data").mkdir(parents=True)
    assert run_check(f'no_applicant_data "{runtime}"')[:2] == ([f"PASS {APPLICANT}"], 0)
    (runtime / "resumes").mkdir()
    (runtime / "profile").symlink_to(tmp_path / "nowhere")  # a dangling link counts too
    lines, failed, _ = run_check(f'no_applicant_data "{runtime}"')
    assert lines == [f"FAIL {APPLICANT} -- found: {runtime}/profile {runtime}/resumes (jobq packet would hand "
                     "grokbot the applicant data; keep it on the Mac)"] and failed == 1
    (runtime / "profile").unlink()
    (runtime / "profile").mkdir()
    (runtime / "profile" / "answers.yaml").write_text("why: x\n")
    assert f"{runtime}/profile/answers.yaml" in run_check(f'no_applicant_data "{runtime}"')[0][0]
    lines, failed, _ = run_check(f'no_applicant_data "{tmp_path / "missing"}"')
    assert lines == [f"FAIL {APPLICANT} -- runtime not found: {tmp_path / 'missing'}"] and failed == 1


def test_check_runs_the_new_probes_in_the_main_shell():
    check = (G / "check.sh").read_text()
    for call in ('sweep_loopback <<< "$(ss -H -4 -ltn 2>/dev/null | awk "$LOOPBACK_AWK")"',
                 'probe_gateway "$(ip -4 route show default 2>/dev/null | awk "$GATEWAY_AWK")"',
                 'probe_tcp 100.100.100.100 53 "Tailscale\'s DNS resolver"', "\nprobe_ipv6\n",
                 'no_applicant_data "$RUNTIME"\n', "RUNTIME=/home/postedism/Desktop/job-search/runtime\n"):
        assert call in check, call
    # A function at the end of a pipeline runs in a subshell: its FAIL would not reach the exit status.
    assert "| sweep_loopback" not in check and "| probe_" not in check


# ---- sign-in links from the hidden display: the link recorder ----------------------------------------------------------
# Grok Bot's sign-in opens a link with xdg-open; with no defaults for grokbot, xdg-open started an installed app (the
# ChatGPT app) that the owner could never reach. setup.sh app puts a recorder first on the app's PATH (as xdg-open and
# the browser launchers), in BROWSER and in grokbot's mime defaults: it writes the link to the app's journal.

RECORDER = G / "grokbot-open-url"
LINK = "https://cursor.com/loginDeepControl?challenge=abc&uuid=123&mode=login&redirectTarget=sandbox"
RECORDER_CODE = [
    'if [ "$#" -eq 0 ]; then',
    "  printf 'grokbot-open-url: (no link)\\n' >&2",
    "else",
    "  printf 'grokbot-open-url: %s\\n' \"$1\" >&2",
    "fi",
    "exit 0",
]
LAUNCHER_BODY = (
    "#!/bin/sh\n"
    "# Written by deploy/grokbot/setup.sh app. --no-sandbox: the app gets no user namespaces (docs/grok-bot.md §7.2).\n"
    "# --password-store=basic: grokbot has no desktop keyring, so the app keeps its secrets in a file.\n"
    "# PATH and BROWSER: the link recorder comes first, so a link is recorded in the app's journal, not opened (§7.3).\n"
    "export PATH=/usr/local/libexec/grokbot-bin:/usr/local/bin:/usr/bin:/bin\n"
    "export BROWSER=/usr/local/libexec/grokbot-bin/xdg-open\n"
    'exec "$exe" --no-sandbox --password-store=basic "\\$@"\n')
RECORDER_DESKTOP = ("[Desktop Entry]\n"
                    "Type=Application\n"
                    "Name=Record link for the owner\n"
                    "Exec=/usr/local/libexec/grokbot-bin/xdg-open %u\n"
                    "NoDisplay=true\n"
                    "MimeType=x-scheme-handler/http;x-scheme-handler/https;\n")
RECORDER_MIMEAPPS = ("[Default Applications]\n"
                     "x-scheme-handler/http=grokbot-open-url.desktop\n"
                     "x-scheme-handler/https=grokbot-open-url.desktop\n")
# postedism's login shell on the server is fish, which runs the quoted part of `ssh home-ubuntu '...'`: fish rejects "$"
# followed by a quote, so the command holds no "$" and grep -x anchors the whole line instead of ^...$.
SIGN_IN_COMMAND = ("ssh home-ubuntu 'journalctl _UID=1001 --since -15min -o cat | sed -n \"s/^grokbot-open-url: //p\" | "
                   "LC_ALL=C grep -xE \"https://cursor\\.com/[A-Za-z0-9._~%?&=/:+-]*\" | tail -n 1'")


def test_the_link_recorder_parses_with_sh():
    assert subprocess.run(["/bin/sh", "-n", str(RECORDER)]).returncode == 0


@pytest.mark.parametrize("args, line", [([LINK], f"grokbot-open-url: {LINK}\n"),
                                        ([], "grokbot-open-url: (no link)\n"),
                                        ([LINK, "--extra"], f"grokbot-open-url: {LINK}\n")])
def test_the_link_recorder_writes_one_line_to_stderr_and_exits_0(tmp_path, args, line):
    # PATH is an empty directory: the recorder needs no program at all, so it cannot start one.
    run = subprocess.run(["/bin/sh", str(RECORDER), *args], cwd=tmp_path, env={"PATH": str(tmp_path)},
                         capture_output=True, text=True)
    assert (run.returncode, run.stdout, run.stderr) == (0, "", line)
    assert list(tmp_path.iterdir()) == []


def test_the_link_recorder_is_exactly_these_lines():
    # Pinned in full: an added line that starts a program (even in the background) changes this list.
    text = RECORDER.read_text()
    assert text.startswith("#!/bin/sh\n")
    assert [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")] == RECORDER_CODE


def setup_heredoc(opening, after=""):
    """The body of the first heredoc in setup.sh whose opening line contains `opening`, after the text `after`."""
    setup = (G / "setup.sh").read_text()
    body = setup.index("\n", setup.index(opening, setup.index(after))) + 1
    return setup[body:setup.index("\nEOF\n", body) + 1]


def test_the_launcher_is_exactly_this_script():
    assert setup_heredoc('cat > "$LAUNCHER.new" <<EOF') == LAUNCHER_BODY


def test_setup_installs_the_recorder_under_every_opener_name():
    setup = (G / "setup.sh").read_text()
    assert "RECORDER_DIR=/usr/local/libexec/grokbot-bin\n" in setup and 'LINK_RECORDER="$RECORDER_DIR/xdg-open"\n' in setup
    install = ('  install -d -m 0755 -o root -g root /usr/local/libexec "$RECORDER_DIR"\n'
               "  for opener in xdg-open x-www-browser www-browser sensible-browser; do\n"
               '    install -m 0755 -o root -g root "$here/grokbot-open-url" "$RECORDER_DIR/$opener.new"\n'
               '    mv -f "$RECORDER_DIR/$opener.new" "$RECORDER_DIR/$opener"\n'
               "  done\n")
    assert install in setup
    assert setup.index(install) < setup.index('mv -f "$LAUNCHER.new" "$LAUNCHER"')


def test_setup_writes_the_mime_defaults_as_grokbot_with_this_exact_content():
    assert setup_heredoc('bot_write 0644 "$BOT_HOME/.local/share/applications/grokbot-open-url.desktop" <<') \
        == RECORDER_DESKTOP
    assert setup_heredoc('bot_write 0644 "$BOT_HOME/.config/mimeapps.list" <<') == RECORDER_MIMEAPPS


def test_a_running_app_is_stopped_with_every_app_scope_and_started_again():
    # Electron moves the app's main process into an app-*.scope outside grokbot-app.service, and an app a link opened
    # (the ChatGPT app) runs in another: restarting the service alone would leave both running.
    lines = (G / "setup.sh").read_text().splitlines()
    start = lines.index('  if systemctl --user --machine="$BOT@" is-active --quiet grokbot-app.service; then')
    middle = lines.index("  else", start)
    end = lines.index("  fi", middle)
    assert lines[start + 1:middle] == [
        '    systemctl --user --machine="$BOT@" stop grokbot-app.service',
        "    stop_app_scopes",
        '    systemctl --user --machine="$BOT@" start grokbot-app.service',
        '    echo "restarted the Grok Bot app: it now records links instead of opening them"']
    assert lines[middle + 1:end] == [
        "    stop_app_scopes",
        '    echo "grokbot-app.service is not running: enable --now starts it with the link recorder"']
    assert lines[end + 1:end + 3] == ["  # shellcheck disable=SC2086 # UNITS is a list of unit names",
                                      '  systemctl --user --machine="$BOT@" enable --now $UNITS']
    function = lines.index("stop_app_scopes() {")
    assert lines[function:lines.index("}", function) + 1] == STOP_APP_SCOPES


STOP_APP_SCOPES = [
    "stop_app_scopes() {",
    "  local scopes",
    "  scopes=\"$(systemctl --user --machine=\"$BOT@\" list-units --type=scope --no-legend --plain 'app-*.scope' |",
    "    awk \"$APP_SCOPES_AWK\" | tr '\\n' ' ')\"",
    '  if [ -n "$scopes" ]; then',
    "    systemctl --user --machine=\"$BOT@\" stop 'app-*.scope'",
    '    echo "stopped $BOT\'s app scopes: ${scopes% }"',
    "  else",
    '    echo "no app scope of $BOT was running"',
    "  fi",
    "}",
]


LIST_SCOPES = """\
app-grok-bot-4053150.scope                loaded active running app-grok-bot-4053150.scope
app-org.chromium.Chromium-4098177.scope   loaded active running app-org.chromium.Chromium-4098177.scope
init.scope                                loaded active running System and Service Manager
* app-foo-12.scope                        loaded failed failed  app-foo-12.scope
"""


@pytest.mark.parametrize("script, variable, scopes", [
    ("setup.sh", "APP_SCOPES_AWK", ["app-grok-bot-4053150.scope", "app-org.chromium.Chromium-4098177.scope",
                                    "app-foo-12.scope"]),
    ("check.sh", "OTHER_APP_SCOPES_AWK", ["app-org.chromium.Chromium-4098177.scope", "app-foo-12.scope"])])
def test_app_scopes_are_read_from_list_units(script, variable, scopes):
    program = re.search(rf"^{variable}='([^']*)'", (G / script).read_text(), re.M).group(1)
    out = subprocess.run(["awk", program], input=LIST_SCOPES, capture_output=True, text=True, check=True).stdout
    assert out.splitlines() == scopes


def test_check_reports_other_apps_running_as_grokbot():
    check = (G / "check.sh").read_text()
    block = check[check.index('name="apps running as grokbot besides Grok Bot (app-*.scope)"'):]
    block = block[:block.index("\nfi\n")]
    assert 'systemctl --user --machine="$BOT@" list-units --type=scope --no-legend --plain' in block
    assert 'awk "$OTHER_APP_SCOPES_AWK"' in block and 'report "$name"' in block and "pass " not in block


def sign_in_commands(source):
    """Every sign-in command (`ssh home-ubuntu 'journalctl ...'`) in the guide, the dated note, or setup.sh's closing
    message as bash prints it (with grokbot's UID 1001)."""
    if source == "setup.sh":
        body = setup_heredoc("cat <<EOF", after='step "app 5/5')
        script = f"uid=1001; BOT=grokbot; here=/srv/deploy/grokbot\ncat <<EOF\n{body}EOF\n"
        text = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
    else:
        root = Path(__file__).resolve().parents[2] / "docs"
        text = (root / {"guide": "grok-bot.md", "note": "updates/2026-10-19-grok-bot-integration.md"}[source]).read_text()
    return re.findall(r"ssh home-ubuntu 'journalctl [^']*'", text)


@pytest.mark.parametrize("source", ["guide", "note", "setup.sh"])
def test_every_copy_of_the_sign_in_command_is_the_ruled_one(source):
    assert sign_in_commands(source) == [SIGN_IN_COMMAND]


@pytest.mark.parametrize("source", ["guide", "note", "setup.sh"])
def test_the_remote_part_of_the_sign_in_command_has_no_dollar_sign(source):
    # fish, postedism's login shell on the server, runs this part; it rejects "$" followed by a quote.
    for command in sign_in_commands(source):
        assert "$" not in command.split("'")[1], command


JOURNAL = ["-- Boot 1234 --", "Started grokbot-app.service.",
           "grokbot-open-url: https://cursor.com.evil.example/x",
           f"grokbot-open-url: {LINK}",
           "grokbot-open-url: https://evil.example/cursor.com/",
           "grokbot-open-url: \x1b]8;;https://evil.example\x1b\\https://cursor.com/x\x1b]8;;\x1b\\",
           "grokbot-open-url: https://cursor.com/x\x1b[0m",
           "grokbot-open-url: (no link)"]


@pytest.mark.parametrize("shell", [["bash", "-c"], [shutil.which("fish"), "--no-config", "-c"]], ids=["bash", "fish"])
def test_the_sign_in_command_keeps_only_a_plain_cursor_link(tmp_path, shell):
    # The remote part exactly as setup.sh prints it, with a journalctl that prints these lines whatever its arguments:
    # only the plain cursor.com link comes out; a lookalike, another host, an OSC 8 hyperlink and a link followed by an
    # escape sequence are dropped.
    if shell[0] is None:
        pytest.skip("fish is not installed")
    remote = sign_in_commands("setup.sh")[0].split("'")[1]
    (tmp_path / "journal").write_text("\n".join(JOURNAL) + "\n")
    stub = tmp_path / "journalctl"
    stub.write_text(f'#!/bin/sh\nexec /bin/cat "{tmp_path / "journal"}"\n')
    stub.chmod(0o755)
    run = subprocess.run([*shell, remote], capture_output=True, text=True, timeout=30,
                         env={"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": str(tmp_path), "LC_ALL": "C"})
    assert (run.returncode, run.stdout, run.stderr) == (0, f"{LINK}\n", "")

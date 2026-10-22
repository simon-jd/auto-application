# Grok Bot local execution: `jobq` and its server wrapper — 2026-10-19

First part of the [Grok Bot integration](../superpowers/specs/2026-10-19-grok-bot-integration-design.md)
([plan](../superpowers/plans/2026-10-19-grok-bot-integration.md); guide, in Chinese: [grok-bot.md](../grok-bot.md)).
The Grok Bot desktop app on home-ubuntu, running as a separate low-privilege `grokbot` account, and Claude Code over
ssh reach auto-app through one fixed-path command, `deploy/jobq`. There is no tunnel, no public listener and no MCP.
The system stays deterministic: it calls no model or agent and submits no application.

**Status: deployed and checked on home-ubuntu, and run end to end on a real job up to the Submit button
(2026-10-21); nothing has been submitted yet.** Built and tested here, then run on the server: the `jobq` command, the
server wrapper, and the install and check scripts for the `grokbot` account (`deploy/grokbot/`); the results are in
[Deployment and live acceptance](#deployment-and-live-acceptance-2026-10-20-to-2026-10-21) and
[Task 7](#task-7-one-real-job-end-to-end-2026-10-21). The Mac apply-kit (its README) and the jd-resume-tailor changes
(`scripts/apply_kit.py` and the skill's workflow) are implemented locally and reviewed too, outside this repository.
The fish-safe sign-in command **is** deployed: the server's `deploy/grokbot/setup.sh` is byte-identical to this tree's
(sha256), and the command it prints runs under postedism's fish with no parse error (checked 2026-10-21). Task 7's five findings are
fixed and deployed ([Task 7's five findings, fixed](#task-7s-five-findings-fixed-2026-10-21)).
Pending: the first actual submission.

- **`jobq`** (`src/auto_app/jobq/cli.py`, console script `auto-app-jobq`). Twelve commands over the existing services:
  `new` (the Discord digest rules, sponsorship first), `seen` (the shared `notified` mark), `search`, `show`,
  `want` / `skip` (the owner's confirmed decision, `--reason` required), and the handoff commands `queue`, `claim`,
  `packet`, `record`, `release` and `account`. Claims are always recorded for the agent `grok-bot`. It accepts no
  `--db`, `--preferences` or other path argument: the runtime is `$AUTO_APP_HOME`, and a missing database is a JSON
  error, never created or migrated. stdout is one JSON object; exit status 0 is success, 1 a failed request
  (`{"status": "error", "error": "…"}`), 2 bad arguments or configuration. The visa signal (`sponsorship`: `offers`,
  `no`, `unknown`) is in `new`, `search` and `show`; `citizenship_required` jobs stay excluded, so they never reach
  `new` or `queue`.
- **Wrapper `deploy/jobq`** (mode 755). It is the only command `grokbot` may run as `postedism` (through sudo, set up by
  `deploy/grokbot/setup.sh`) and the one Claude Code calls over ssh. It finds the repository from its own resolved location, takes the
  sibling `runtime/` as the home, changes into it, forces `AUTO_APP_HOME` and starts
  `.venv/bin/python -I -m auto_app.jobq.cli` with the caller's arguments. The caller's `AUTO_APP_HOME` and `PYTHON*`
  variables, working directory and `sys.path` are therefore not trusted. If `runtime/` or `.venv` is missing it fails
  with a shell error and no JSON.
- **Claim tokens never begin with "-".** `HandoffService.claim` draws again while the token starts with a dash, so a
  token is always safe to pass as a plain command-line argument (`jobq packet TOKEN`, `auto-app handoff packet
  TOKEN`); argparse and Typer would read such a token as an option. The format is unchanged (32 URL-safe characters).
  This also fixes the existing `auto-app handoff` CLI, which had the same 1-in-64 failure; no production claim existed.
- **`grokbot` account scripts (`deploy/grokbot/`), run by the owner with sudo, never by Claude.** `setup.sh base` checks
  the wrapper (regular file, executable, owned by `postedism` or root, neither it nor its directory writable by group
  or others) and that `/home/postedism` is closed to other users; creates `grokbot` (locked password, no
  supplementary groups, home 750); installs the sudoers rule (`visudo -cf` first; Defaults `env_reset` and
  `secure_path` only) and `/usr/local/bin/jobq`; loads the nftables table `inet grokbot_egress` through
  `grokbot-egress.service` (after `nft -c -f`), with a `user@<UID>.service` drop-in so grokbot's services never run
  without it; then enables linger and installs `xvfb` and `x11vnc`. After the owner installs the Grok Bot `.deb`,
  `setup.sh app` reads the program from its `.desktop` entry, writes `/usr/local/libexec/grokbot-app`
  (`--no-sandbox --password-store=basic`), disables any AppArmor profile that allows user namespaces for that program,
  installs three user units (Xvfb `:90`, the app, x11vnc on `127.0.0.1:5990`) and prints a one-time VNC password.
  `check.sh` probes what grokbot can do as `grokbot`; root itself only reads (grokbot's sudo rights, the egress table,
  whether the runtime database exists, the loaded AppArmor profiles, and whether each TCP target accepts root's own
  connection). It prints `PASS`, `FAIL` or `REPORT` lines (exit 1 on any FAIL). Both setup
  steps can be re-run and stop at the first failing step; they never touch the `postedism` account and never flush
  the global ruleset.

Additions to the plan's install steps (the [guide](../grok-bot.md) §4 and §7 describe them):

- **The server's sudo is sudo-rs 0.2.13** (`/usr/bin/sudo` and `/usr/sbin/visudo`; classic sudo 1.9.17p2 is also
  installed as `sudo.ws` / `visudo.ws`), so the sudoers file is written for both: its Defaults line is
  `Defaults:grokbot env_reset, secure_path="/usr/sbin:/usr/bin:/sbin:/bin"`. sudo-rs rejects the plan's
  `!requiretty` as an unknown setting (classic sudo does not require a tty by default anyway); without it, visudo-rs
  and visudo.ws both report "parsed OK" (checked read-only on the server). A test asserts the file never contains
  `requiretty`. `check.sh` reads grokbot's sudo rights as root (`sudo -n -l -U grokbot`, no password needed with
  either sudo) and parses them tolerantly (wrapped lines are joined) but passes only on exactly one rule, the wrapper
  as `postedism` with NOPASSWD; output it cannot parse is a FAIL that shows it on one sanitized line.

- **Egress: only VNC's replies pass as "established"** (review ruling). The first rule of `chain from_grokbot` is
  `oifname "lo" tcp sport 5990 ct state established accept` instead of the plan's `ct state established,related
  accept`. With the generic rule, a grokbot listener on a port a stopped service left free (for example
  `127.0.0.1:8767` during a deployment) could answer the owner's clients, and connections opened while the table was
  missing would stay open. grokbot can still take such a port: clients complete the handshake and send their request,
  which grokbot can read, but its replies are rejected, so the result is a denial of service; the guide lists it as a
  residual.
- **The app runs with `--no-sandbox`; no AppArmor userns profile** (the owner's decision, replacing spec §6.2's
  "profile, not `--no-sandbox`"). A `flags=(unconfined)` profile with `userns` is inherited by the app's children, so
  every command Grok's Shell tool runs could create user namespaces despite
  `kernel.apparmor_restrict_unprivileged_userns = 1`. The Chromium sandbox adds little for an account that can run
  arbitrary commands anyway. `setup.sh app` no longer writes a profile. It links any profile in `/etc/apparmor.d`
  that attaches to the app's program and allows `userns` (for example one the `.deb` ships) into
  `/etc/apparmor.d/disable/` and unloads it. It also unloads and deletes `/etc/apparmor.d/grokbot-electron` from an
  earlier version, and never touches other programs' profiles. Detection is shared with `check.sh` in
  `deploy/grokbot/userns-profiles.awk`; `check.sh` reports any loaded profile that still allows `userns` for the
  program. Re-run `setup.sh app` after installing a new `.deb`, whose install script may load its profile again.
- `grokbot`'s subordinate UID/GID ranges (`/etc/subuid`, `/etc/subgid`, which `useradd` assigns) are removed, and
  `check.sh` verifies it. With them, grokbot could run processes under other UIDs in a rootless container that shares
  the host network, and the egress rule matches only `meta skuid "grokbot"`.
- Linger is enabled at the end of the firewall step, not with the account, so grokbot's user manager first starts with
  the table already loaded.
- `grokbot-egress.service` is ordered like `nftables.service` (`DefaultDependencies=no`, before `network-pre.target`),
  after it and `PartOf=` / `ReloadPropagatedFrom=` it, because its "flush ruleset" would remove the table; it has an
  `ExecReload` (atomic re-load), which re-runs of `setup.sh base` use.
- `setup.sh app` refuses a program in a system bin directory (such as `env` or `sh` from an unusual `Exec=` line):
  the launcher would start the wrong program, and the AppArmor step would change profiles other users depend on.
- The VNC password is 8 characters (the first 8 of `openssl rand -base64 12`), because VNC uses only 8; it is kept on
  re-runs.
- Root never writes inside `/home/grokbot`: the units and the VNC password file are written as `grokbot`, so links
  that grokbot placed there cannot redirect root's writes.
- `check.sh` starts with two self-tests (commands really run as `grokbot`; a TCP connection to
  `www.cloudflare.com:443` works), so its "connection refused" results cannot pass because the probe itself is
  broken, and also checks no supplementary groups, no subordinate IDs, home mode 750, linger, and that port 5990
  listens on loopback only.
- **`check.sh` passes only what it exercised** (review ruling):
  - Each TCP target is first connected to as root, whose traffic is not filtered. A target root cannot reach (not
    listening, the Mac asleep) is `REPORT …: not listening, not tested`, never PASS.
  - The IPv6 probe uses a port that really listens on loopback (the first one from `ss -H -6 -ltn` that root can reach
    through `[::1]`, for example sshd's 22) instead of `[::1]:6379`; Redis listens on IPv4 only.
  - A missing runtime database is a FAIL ("runtime database not found") instead of a vacuous "cannot read".
  - `nft list table inet grokbot_egress` is checked next to the unit's active state, which survives
    `nft flush ruleset`.

Decisions that differ from the plan's table (the [guide](../grok-bot.md) and README follow these values):

- `search --cursor` accepts up to **4096** characters, not 500. A cursor encodes the search's filters, and `search`
  itself returns cursors longer than 500 for long filters (about 1,850 characters for a 200-character CJK query),
  so a 500 cap would have blocked paging through its own output. Three very long CJK filters can still exceed 4096.
- Reason, note, site, email and search text are trimmed, must be 1–500 characters (filters 200, cursor 4096) and reject
  **every Unicode control character** (categories Cc, Zl and Zp: C0 and C1 controls, DEL, U+2028, U+2029), not only
  ord < 32. Format characters such as U+202E and no-break spaces are still accepted.
- Database and OS errors (`sqlite3.Error`, SQLAlchemy errors, `OSError`) are reported as
  `{"status": "error", "error": "database error (OperationalError); retry"}` (or `file error (…)`) with exit 1:
  the exception class only, never SQL, parameters or paths. The "retry" hint fits a locked database; other bugs still
  raise and show a traceback on stderr.
- `seen` checks all IDs with one scan of the jobs table instead of one `DbStore.job()` scan per ID (a scan took about
  31 ms on a 2,000-job database, so 200 IDs would take several seconds), with the same meaning of "known" (archived
  and rule-excluded jobs count). It skips the schema check `QueryService` does for other commands.

Verification on 2026-10-19: `uv run pytest -q tests/jobq tests/ops` **101 passed**; `tests/handoff tests/agent
tests/jobq` **214 passed** (these include the dash-token regression tests). The wrapper test copies `deploy/jobq` into
a directory whose name has spaces, points `runtime` at the test database through a symlink, shadows `auto_app` through
`PYTHONPATH` and sets a wrong `AUTO_APP_HOME`: the wrapper still reads the right database, exits 2 on a usage error and
is executable. A third test checks the working directory, the exported home and that arguments with spaces and shell
characters arrive unchanged. Removing `-I`, the `AUTO_APP_HOME` export, the `cd` or the quoting of `"$@"` each fails a
test. The full suite was not run for this change; it belongs to the deployment step.

Docs updated in this change: this entry and the index, the [Chinese guide](../grok-bot.md), the README's "Grok Bot
(local execution)" section (checked against `auto-app-jobq --help` and every subcommand's help), the
[handoff contract](../agent-handoff.md) and [Step 4 §21](../step4-run.md).

Verification of the `grokbot` scripts on 2026-10-19: `uv run pytest -q tests/ops tests/jobq` **118 passed**.
`tests/ops/test_grokbot_files.py` covers:
- the sudoers rule and a Defaults line without `requiretty`;
- the order of the egress rules, with no generic established rule;
- the setup strings and unit flags, a launcher with `--no-sandbox` and no userns profile template;
- `userns-profiles.awk` on sample profiles;
- the boundaries `check.sh` probes, its root prechecks, and its `sudo -l -U` parser on sample outputs (classic,
  wrapped, two rules, unparsable);
- `bash -n` of the three scripts under macOS bash 3.2. Both scripts were
also dry-run here against a scratch root with stub commands (first run, re-run, and failure paths). nftables, sudo,
systemd, AppArmor, Xvfb and the app itself can only be checked on the server, with `check.sh`. The guide's new §7
(server installation) covers the two setup steps, the `.deb`, VNC sign-in, the Grok Bot settings and how to read
`check.sh`.

## Final review fixes (2026-10-20)

The final whole-branch review found nothing Critical; this wave fixes its two Important and six Minor findings.

- **`jobq show` leaves out the import records.** `QueryService.job` adds `ingestion`: the import entries with their raw
  `payload`, `parsed_ingestion` and the change payloads. For a mailbox import that payload holds the e-mail's message id,
  subject and the alert's original links, which can carry per-recipient tracking or sign-in tokens, and spec §6.1 lists
  no import data for `show`. `show` now drops the top-level `ingestion` key. A test imports an e-mail-style record and
  checks that the output has no `ingestion` or `payload` key anywhere and no token, message id or subject.
  `apply_kit.py` reads none of these fields.
- **`check.sh` probes each reject rule on a target that only that rule stops.** Each target is checked as root first,
  with the usual meaning of PASS, REPORT "not tested" and FAIL:
  - Rule 4 (private, CGNAT and link-local IPv4): the default gateway from `ip -4 route show default`, on the first of
    TCP 53, 80 and 443 that root reaches (a missing or public gateway is a REPORT), and Tailscale's resolver
    `100.100.100.100:53`, next to the Mac's tailnet address. The LAN-address probes leave through `lo`, so rule 3
    stopped them, not rule 4.
  - Rule 5 (all IPv6): root runs `curl -q -6 -sS -o /dev/null -w '%{http_code}' --max-time 5
    https://www.cloudflare.com/cdn-cgi/trace`. Only when that returns 200 must the same command fail as grokbot;
    otherwise the line is a REPORT "no IPv6 route, not tested". The `[::1]` probe is stopped by rule 3.
  - Rule 3, wider: a sweep of every IPv4 loopback listener in `ss -H -4 -ltn` (127.0.0.0/8, without the DNS stub's
    `127.0.0.53:53` / `127.0.0.54:53` and VNC's 5990), at most 40, printed as one summary line plus a FAIL line naming
    any port grokbot reached.
  - The server keeps no profile, resumes or answer bank (spec §2): a FAIL if `runtime/profile`, `runtime/resumes` or
    `runtime/profile/answers.yaml` exists (checked as root), because `jobq packet` would hand grokbot a profile's
    applicant fields.
- **The egress chain is pinned.** A static test compares the whole body of `chain from_grokbot` with the five expected
  rules, their actions and their order. The old order test passed a mutant whose rule 4 ended in `accept`, and also DNS
  to any port and an extra accept rule; the new test fails all three.
- **What the wrapper fixes, exactly.** sudo's `env_reset` keeps a short default list (`LANG`, `LC_*`, `TZ` and a few
  terminal and display variables; sudo-rs keeps the same), so "the caller's environment is not trusted" said too much.
  The wrapper's comment (its commands are unchanged), the README and the guide now say what holds: the runtime home and
  working directory are fixed, `-I` ignores `PYTHON*`, sudo resets the rest apart from that list. `TZ` would move the
  daily quota's day while `guard.timezone` is unset, so deployment writes
  `{"lease_minutes": 120, "timezone": "America/Los_Angeles"}` to `runtime/config/handoff.json` (the guide's §2.6).
- **VNC password.** `setup.sh app` follows the one-time password with a warning not to paste that line, or any other
  output of `setup.sh app`, into a chat; `check.sh` output holds no secrets and is safe to share. The guide says the same.
- **Status lines** in the guide, this entry and Step 4 no longer say that the apply-kit and `apply_kit.py` are unbuilt.
- **jd-resume-tailor** (separate repository): `apply_kit.py publish` refuses unless the sha256 of `resume.tex` equals
  `source_sha256` in `resume.build.json`, and the same for `cover_letter.tex`, and says to rebuild. A stale PDF next to
  an edited `.tex` can no longer be published; the message written after the application reads the `.tex`.

Verification of this wave on 2026-10-20: `uv run pytest -q tests/jobq tests/ops tests/handoff tests/agent` **281
passed**; the full suite `uv run pytest -q` **2022 passed** (2002 before, plus 20 new tests), 1 warning (the existing
discord `audioop` deprecation), 548 s. The new check.sh tests take the functions out of `check.sh` and run them with
stand-ins for the commands that need root or grokbot, and run the two new awk programs on sample `ss` and
`ip route` outputs; 27 of 27 mutants of the new code (inverted verdicts, probing grokbot before root, the cap, the
port order, the private ranges, a pipeline call, a dropped warning, ...) fail at least one test. check.sh was also
dry-run under macOS bash 3.2 against a scratch root with stub commands: all boundaries held (no FAIL), five leaks
(five FAIL lines, exit 1), nothing testable (REPORT lines only), a public gateway and a gateway with no open port.
Each of `setup.sh`, `check.sh`, `jobq-client` and `deploy/jobq` passes `/bin/bash -n` (bash 3.2). The awk programs
were run with macOS awk, not the server's mawk. jd-resume-tailor: **152 tests OK** (148 before, plus 4). Docs
updated in this wave: this entry and the index row, the [guide](../grok-bot.md) (§2.2, §2.3, §2.6, §3, §4, §6,
§7.2, §7.3, §7.5, §8), the README's Grok Bot section and [Step 4](../step4-run.md) (status row and §21); the skill's
README for `publish`.

Pending, in order (the Mac apply-kit README, `apply_kit.py` and the skill changes are done locally and reviewed):
deployment with the owner's consent plus `runtime/config/handoff.json` set to
`{"lease_minutes": 120, "timezone": "America/Los_Angeles"}`; the owner running `setup.sh base`, installing the Grok Bot
`.deb` and running `setup.sh app` (with sudo, never Claude); `check.sh` as the isolation check; one real job end to
end, stopping before Submit. No commit or push was made, nothing was deployed or
changed on the server, no `.env`, profile or mailbox was read, and no application was submitted.

## Sign-in links from the hidden display (2026-10-20)

**Problem, found on the server.** `setup.sh base` and `setup.sh app` ran on home-ubuntu (Ubuntu 26.04.1; Grok Bot
0.72.0 at `/opt/Grok Bot/grok-bot`), but the sign-in could not be reached:
- Grok Bot signs in with Cursor's polling flow. It opens a `https://cursor.com/loginDeepControl?…` link through
  Electron's `openExternal`, which runs `xdg-open`, then polls until the login is finished in any browser.
- grokbot had no mime defaults, and the system has no default for `x-scheme-handler/https`. So `xdg-open` took the
  first handler in `mimeinfo.cache`, the ChatGPT desktop app, and started it as grokbot.
- The ChatGPT window sat behind Grok Bot on a display with no window manager, so the login page never appeared.
- Electron also moves the app's main process into a transient `app-grok-bot-<pid>.scope` of grokbot's user manager;
  the ChatGPT app ran in `app-org.chromium.Chromium-<pid>.scope`. Both are outside grokbot-app.service.

**Fix.** Links opened through `xdg-open`, the browser launchers or the http/https defaults are recorded, not opened:
- `deploy/grokbot/grokbot-open-url` (POSIX sh) writes one line `grokbot-open-url: <link>` to stderr (or `(no link)`)
  and exits 0; it runs nothing else.
- `setup.sh app` installs it as `xdg-open`, `x-www-browser`, `www-browser` and `sensible-browser` in
  `/usr/local/libexec/grokbot-bin` (root, 0755, written to a temporary name and renamed).
- The launcher exports `PATH=/usr/local/libexec/grokbot-bin:/usr/local/bin:/usr/bin:/bin` and
  `BROWSER=/usr/local/libexec/grokbot-bin/xdg-open` before its unchanged `exec` line.
- For programs that read mime defaults instead (`gio open`, portals), `setup.sh app` writes as grokbot (`bot_write`;
  root never writes in `/home/grokbot`) `~/.local/share/applications/grokbot-open-url.desktop` (Exec = the recorder)
  and `~/.config/mimeapps.list` (http and https go to it). Their content is identical on every run.
- The app's own links (such as the sign-in) land in grokbot-app.service's journal. A command that captures stderr
  sees the line in its own output instead. A browser started by name or by full path (for example `google-chrome`) is
  not covered: PATH keeps `/usr/bin`, and Python webbrowser's `$BROWSER` entry can fall through to google-chrome when
  the recorder has already exited.
- If the app is active, `setup.sh app` stops grokbot-app.service, stops every `app-*.scope` in grokbot's user manager
  (the app's main process and the ChatGPT app), and starts the service again; it prints what it stopped. If the app is
  not active, it still stops leftover scopes before `enable --now`.
- A re-run therefore ends Grok's in-flight work (an open claim, a half-filled form) and any sign-in in progress. The
  guide's §7.2 and setup.sh's closing message now say so.
- `check.sh` reports any `app-*.scope` in grokbot's manager besides `app-grok-bot-*`, i.e. another GUI app running as
  grokbot.

**Sign-in path** ([guide](../grok-bot.md) §7.3, "登录 Grok Bot"):
1. Click sign-in in VNC.
2. On the Mac, run
   `ssh home-ubuntu 'journalctl _UID=1001 --since -15min -o cat | sed -n "s/^grokbot-open-url: //p" | LC_ALL=C grep -xE "https://cursor\.com/[A-Za-z0-9._~%?&=/:+-]*" | tail -n 1'`.
   postedism is in `adm`, so no sudo is needed. Anything running as grokbot can write to that journal, so the command
   keeps only a plain `https://cursor.com/…` link: other hosts, lookalikes such as `cursor.com.evil.example` and
   terminal escape sequences are dropped. postedism's login shell on the server is fish, which runs the quoted part of
   `ssh home-ubuntu '…'`, so the command contains no `$`: `grep -x` matches the whole line.
3. Open the link in the Mac's browser; the app completes the login by polling.

The link is single-use, and clicking sign-in again prints a fresh one. The guide's §5 lists the recorded links as a
residual readable by `adm` members.

Verification on 2026-10-20 (local):
- `uv run pytest -q tests/ops tests/jobq` **158 passed**. The recorder tests:
  - it parses with `sh -n`;
  - run by `/bin/sh` with an empty PATH, it writes exactly one stderr line and exits 0;
  - its code lines are pinned exactly.
- Other tests:
  - the launcher's whole body is pinned (PATH, BROWSER, `exec`);
  - setup.sh installs the four names and writes both grokbot files with the exact content;
  - the restart path's lines are pinned between the `is-active` check and its `else`, and the `else` branch stops
    scopes too;
  - the scope awk programs of setup.sh and check.sh are run on sample `list-units` output;
  - every copy of the sign-in command (the guide, this note, setup.sh's closing message rendered by bash) is the
    exact command, and its quoted remote part has no `$`;
  - that remote part, run under bash and under fish (`fish --no-config -c`, skipped where fish is missing) with a
    stub `journalctl`, prints only the plain cursor.com link, dropping lookalike, other-host and escape-sequence
    lines.
- Mutants caught: 7 of 7 in the first round, and 10 of 10 in the review round. The review round's mutants include a
  recorder that also starts the ChatGPT app in the background, and the restart moved outside the `is-active` check.
- `/bin/bash -n` on setup.sh and check.sh (bash 3.2) and `/bin/sh -n deploy/grokbot/grokbot-open-url` pass.
- A dry run of `setup.sh app` against a scratch root with stub commands showed:
  - active app: stop service, stop scopes, start service;
  - inactive app: stop leftover scopes, then `enable --now`;
  - all four names installed;
  - the generated launcher sent `x-www-browser`, `sensible-browser`, `$BROWSER` and `xdg-open` calls of a stand-in
    app to the recorder.
- On the server, after the deployment of the first review round (before the fish fix), `tests/ops/test_grokbot_files.py`
  and `tests/jobq` **121 passed** under bash 5.3.9 and mawk. The live results are in the next section.

## Deployment and live acceptance (2026-10-20 to 2026-10-21)

Task 6 of the plan, run with the owner. Times are PDT.

- **Deployment.** Two rsync deployments, each with the owner's consent (`scripts/deploy_linux.sh`): the reviewed tree
  (2026-10-20 11:46) and the sign-in recorder after its first review round (19:39). Each backed up the runtime
  databases first and restarted the four auto-app services (`agent`, `ingest`, `preparation`, `worker`; no restarts
  afterwards); `job-search-tunnel` is not part of a deployment and kept running. `runtime/config/handoff.json` is
  `{"lease_minutes": 120, "timezone": "America/Los_Angeles"}`. On the server (bash 5.3.9, mawk)
  `tests/ops/test_grokbot_files.py` and `tests/jobq` passed: 108, then 121 after the second deployment. As postedism
  the wrapper answers `new`, `show` (no `ingestion` or `payload` key) and `queue` and refuses `--db` with exit 2;
  `systemd-analyze verify` accepts `grokbot-egress.service`.
- **Install.** The owner ran `setup.sh base`, installed Grok Bot 0.72.0 from its `.deb` and ran `setup.sh app`.
  grokbot is uid 1001 with no supplementary groups and linger on; `grokbot-egress.service` is active; Xvfb `:90`, the
  app and x11vnc (`127.0.0.1:5990`) run as grokbot's user units. The Mac reaches VNC through
  `ssh -N -L 5990:127.0.0.1:5990 home-ubuntu`. That command holds its terminal, so it needs a tab of its own:
  commands typed into that tab went nowhere, twice.
- **Sign-in.** The ChatGPT incident above was found here. After the second deployment the owner re-ran
  `setup.sh app` (23:16). It stopped the service, the app's scope and a ChatGPT scope (a new copy, started by a
  sign-in click before the re-run), then started the app. Two sign-in clicks became `https://cursor.com/loginDeepControl…`
  lines in the journal, nothing was opened, and the owner finished the login in the Mac's browser. Read as grokbot:
  `xdg-mime query default` picks `grokbot-open-url.desktop` for https and http, and so does `gio mime` for https;
  grokbot's enabled user units are the three grokbot units and Ubuntu's global user presets; there is no autostart
  directory.
- **Fish.** The first version of the sign-in command failed on the server. `ssh home-ubuntu '…'` runs the quoted part
  in postedism's login shell, fish 4.2.1, which rejects `$"` ("Expected a variable name after this $.", exit 127);
  both reviews had run the remote side under bash, zsh and sh only. The `grep -x` command in the previous section is
  the fix, with a fish test. It was local when this was written, so the server's `setup.sh app` still printed the old
  command. (Checked 2026-10-21: it has since been deployed — the server's `deploy/grokbot/setup.sh` is byte-identical
  to this tree's, and the command it prints runs under postedism's fish with no parse error.)
- **`check.sh`** (2026-10-21 00:09): **28 PASS, 0 FAIL, 7 REPORT**, ending `check.sh: no FAIL`.
  - PASS: jobq through sudo; `--db` refused; no access to `/home/postedism` or the runtime database; no profile,
    resumes or answer bank; exactly one sudo rule and no other sudo; Docker and LXD sockets not writable; all 22 IPv4
    loopback listeners refused; the LAN address's 6379 and 1433, `[::1]:5990`, the gateway `10.0.0.1:53` and
    Tailscale's `100.100.100.100:53` refused; no IPv6 for grokbot; public HTTPS 200; the table loaded; no groups or
    subordinate IDs; home 750; linger; VNC on loopback only.
  - The IPv6 PASS was checked by hand, because the probe passes on any status other than 200. No proxy is configured
    (`/etc/environment`, `pam_env.conf`), and postedism's `curl -q -6 --noproxy '*'` gets 200 from cdn-cgi/trace over
    the IPv6 default route, so root's 200 is real IPv6.
  - REPORT:
    - the Mac's tailnet SSH `100.104.170.71:22`: not listening, not tested (the `100.100.100.100` PASS shows that
      rule 4 covers `100.64.0.0/10`);
    - no AppArmor profile allows userns for the app, and no app scope runs besides Grok Bot's;
    - one postedism command line matches the secret pattern: gnome-keyring-daemon's component name `secrets`;
    - grokbot can run `tailscale status` (device names and tailnet addresses) and `lpstat -r` (CUPS);
    - 1,527 world-readable postedism files in `/tmp`, which grokbot can read. They come from other work: a Go
      runtime build (1,007), codex approval schemas (447), pytest's temporary directories (240), audit files (164),
      Claude temporary files (16) and others.
- **Restart.** `sudo systemctl restart user@1001.service` (00:16): grokbot's manager, the app, Xvfb and x11vnc were
  back within a second, and the app was still signed in (checked in VNC).
- **Grok conversation.** Asked in Grok Bot to run `jobq new --limit 3` on the owner's computer (09:28), Grok returned
  three real jobs and "1,087 more". The server's sudo log has
  `grokbot : USER=postedism ; COMMAND=…/deploy/jobq new --limit 3` at 09:28:49. An earlier attempt left no trace on
  the server (no sudo entry, process or SSH login), so a run counts only when that log shows it.
- **Grok Bot settings**, set by the owner inside the app: local execution "Always allow" on home-ubuntu and "Ask every
  time" on the Mac, and an Auto-review Ask-first rule before submitting an application form.
- **Observed, not changed:**
  - `xdg-desktop-portal` and its GTK backend cannot start in grokbot's session ("cannot open display": the user
    manager's environment has no `DISPLAY`). The app runs without portal services, which therefore cannot open links
    either.
  - The app adds `x-scheme-handler/grokbot` and `x-scheme-handler/sand` (its own deep links) to grokbot's
    `mimeapps.list`. A `setup.sh app` re-run writes the two http/https lines back, and the app adds its lines again
    when it starts.
  - `~grokbot/.config/Codex`, a Chromium-style profile, is left over from the ChatGPT app; nothing uses it.

Follow-ups: clean up the world-readable files in `/tmp` or tighten the umask of the tools that write them; decide
whether to delete `~grokbot/.config/Codex`; list the portal and `/tmp` items in the guide's §5.

Pending: deploying the fish-safe sign-in command; Task 7, one real job end to end, stopping before Submit. No commit
or push was made, no `.env`, profile or mailbox was read, and no application was submitted.

## Task 7: one real job end to end (2026-10-21)

The last task of the plan, run with the owner on job **#14143, Palantir Technologies — Software Engineer, New Grad -
Query Engines** (Lever). The run went to the Submit boundary and stopped there: **nothing was submitted**, and no
outcome was recorded. The owner ended the run to work on other parts of the system, so the claim was left open.

**The chain that ran.** Claude Code marked the job `want_to_apply` over ssh as postedism (`jobq want 14143 --reason …`,
the owner picked it from a shortlist); jd-resume-tailor produced the packet for it (track `infra/distributed_systems`,
two HM review rounds) and `apply_kit.py publish` wrote `apply-kit/jobs/14143/` on the Mac: `resume.pdf`,
`cover_letter.pdf`, `answers.md`, `jd.txt`, `job.json`, with `jobs/index.json` updated. In VNC, Grok then claimed the
job, read the packet, read the package on the Mac, copied the two PDFs to its cloud computer and filled the Lever form.

**Verified on the server** (read as postedism; the agent's own account of its work is not evidence):

- `/var/log/auth.log` has `grokbot : PWD=/home/grokbot ; USER=postedism ; COMMAND=…/deploy/jobq claim 14143` and the
  `…/deploy/jobq packet <token>` that follows it. The sudo path works end to end from the Grok Bot app.
- `jobq queue` returns `{"items": [], "total": 0, "blocked": {"daily_limit": false, "by_reason": {"claimed": 1}},
  "submitted_today": 0}`: the claim is live, the job is correctly out of the queue, and nothing was submitted.
- The package on disk matches `job.json` (four files, sha256 each); both PDFs are one page and their text extracts.

**What the run proved, beyond the commands.** The Grok Bot Auto-review "Ask first" rule held at the Submit button. The
apply-kit cheat sheet's convention that a **待确认 (to be confirmed) entry counts as ASK until the owner clears it**
also held: of the nine fields Grok stopped on, six are 待确认 rows in `apply-kit/answers.md`, and it asked instead of
using the suggested values. Neither is enforced by `jobq`; both are interface conventions, and this is the first time
either was exercised against a real form.

**Found in this run (none fixed yet):**

1. **A form field was transcribed wrong and nothing catches it.** Grok entered the owner's LinkedIn as
   `www.linkedin.com/postedism`, dropping `/in/`, which is a dead link. `data/profile.json` and the resume PDF both
   carry the correct `https://www.linkedin.com/in/postedism/`. Nothing compares what the agent types into a form
   against the packet, the profile or the resume, so a transcription error reaches the employer silently. The packet
   would be the natural place to carry these values, but see item 4.
2. **The cover letter had nowhere to go.** Grok reported one file upload field on the Lever form and no cover-letter
   slot, while the cheat sheet (§11) says to upload `cover_letter.pdf` whenever it is optional. Whether the form really
   has no slot was not confirmed. If some ATS forms genuinely take only a resume, the skill is producing a PDF that
   cannot be delivered, and the fallback (paste the body into "Additional information", which the cheat sheet tells
   the agent to leave empty) needs an owner decision rather than an agent's.
3. **The agent wrote long-form answers that `answers.md` does not cover.** The form asked questions outside the three
   in the package ("If Palantir didn't exist…", FDSE vs SWE, preferred product). Grok wrote them itself under the
   cheat sheet's Agent rules. They were not reviewed before the run stopped. The package's `answers.md` covers three
   common questions; a real form asked six.
4. **`packet` carries no applicant data on this deployment.** `runtime/profile/` and `runtime/resumes/` do not exist on
   home-ubuntu by design, so `packet` returns empty `profile`, `answers` and `resumes` (`_profile` reads with
   `missing_ok=True`, and a missing directory raises `FileNotFoundError`, which that path treats as absent). The
   applicant data the agent types comes only from the Mac's apply-kit, which `jobq` cannot see or check.
5. **The claim token is logged in plain text.** sudo records the whole command line, so `jobq packet <token>` writes the
   token into `/var/log/auth.log`, readable by the `adm` group. Within the current trust boundary the impact is small —
   the only caller is grokbot and postedism is more privileged anyway — but the token is a capability that reads the
   package and records an outcome. Added to the guide's §5 residuals.

**Still pending after this run:** the owner's answers to the nine fields (six 待确认 rows in `apply-kit/answers.md`,
plus three the cheat sheet does not cover at all: an upcoming offer deadline, high-school name and year, and
AI-notetaker consent); confirming the cover-letter slot; reviewing the three agent-written answers; and the submission
itself. The claim's 120-minute lease ends 2026-10-21 20:14 PDT; letting it expire or `jobq release <token>` both return
the job to the queue with `want_to_apply` intact.

## Task 7's five findings, fixed (2026-10-21)

Each of [Task 7](#task-7-one-real-job-end-to-end-2026-10-21)'s five findings, with what changed. Two of them live
outside this repository, in the Mac's apply-kit and the jd-resume-tailor skill; they are listed here because the
guide's §6 documents that interface.

1. **A form field was transcribed wrong and nothing caught it** → **the package now carries the identity fields.**
   `apply_kit.py publish` writes `fields.json` next to the resume: `{"source": …, "fields": {full_name, email, phone,
   location, linkedin, github, website}}`, read from the skill's `data/profile.json`. An absent value is `null`
   (leave the form field empty). Publishing refuses a profile whose `linkedin`, `github` or `website` is not a full
   `http(s)://` URL — a bare host is exactly the shape that lost `/in/` in the live run — and refuses a profile with
   no name or no email. The apply-kit cheat sheet's rule 4 now says to copy these fields **from `fields.json`**, not
   from the resume PDF, and to list what was actually typed before submitting. Four tests cover the file's contents,
   the URL check, the missing-name/email check and whitespace stripping.
2. **The cover letter had nowhere to go** → the cheat sheet's file row now says to upload it wherever it is optional
   and, when the whole form has no place for it, to **stop and ask the owner** rather than pasting the body into some
   other text box.
3. **The agent wrote long-form answers that `answers.md` did not cover** (a real form asked six questions; the package
   answered three) → the skill now generates **five**: the previous three plus "something not on your resume" and
   "what you are looking for in your next role". The fourth may use a real experience from the track's library that
   did not fit on the page — it is the one question the resume by definition cannot answer — while the rest stay
   inside the resume and cover letter. On top of that, the cheat sheet now requires the agent to **paste in full** any
   answer it wrote itself, and to leave a question blank and ask when the material has nothing to support it.
4. **`packet` carries no applicant data on this deployment** → unchanged, and it stays that way: the server holds no
   `profile/` or `resumes/` by design. Finding 1's `fields.json` gives the agent an authoritative copy on the Mac,
   which is where that data belongs.
5. **The claim token was logged in plain text** → **`packet`, `record` and `release` now read the token from stdin.**
   The positional argument is optional; leaving it out, or passing `-`, reads one line from stdin (at most 256 bytes,
   stripped, matched against the same token pattern). A missing, empty, overlong or malformed token is a usage error
   (exit 2), and on a terminal the error says how to pipe it in. The argument still works, so nothing that calls the
   old form breaks — but sudo logs the whole command line, so the argument form still writes the token into
   `/var/log/auth.log`; the guide and the instructions for Grok use the stdin form. Eight tests cover stdin with and
   without a trailing newline, `-`, surrounding blanks, the argument form, the four bad-input cases, the terminal
   message, and that the wrapper's `exec` does not redirect stdin.

Verification: `uv run pytest -q tests/jobq tests/ops tests/handoff tests/agent` **309 passed** (jobq 72 → 80).
In the skill's own repository, `.venv/bin/python -m unittest discover -s tests` **156 passed** (152 → 156).

Docs updated: this entry, the [guide](../grok-bot.md) (§2.3's three command rows, §5's token residual, §6's package
layout and its hand-off conventions), the README's `jobq` table and, outside this repository, `apply-kit/README.md`,
`apply-kit/answers.md` and the skill's `SKILL.md`.

**Deployed 2026-10-21 19:34 PDT** with `scripts/deploy_linux.sh home-ubuntu` (rsync mode, the owner asked for it).
`uv lock --check` passed first, so `uv sync --frozen` could not strand the services; there was no leftover
`.deploy-services`; the four running services (ingest, agent, preparation, worker) were stopped, the runtime databases
backed up, and all four came back active. Checked afterwards on the server: `src/auto_app/jobq/cli.py`, `deploy/jobq`
and `src/auto_app/handoff/service.py` have the same sha256 as this tree; `jobq packet --help` prints
`usage: jobq packet [-h] [TOKEN]`; empty stdin gives `no claim token on stdin` (exit 2), a malformed one
`what arrived on stdin is not a claim token` (exit 2), and a well-formed unknown token piped in reaches the service
(`unknown or released claim token`, exit 1), which the argument form answers identically. `.venv/bin/python -m pytest
-q tests/jobq tests/ops` **166 passed** on the server.

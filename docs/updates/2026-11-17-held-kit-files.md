# 2026-11-17 — Handoff: a list of held forms, and the packages read from the server with no token

## Why

The owner decided on 2026-11-17 to move the Claude Code side (the jd-resume-tailor skill and its `apply_kit.py`) to
home-ubuntu. Each job's package (`resume.pdf`, `cover_letter.pdf`, `answers.md`, `fields.json`, `jd.txt`, `job.json`)
will be published on the server in `/home/postedism/Desktop/job-search/apply-kit`, next to `runtime/` and
`auto-application/`. The Mac copy stops being updated.

Until now Grok read the packages on the Mac. After the move it needs another way to get them. The owner asked to keep
security light: Grok gets the files through `jobq`, which already runs as `postedism`, so it needs no claim token and
no permission on the folder changes. ChatGPT Dots, once its handoff tools are on, takes the same route over MCP.

The owner also wants it faster. Claude Code makes materials for up to three jobs at a time, and the owner approves
all held forms together in one Grok conversation ("提交 #a #b #c"), so the agent needs a list of the forms that wait
for that go. The owner still decides which jobs to apply to (`jobq want` only after the owner's explicit
confirmation), and still checks each form once, at the agent's pre-submit list; neither changes here.

## Change

- **`HandoffService.held_applications(now=None)`**, read-only. It lists the jobs whose latest application is `ready`
  (held) and whose claim is still live, oldest first. Each item has `job_id`, `title`, `company`, `apply_url` (the
  application's), `agent` (the claim's), `lease_until` and `held_since` (the application's last update; the same
  agent claiming the job again moves it). Items are sorted by `held_since`, then job ID.
- **The apply-kit folder**: `APPLY_KIT_DIR = "apply-kit"`, the runtime's sibling (`HandoffService.kit_dir`, the
  resolved runtime's parent). Three read-only methods, none needing a token:
  - `kit_index()`: `apply-kit/jobs/index.json`, or `{"jobs": []}` when it does not exist yet. Invalid JSON, or
    anything but an object whose `jobs` is a list, is an error.
  - `kit_file(job_id, name)`: the bytes of `apply-kit/jobs/<job_id>/<name>`. `name` is one of `KIT_FILES`: `job.json`,
    `fields.json`, `jd.txt`, `resume.pdf`, `cover_letter.pdf`, `answers.md`, `message.txt`. A missing package or file
    is `job #<id> has no <name> in its package (not published yet?)`.
  - `kit_guide(which)`: `apply-kit/answers.md` (the cheat sheet, `which="cheatsheet"`, the default) or
    `apply-kit/README.md` (`"readme"`).
  - All three go through the existing bounded reader, now generalized as `_read_beneath`. Each path step, the
    `apply-kit` folder included, is opened without following symlinks, and only a regular file of at most 10 MiB
    that does not change while read is returned. `_read_local` (resumes, evidence, profile) uses it unchanged.
- **`jobq` (Grok)** gains three commands. None needs a token, and none is refused through sudo:
  - `held` prints the list as JSON.
  - `kit` prints the index. `kit ID` prints the job's `job.json` as one JSON object. `kit ID FILE` writes the file's
    raw bytes to stdout, for example `jobq kit 14143 resume.pdf > ~/kit/14143/resume.pdf`. `FILE` is limited to the
    seven names by argparse.
  - `guide [cheatsheet|readme]` prints the guide's text, `cheatsheet` by default.
  - `kit ID FILE` and `guide` are the exceptions to "stdout is one JSON object". Errors are still
    `{"status": "error", "error": "..."}` with exit status 1, and bad arguments exit with 2.
- **MCP handoff tools (only when handoff tools are on)** gain three read-only tools:
  - `get_held_applications()`.
  - `get_application_file(job_id, name)`, which returns `job_id`, `name`, `bytes` and `sha256`, plus `text` for
    `.json`, `.md` and `.txt` files (UTF-8, otherwise an error) or `base64` for the PDFs.
  - `get_apply_guide(which="cheatsheet")`, which returns `{"name", "text"}` with the file name (`answers.md` or
    `README.md`).
- **The owner's CLI** gains `auto-app handoff held` (JSON).
- **Runtime configuration, live on home-ubuntu since 2026-11-17** (the server's `runtime/config/handoff.json`, not
  this code): `"daily_submissions": 200`. The company limit stays at 3 per 30 days.

What this means for access: whoever can run `jobq` (grokbot, through sudo) or the handoff MCP tools can read every
published package, `fields.json` included. That is the owner's choice for this step. The reads reach only the fixed
file names under `apply-kit/`, and nothing in the runtime.

Compatibility: no existing command, output field or default changed. The existing `jobq` commands and their JSON are
unchanged, and the Dot listener gains no tools (handoff tools stay opt-in and require authentication).

## Tests

- `tests/handoff/test_held_and_kit.py` (29 tests). It covers the held list's fields, order and tie-break, an expired
  or used hold leaving the list, that the list writes nothing, the same agent's re-claim, the index (missing, empty,
  broken), each package file, missing files and packages, unknown names and bad job numbers. It also checks symlinks
  refused at the file, the package folder, a guide and the `apply-kit` folder itself, a file over 10 MiB, a directory
  in place of a file, both guides, and `auto-app handoff held`.
- `tests/jobq/test_held_kit_cli.py` (18 tests). It covers `held` after `want`, `claim` and `hold`; `kit`, `kit ID` and
  `kit ID FILE` byte for byte; `guide`; reads through sudo with no token; JSON errors for a missing file, a symlink and
  a broken `job.json`; usage errors (exit 2); that `jobq`'s file names match the service's; and `--help`.
- `tests/handoff/test_held_kit_mcp.py` (3 tests). It covers the three tools, text versus base64 with the SHA-256,
  errors, and that the tools are absent without handoff tools.
- `tests/test_cli_structure.py` lists `held` in the `handoff` group.
- Each test puts its runtime one level down in its own `tmp_path`, so the sibling `apply-kit/` belongs to that test.

## State

Deployed to home-ubuntu on 2026-11-17 (`scripts/deploy_linux.sh`, rsync mode). Before the deploy, the full suite ran
2,607 tests: 2,606 passed and 1 failed. The failure was `tests/ops` asking every unit in `deploy/` for an `OnFailure=` alert. After that was fixed, the ops,
handoff, `jobq` and agent tests passed (457).

Checked on the server after the deploy:

- `jobq held` answers `{"items": []}`.
- `jobq kit` lists #14143.
- The sha256 of `jobq kit 14143 resume.pdf` matches its `job.json`.
- `jobq guide` and `jobq guide readme` print the cheat sheet and README.
- A mistyped FILE exits 2 with `argument FILE: invalid choice`.

**The move was done the same day:**

- The skill is at `/home/postedism/.claude/skills/jd-resume-tailor` with its own Python 3.12 venv, and all 249 of its
  tests pass there. Its `apply_kit.json` uses `"ssh_host": "local"`.
- The 14143 resume and cover letter compile on the server: 1 page, ATS check OK.
- The apply-kit (cheat sheet, README, transcript and the #14143 package) is at
  `/home/postedism/Desktop/job-search/apply-kit`.
- `deploy/jd-apply-queue.{sh,service,timer}` are installed as user units, and the timer is enabled, running hourly:
  - The runner checks `apply_kit.py todo` first, and starts `claude -p --permission-mode auto` only when there is
    work to do.
  - A headless smoke test ran a command in auto mode.
  - The first triggered run logged "nothing to do".
- The Mac's desktop scheduled task `auto-app-queue` is disabled.

Not yet verified:

- No real job has gone through the server run.
- Nobody has read the files as grokbot through sudo: only postedism can run the wrapper here.

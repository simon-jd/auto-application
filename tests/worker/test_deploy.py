"""Run the deployment artifact against local SSH/systemd fakes and a real local rsync/SQLite DB."""

import json
import os
from pathlib import Path
import shutil
import shlex
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "deploy_linux.sh"
SERVICES = ["auto-app-ingest.service", "auto-app-agent.service", "auto-app-preparation.service",
            "auto-app-agent-public.service", "auto-app-bot.service"]


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_name(path.name + ".py").write_text(body)
    path.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$0.py" "$@"\n')
    path.chmod(0o700)


@pytest.fixture
def deployment(tmp_path):
    source, remote, bin_dir = (tmp_path / name for name in ("source", "remote", "bin"))
    (source / "scripts").mkdir(parents=True)
    if SCRIPT.exists():
        shutil.copy2(SCRIPT, source / "scripts" / SCRIPT.name)
    (source / "version.txt").write_text("new")
    private = [".env", ".env.local", "config/screening.json", "config/feeds.yaml", "config/custom.json",
               "data/jobs.db", "credentials/key.json", "profile/cv.txt", "filters/private.md",
               "resumes/private.pdf", "evidence/confirmation.png", "preparation/run/attachment.pdf",
               "preparation/preparation.sqlite3", "other/private.sqlite3", "other/private.sqlite3-wal",
               "browser-profiles/session", ".claude/settings.json", ".codex/history", ".agents/private",
               ".aws/config", ".superpowers/sdd/log", "build/cache", "dist/app", "__pycache__/a.pyc",
               ".pytest_cache/cache", "node_modules/package", "reports/private", "runtime/private"]
    for name in [*private, ".env.example", "config/screening.example.json"]:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fictional fixture only")
    real_git = shutil.which("git")
    assert real_git
    (source / ".gitignore").write_text("\n".join([*private, ".venv/"]) + "\n")
    subprocess.run([real_git, "init", "-q", str(source)], check=True)
    subprocess.run([real_git, "-C", str(source), "add", "scripts", "version.txt", ".gitignore",
                    ".env.example", "config/screening.example.json"], check=True)
    subprocess.run([real_git, "-C", str(source), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
    subprocess.run([real_git, "init", "--bare", "-q", str(remote / "repo.git")], check=True)
    checkout = remote / "auto-application"
    (checkout / "config").mkdir(parents=True)
    (checkout / "config/screening.json").write_text("existing remote config")
    (checkout / "version.txt").write_text("old")
    db = remote / "runtime/data/auto_app.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE predeploy_marker (value TEXT)")
        conn.execute("INSERT INTO predeploy_marker VALUES ('original')")
    events, states = tmp_path / "events.jsonl", tmp_path / "services.json"
    events.touch()
    states.write_text(json.dumps(dict.fromkeys(SERVICES, True) | {"auto-app-worker.service": False}))
    shared = '''import json, os, pathlib, subprocess, sys
def log(event, **fields):
    with open(os.environ["DEPLOY_TEST_EVENTS"], "a") as out:
        out.write(json.dumps({"event": event, **fields}) + "\\n")
'''
    executable(bin_dir / "ssh", shared + '''
log("ssh", phase=sys.argv[-1])
result = subprocess.run(["bash", "-c", " ".join(sys.argv[2:])], input=sys.stdin.read(), text=True)
sys.exit(result.returncode)
''')
    executable(bin_dir / "rsync", shared + '''
if "--server" in sys.argv:
    sys.exit(subprocess.run([os.environ["DEPLOY_TEST_RSYNC"], *sys.argv[1:]]).returncode)
base = pathlib.Path(os.environ["BASE"])
states = json.loads(pathlib.Path(os.environ["DEPLOY_TEST_STATES"]).read_text())
backups = list((base / "runtime/data").glob("*.pre-deploy.db"))
log("rsync", backups=[str(p) for p in backups], active=[s for s, active in states.items()
    if active is True or active in {"active", "activating", "reloading", "deactivating"}])
if os.environ.get("DEPLOY_TEST_FAIL") == "rsync":
    sys.exit(23)
args = sys.argv[1:]
args[-1] = args[-1].split(":", 1)[1]
sys.exit(subprocess.run([os.environ["DEPLOY_TEST_RSYNC"], *args]).returncode)
''')
    executable(bin_dir / "systemctl", shared + '''
args = [a for a in sys.argv[1:] if not a.startswith("--")]
action, names = args[0], args[1:]
path = pathlib.Path(os.environ["DEPLOY_TEST_STATES"])
states = json.loads(path.read_text())
if action == "is-active":
    sys.exit(0 if states.get(names[0]) is True or states.get(names[0]) == "active" else 3)
log(action, services=names)
if action == "show":
    if os.environ.get("DEPLOY_TEST_FAIL") == "inspect":
        sys.exit(1)
    value = states.get(names[0], "not-found")
    active = ("active" if value else "inactive") if isinstance(value, bool) else value
    print("LoadState=" + ("not-found" if active == "not-found" else "loaded"))
    print("ActiveState=" + ("inactive" if active == "not-found" else active))
    print("SubState=" + ("auto-restart" if active == "activating" else "running" if active == "active" else "dead"))
if action in {"start", "restart", "stop"}:
    for name in names:
        states[name] = action != "stop"
    path.write_text(json.dumps(states))
''')
    executable(bin_dir / "uv", shared + '''
log("sync", args=sys.argv[1:])
sys.exit(1 if os.environ.get("DEPLOY_TEST_FAIL") == "sync" else 0)
''')
    executable(checkout / ".venv/bin/python", shared + '''
log("migrate", args=sys.argv[1:])
if os.environ.get("DEPLOY_TEST_FAIL") == "migrate":
    sys.exit(1)
sys.exit(subprocess.run([sys.executable, *sys.argv[1:]], input=sys.stdin.read(), text=True).returncode)
''')
    executable(bin_dir / "git", shared + '''
args = sys.argv[1:]
log("git", args=args)
if args[0] == "push":
    log("push")
    args[2] = args[2].split(":", 1)[1]
if "checkout" in args:
    base = pathlib.Path(os.environ["BASE"])
    backups = list((base / "runtime/data").glob("*.pre-deploy.db"))
    log("checkout", backups=[str(p) for p in backups])
sys.exit(subprocess.run([os.environ["DEPLOY_TEST_GIT"], *args]).returncode)
''')
    rsync = shutil.which("rsync")
    if rsync is None:
        pytest.skip("deployment exclusion test needs rsync")
    env = os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "BASE": str(remote),
                        "DEPLOY_TEST_EVENTS": str(events), "DEPLOY_TEST_STATES": str(states),
                        "DEPLOY_TEST_RSYNC": rsync, "DEPLOY_TEST_GIT": real_git, "PYTHONPATH": str(ROOT / "src")}

    def run(*, dirty=False, ref=None, **extra):
        if dirty:
            (source / "uncommitted.txt").write_text("fixture dirty change")
        return subprocess.run(["bash", str(source / "scripts" / SCRIPT.name), "fake-host", *([ref] if ref else [])],
                              env=env | extra, text=True, capture_output=True)

    return run, remote, events, states, private


def test_deploy_backs_up_before_sync_migrates_then_restores_only_running_services(deployment):
    run, remote, events, states, _ = deployment
    result = run()
    assert result.returncode == 0, result.stderr
    history = [json.loads(line) for line in events.read_text().splitlines()]
    names = [entry["event"] for entry in history]
    assert names.index("stop") < names.index("rsync") < names.index("sync") < names.index("migrate") < names.index("start")
    rsync = next(entry for entry in history if entry["event"] == "rsync")
    assert rsync["active"] == [] and len(rsync["backups"]) == 1
    backup = Path(rsync["backups"][0])
    assert backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT value FROM predeploy_marker").fetchone() == ("original",)
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='job_screenings'").fetchone() is None
    with sqlite3.connect(remote / "runtime/data/auto_app.db") as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='job_screenings'").fetchone()
    assert json.loads(states.read_text()) == dict.fromkeys(SERVICES, True) | {"auto-app-worker.service": False}
    assert not (remote / "runtime/.deploy-services").exists()
    assert not (remote / "runtime/.deployed").exists()


def test_deploy_excludes_private_files_and_caches_without_deleting_remote_config(deployment):
    run, remote, _, _, private = deployment
    result = run()
    assert result.returncode == 0, result.stderr
    checkout = remote / "auto-application"
    assert (checkout / "version.txt").read_text() == "new"
    assert (checkout / "config/screening.json").read_text() == "existing remote config"
    for name in set(private) - {"config/screening.json"}:
        assert not (checkout / name).exists(), name
    assert (checkout / "config/screening.example.json").is_file()
    assert (checkout / ".env.example").is_file()


@pytest.mark.parametrize("phase", ["rsync", "sync", "migrate"])
def test_deploy_failure_preserves_backup_and_keeps_services_stopped(deployment, phase):
    run, remote, events, states, _ = deployment
    result = run(DEPLOY_TEST_FAIL=phase)
    assert result.returncode != 0
    history = [json.loads(line)["event"] for line in events.read_text().splitlines()]
    assert not any(event in history for event in ("start", "restart"))
    assert not any(json.loads(states.read_text()).values())
    assert len(list((remote / "runtime/data").glob("*.pre-deploy.db"))) == 1
    assert (remote / "runtime/.deploy-services").is_file()
    assert "Inspect runtime/.deploy-services" in result.stderr


def test_backup_failure_prevents_code_sync(deployment):
    run, remote, events, _, _ = deployment
    (remote / "runtime/data/auto_app.db").write_bytes(b"not a database")
    result = run()
    assert result.returncode != 0
    history = [json.loads(line)["event"] for line in events.read_text().splitlines()]
    assert "rsync" not in history and "start" not in history


@pytest.mark.parametrize("initial", ["activating", "reloading"])
def test_deploy_stops_restart_pending_service_before_sync_and_restores_it(deployment, initial):
    run, _, events, states, _ = deployment
    states.write_text(json.dumps(dict.fromkeys(SERVICES, True) | {"auto-app-worker.service": initial}))
    result = run()
    assert result.returncode == 0, result.stderr
    history = [json.loads(line) for line in events.read_text().splitlines()]
    assert next(entry for entry in history if entry["event"] == "rsync")["active"] == []
    assert json.loads(states.read_text())["auto-app-worker.service"] is True


def test_inspection_failure_aborts_before_backup_or_sync(deployment):
    run, remote, events, states, _ = deployment
    original = states.read_text()
    result = run(DEPLOY_TEST_FAIL="inspect")
    assert result.returncode != 0
    history = [json.loads(line)["event"] for line in events.read_text().splitlines()]
    assert not any(event in history for event in ("stop", "rsync", "sync", "migrate", "start"))
    assert list((remote / "runtime/data").glob("*.pre-deploy.db")) == []
    assert states.read_text() == original


def test_uninstalled_service_is_skipped_and_stopping_service_is_not_restarted(deployment):
    run, _, events, states, _ = deployment
    states.write_text(json.dumps({"auto-app-ingest.service": True, "auto-app-agent.service": True,
                                 "auto-app-preparation.service": "deactivating"}))
    result = run()
    assert result.returncode == 0, result.stderr
    history = [json.loads(line) for line in events.read_text().splitlines()]
    assert next(entry for entry in history if entry["event"] == "rsync")["active"] == []
    assert json.loads(states.read_text()) == {"auto-app-ingest.service": True, "auto-app-agent.service": True,
                                            "auto-app-preparation.service": False}


def test_running_grok_tunnel_is_stopped_and_started_again_with_its_listener(deployment):
    # The tunnel Requires agent-public, so systemd stops it with agent-public; deploy must restart it.
    run, _, events, states, _ = deployment
    states.write_text(json.dumps(dict.fromkeys(SERVICES, True) | {"job-search-grok-tunnel.service": True}))
    result = run()
    assert result.returncode == 0, result.stderr
    history = [json.loads(line) for line in events.read_text().splitlines()]
    stop = next(entry for entry in history if entry["event"] == "stop")
    start = next(entry for entry in history if entry["event"] == "start")
    assert "job-search-grok-tunnel.service" in stop["services"]
    assert start["services"].index("auto-app-agent-public.service") < start["services"].index(
        "job-search-grok-tunnel.service")
    assert json.loads(states.read_text())["job-search-grok-tunnel.service"] is True


def test_script_is_executable_and_shell_syntax_is_valid():
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0



def test_git_mode_refuses_a_dirty_tree(deployment):
    run, _, events, _, _ = deployment
    result = run(DEPLOY_MODE="git", dirty=True)
    assert result.returncode != 0 and "uncommitted" in result.stderr
    assert not any(json.loads(line)["event"] == "ssh" for line in events.read_text().splitlines())


def test_git_mode_pushes_ref_and_records_success_only_after_resume(deployment):
    run, remote, events, _, _ = deployment
    result = run(DEPLOY_MODE="git")
    assert result.returncode == 0, result.stderr
    history = [json.loads(line) for line in events.read_text().splitlines()]
    names = [entry["event"] for entry in history]
    assert names.index("stop") < names.index("push") < names.index("checkout") < names.index("migrate") < names.index("start")
    checkout = next(entry for entry in history if entry["event"] == "checkout")
    assert len(checkout["backups"]) == 1
    push = next(entry["args"] for entry in history if entry["event"] == "git" and entry["args"][0] == "push")
    commit = push[-1].split(":", 1)[0]
    assert push[-1].endswith(":refs/heads/deploy")
    assert (remote / "runtime/.deployed").read_text().split()[0] == commit
    assert (remote / "auto-application/version.txt").read_text() == "new"
    assert (remote / "auto-application/.venv/bin/python").exists()
    assert (remote / "auto-application/config/screening.json").read_text() == "existing remote config"


def test_git_rollback_preserves_private_paths_without_old_ignore_rules(deployment):
    run, remote, _, _, private = deployment
    source = remote.parent / "source"
    git = shutil.which("git")
    original = (source / ".gitignore").read_text()
    # Local excludes keep private source fixtures untracked while the historical
    # deployment ref lacks the rules that protect private files on the host.
    (source / ".git/info/exclude").write_text(original)
    (source / ".gitignore").write_text(".venv/\n.env\n")
    subprocess.run([git, "-C", str(source), "add", ".gitignore"], check=True)
    subprocess.run([git, "-C", str(source), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "-qm", "old ignore rules"], check=True)
    old = subprocess.check_output([git, "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    (source / ".gitignore").write_text(original)
    subprocess.run([git, "-C", str(source), "add", ".gitignore"], check=True)
    subprocess.run([git, "-C", str(source), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "-qm", "new ignore rules"], check=True)
    assert run(DEPLOY_MODE="git").returncode == 0
    checkout = remote / "auto-application"
    for name in private:
        path = checkout / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("existing remote private fixture")
    (checkout / "obsolete.py").write_text("stale untracked application code")
    result = run(DEPLOY_MODE="git", ref=old)
    assert result.returncode == 0, result.stderr
    for name in private:
        assert (checkout / name).is_file(), name
        assert (checkout / name).read_text() == "existing remote private fixture", name
    assert not (checkout / "obsolete.py").exists()


@pytest.mark.parametrize("private_path", ["config/screening.json", "preparation/run/attachment.pdf", ".env"])
def test_git_deploy_rejects_incoming_tracked_private_files_before_checkout(deployment, private_path):
    run, remote, events, _, _ = deployment
    source = remote.parent / "source"
    git = shutil.which("git")
    subprocess.run([git, "-C", str(source), "add", "-f", private_path], check=True)
    subprocess.run([git, "-C", str(source), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "-qm", "tracked private fixture"], check=True)
    target = remote / "auto-application" / private_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("existing remote private fixture")
    result = run(DEPLOY_MODE="git")
    assert result.returncode != 0
    assert "private" in result.stderr.lower()
    assert target.read_text() == "existing remote private fixture"
    assert not any(json.loads(line)["event"] == "checkout" for line in events.read_text().splitlines())
    assert not (remote / "runtime/.deployed").exists()


def test_git_cleanup_preserves_private_directory_symlinks(deployment):
    run, remote, _, _, _ = deployment
    target = remote / "runtime/preparation"
    target.mkdir()
    (target / "attachment.pdf").write_text("fictional private attachment")
    link = remote / "auto-application/preparation"
    link.symlink_to(target, target_is_directory=True)
    result = run(DEPLOY_MODE="git")
    assert result.returncode == 0, result.stderr
    assert link.is_symlink()
    assert (link / "attachment.pdf").read_text() == "fictional private attachment"


def test_git_preflight_preserves_a_config_directory_symlink(deployment):
    run, remote, events, _, _ = deployment
    link = remote / "auto-application/config"
    target = remote / "runtime/config"
    link.rename(target)
    link.symlink_to(target, target_is_directory=True)
    result = run(DEPLOY_MODE="git")
    assert result.returncode != 0
    assert "private" in result.stderr.lower()
    assert link.is_symlink()
    assert (link / "screening.json").read_text() == "existing remote config"
    assert not any(json.loads(line)["event"] == "checkout" for line in events.read_text().splitlines())


@pytest.mark.parametrize("phase", ["sync", "migrate"])
def test_git_failure_does_not_claim_a_successful_deployment(deployment, phase):
    run, remote, _, _, _ = deployment
    result = run(DEPLOY_MODE="git", DEPLOY_TEST_FAIL=phase)
    assert result.returncode != 0
    assert not (remote / "runtime/.deployed").exists()
    assert (remote / "runtime/.deploy-services").exists()


@pytest.mark.parametrize("ref", ["-x", "HEAD;touch bad", "$(touch bad)", "HEAD\n"])
def test_git_invalid_ref_aborts_before_remote_contact(deployment, ref):
    run, _, events, _, _ = deployment
    result = run(DEPLOY_MODE="git", ref=ref)
    assert result.returncode != 0
    assert not any(json.loads(line)["event"] == "ssh" for line in events.read_text().splitlines())


@pytest.mark.parametrize("base", ["/", "../escape", "Desktop/../escape", "x;touch bad", "$(touch bad)"])
def test_invalid_base_aborts_before_remote_contact(deployment, base):
    run, _, events, _, _ = deployment
    result = run(BASE=base)
    assert result.returncode != 0
    assert not any(json.loads(line)["event"] == "ssh" for line in events.read_text().splitlines())


@pytest.mark.parametrize("mode", ["rsync", "git"])
def test_deploy_also_backs_up_an_existing_preparation_ledger(deployment, mode):
    from auto_app.prepare.store import RunStore

    run, remote, _, _, _ = deployment
    ledger = RunStore(remote / "runtime/preparation")
    result = run(DEPLOY_MODE=mode)
    assert result.returncode == 0, result.stderr
    backups = list(ledger.path.parent.glob("*.pre-deploy.db"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM preparation_runs").fetchone() == (0,)


def test_failed_git_deployment_preserves_previous_success_marker(deployment):
    run, remote, _, _, _ = deployment
    marker = remote / "runtime/.deployed"
    marker.write_text("previous successful deployment")
    result = run(DEPLOY_MODE="git", DEPLOY_TEST_FAIL="migrate")
    assert result.returncode != 0
    assert marker.read_text() == "previous successful deployment"

"""Feed worker timing and locking, using only local fixtures and temporary databases."""

import configparser
from datetime import datetime
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import textwrap

import pytest
import typer
from typer.testing import CliRunner


class Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


def epoch(value):
    return datetime.fromisoformat(value).timestamp()


def config(tmp_path, *, missing=False):
    path = tmp_path / "feeds.yaml"
    (tmp_path / "jobs.json").write_text(json.dumps([
        {"id": "one", "title": "Backend Engineer", "url": "https://example.com/jobs/one",
         "active": True, "is_visible": True, "company_name": "Example"},
    ]))
    path.write_text("sources:\n  local:\n    kind: repository\n    location: jobs.json\n" +
                    ("  failed:\n    kind: repository\n    location: missing.json\n" if missing else ""))
    return path


def test_status_of_missing_database_does_not_create_directory(tmp_path):
    from auto_app.ingest.scheduler import status

    db = tmp_path / "absent" / "jobs.db"
    report = status(db)
    assert report["status"] == "not_started"
    assert report["running"] is False
    assert report["next_run_at"] is None
    assert not db.parent.exists()


def test_first_round_imports_immediately_and_persists_due_time(tmp_path, monkeypatch):
    from auto_app.ingest.scheduler import watch, status
    from auto_app.query.service import QueryService

    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    clock, reports = Clock(), []
    db = tmp_path / "jobs.db"
    result = watch(config(tmp_path), db, once=True, clock=clock, emit=reports.append)
    assert reports[0]["sources"]["local"]["created"] == 1
    assert result["status"] == "ok"
    assert epoch(result["last_run_at"]) == 1_700_000_000
    assert epoch(result["next_run_at"]) == 1_700_001_800
    assert status(db)["running"] is False
    assert status(db)["next_run_at"] == result["next_run_at"]
    with QueryService(db) as query:
        assert query.jobs()["total"] == 1


def test_restart_waits_for_persisted_schedule_and_reloads_config(tmp_path, monkeypatch):
    from auto_app.ingest.scheduler import watch

    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    clock, reports = Clock(), []
    db, feeds = tmp_path / "jobs.db", config(tmp_path)
    watch(feeds, db, once=True, clock=clock)
    feeds.write_text("sources: {}")
    clock.now += 100
    result = watch(feeds, db, clock=clock, sleep=clock.sleep, emit=reports.append,
                   should_stop=lambda: bool(reports))
    assert epoch(result["last_run_at"]) == 1_700_001_800
    assert reports == [{"status": "ok", "sources": {}}]


def test_partial_round_does_not_stop_worker_and_can_recover_next_round(tmp_path, monkeypatch):
    from auto_app.ingest.scheduler import watch

    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    clock, reports = Clock(), []
    feeds = config(tmp_path, missing=True)

    def emitted(report):
        reports.append(report)
        feeds.write_text("sources: {}")

    result = watch(feeds, tmp_path / "jobs.db", interval_seconds=10, clock=clock, sleep=clock.sleep,
                   emit=emitted, should_stop=lambda: len(reports) == 2)
    assert [r["status"] for r in reports] == ["partial", "ok"]
    assert reports[0]["sources"]["local"]["created"] == 1
    assert result["consecutive_failures"] == 0
    assert epoch(result["last_run_at"]) == 1_700_000_010


def test_unexpected_run_error_schedules_retry_and_closes_database(tmp_path, monkeypatch):
    from auto_app.ingest.scheduler import watch, status

    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    clock, reports, engines = Clock(), [], []

    def failing(store, path):
        engines.append(store.engine)
        raise RuntimeError("temporary fetch failure")

    result = watch(config(tmp_path), tmp_path / "jobs.db", once=True, clock=clock,
                   runner=failing, emit=reports.append)
    assert result["status"] == "error"
    assert result["consecutive_failures"] == 1
    assert epoch(result["next_run_at"]) == 1_700_001_800
    assert reports[0]["error"] == "temporary fetch failure"
    assert engines[0].pool.checkedout() == 0
    assert status(tmp_path / "jobs.db")["running"] is False


def test_keyboard_interrupt_closes_round_and_releases_locks(tmp_path, monkeypatch):
    from auto_app.ingest.scheduler import watch, status, import_lock

    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    db = tmp_path / "jobs.db"

    def interrupted(store, path):
        raise KeyboardInterrupt

    result = watch(config(tmp_path), db, runner=interrupted, clock=Clock())
    assert result["status"] == "interrupted"
    assert status(db)["running"] is False
    with import_lock(db):
        pass


def test_second_worker_cannot_run_or_overwrite_state(tmp_path, monkeypatch):
    from auto_app.ingest.scheduler import watch, status, ImportLocked

    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    db, feeds = tmp_path / "jobs.db", config(tmp_path)

    def nested(store, path):
        before = status(db)
        assert before["running"] is True
        with pytest.raises(ImportLocked, match="worker"):
            watch(feeds, db, once=True, clock=Clock())
        assert status(db) == before
        return {"status": "ok", "sources": {}}

    assert watch(feeds, db, once=True, runner=nested, clock=Clock())["status"] == "ok"


def test_manual_import_lock_blocks_round_and_lock_is_reusable(tmp_path, monkeypatch):
    from auto_app.ingest.scheduler import watch, import_lock, ImportLocked

    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    db = tmp_path / "jobs.db"
    with import_lock(db):
        with pytest.raises(ImportLocked):
            with import_lock(db):
                pytest.fail("lock must not permit a second import")
        result = watch(config(tmp_path), db, once=True, clock=Clock())
        assert result["status"] == "busy"
        assert not db.exists()
    assert watch(config(tmp_path), db, once=True, clock=Clock())["status"] == "ok"


def test_stop_during_idle_wait_exits_without_another_import(tmp_path, monkeypatch):
    from auto_app.ingest.scheduler import watch

    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    clock, reports, stopped = Clock(), [], []

    def sleep(duration):
        stopped.append(True)

    result = watch(config(tmp_path), tmp_path / "jobs.db", clock=clock, sleep=sleep,
                   emit=reports.append, should_stop=lambda: bool(stopped))
    assert len(reports) == 1
    assert result["status"] == "ok"


def test_next_run_is_measured_from_completion_without_catchup_burst(tmp_path, monkeypatch):
    from auto_app.ingest.scheduler import watch

    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    clock = Clock()

    def slow_round(store, path):
        clock.now += 300
        return {"status": "ok", "sources": {}}

    result = watch(config(tmp_path), tmp_path / "jobs.db", once=True, clock=clock, runner=slow_round)
    assert epoch(result["last_finished_at"]) == 1_700_000_300
    assert epoch(result["next_run_at"]) == 1_700_002_100


def test_alias_paths_cannot_bypass_import_lock(tmp_path):
    from auto_app.ingest.scheduler import import_lock, ImportLocked

    db = tmp_path / "real.db"
    db.touch()
    alias = tmp_path / "alias.db"
    alias.symlink_to(db)
    with import_lock(db), pytest.raises(ImportLocked):
        with import_lock(alias):
            pytest.fail("database aliases must share the import lock")


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf"), True])
def test_invalid_interval_does_not_create_files(tmp_path, interval):
    from auto_app.ingest.scheduler import watch

    with pytest.raises(ValueError, match="interval"):
        watch(tmp_path / "feeds.yaml", tmp_path / "jobs.db", interval_seconds=interval)
    assert list(tmp_path.iterdir()) == []


def test_status_reports_unreadable_state_without_rewriting_it(tmp_path):
    from auto_app.ingest.scheduler import status

    state = tmp_path / "jobs.db.ingest-state.json"
    state.write_text("not JSON")
    result = status(tmp_path / "jobs.db")
    assert result["status"] == "error"
    assert "state" in result["error"]
    assert state.read_text() == "not JSON"


def schedule_app():
    from auto_app.ingest.schedule_cli import register

    app = typer.Typer()
    register(app)
    return app


def test_schedule_cli_runs_once_and_status_is_read_only(tmp_path):
    app = schedule_app()
    feeds, db = config(tmp_path), tmp_path / "jobs.db"
    runner = CliRunner()
    result = runner.invoke(app, ["watch", "--config", str(feeds), "--db", str(db), "--once"],
                           env={"AUTO_APP_HOME": str(tmp_path)})
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["sources"]["local"]["created"] == 1
    before = db.read_bytes()
    result = runner.invoke(app, ["status", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "ok"
    assert db.read_bytes() == before


def test_schedule_cli_restores_signal_handler_on_exit(tmp_path):
    app = schedule_app()
    previous = signal.getsignal(signal.SIGTERM)
    result = CliRunner().invoke(app, ["watch", "--config", str(config(tmp_path)),
                                     "--db", str(tmp_path / "jobs.db"), "--once"],
                                env={"AUTO_APP_HOME": str(tmp_path)})
    assert result.exit_code == 0, result.output
    assert signal.getsignal(signal.SIGTERM) is previous


def test_daemon_sigterm_exits_promptly_and_preserves_next_run(tmp_path):
    from auto_app.ingest.scheduler import status

    script = ("import typer; from auto_app.ingest.schedule_cli import register; "
              "app = typer.Typer(); register(app); app()")
    db = tmp_path / "jobs.db"
    command = [sys.executable, "-c", script, "watch", "--config", str(config(tmp_path)), "--db", str(db)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env={**os.environ, "AUTO_APP_HOME": str(tmp_path)})
    try:
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready, "worker did not finish its first local round"
        first = json.loads(process.stdout.readline())
        assert first["status"] == "ok"
        assert status(db)["running"] is True
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        assert stdout == ""
        state = status(db)
        assert state["running"] is False
        assert state["status"] == "ok" and state["next_run_at"]
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_daemon_nested_sigterm_finishes_current_round_without_deadlock(tmp_path):
    from auto_app.ingest.scheduler import status

    script = textwrap.dedent("""\
        import os
        import signal
        import threading
        import typer
        from auto_app.ingest import runner
        from auto_app.ingest.schedule_cli import register

        original_run = runner.run_config
        original_notify = threading.Condition.notify_all
        injected = False

        def notify_with_nested_sigterm(condition):
            global injected
            if not injected:
                injected = True
                # Event.set holds a non-reentrant Condition lock here. A second
                # signal must not try to acquire that same lock in its handler.
                os.kill(os.getpid(), signal.SIGTERM)
            return original_notify(condition)

        def finish_round_after_signals(*args, **kwargs):
            report = original_run(*args, **kwargs)
            threading.Condition.notify_all = notify_with_nested_sigterm
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGTERM)
            report["finished_after_sigterm"] = True
            return report

        runner.run_config = finish_round_after_signals
        app = typer.Typer()
        register(app)
        app()
        """)
    db = tmp_path / "jobs.db"
    command = [sys.executable, "-c", script, "watch", "--config", str(config(tmp_path)), "--db", str(db)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env={**os.environ, "AUTO_APP_HOME": str(tmp_path)})
    try:
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pytest.fail("nested SIGTERM deadlocked the worker before its current round completed")
        assert process.returncode == 0, stderr
        [report] = [json.loads(line) for line in stdout.splitlines()]
        assert report["finished_after_sigterm"] is True
        assert report["status"] == "ok" and report["sources"]["local"]["created"] == 1
        state = status(db)
        assert state["running"] is False
        assert state["status"] == "ok" and state["next_run_at"]
        assert state["last_report"]["finished_after_sigterm"] is True
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_systemd_generation_quotes_explicit_linux_paths_and_uses_user_target(tmp_path):
    result = CliRunner().invoke(schedule_app(), [
        "systemd", "--project", '/opt/100% ${PROJECT}/a"b\\c', "--home", "/srv/app data",
        "--config", "/srv/app data/config/feeds.yaml", "--db", "/srv/app data/jobs.db",
        "--uv", "/opt/uv tools/uv", "--interval", "600",
    ])
    assert result.exit_code == 0, result.output
    unit = configparser.ConfigParser(interpolation=None)
    unit.read_string(result.stdout)
    service = unit["Service"]
    assert service["WorkingDirectory"] == '/opt/100%% ${PROJECT}/a"b\\c/'
    assert service["Environment"] == '"AUTO_APP_HOME=/srv/app data"'
    assert service["ExecStart"] == (
        ':"/opt/uv tools/uv" run --project "/opt/100%% ${PROJECT}/a\\"b\\\\c" '
        'auto-app ingest watch --config "/srv/app data/config/feeds.yaml" '
        '--db "/srv/app data/jobs.db" --interval 600')
    assert unit["Install"]["WantedBy"] == "default.target"
    assert service["Restart"] == "on-failure"
    assert list(tmp_path.iterdir()) == []


def test_systemd_paths_do_not_follow_generating_host_symlinks(tmp_path):
    from auto_app.ingest.schedule_cli import build_systemd_unit

    actual = tmp_path / "actual"
    actual.mkdir()
    target = tmp_path / "linux"
    target.symlink_to(actual, target_is_directory=True)
    text = build_systemd_unit(project=target / "project", home=target / "home", uv=str(target / "uv"))
    assert f'WorkingDirectory={target}/project/' in text
    assert f'--config "{target}/home/config/feeds.yaml"' in text
    assert f'--db "{target}/home/data/auto_app.db"' in text


@pytest.mark.parametrize("field", ["project", "home", "config", "db", "uv"])
def test_systemd_rejects_control_characters(field):
    from auto_app.ingest.schedule_cli import build_systemd_unit

    args = {"project": Path("/opt/app"), "home": Path("/srv/app"), "uv": "/bin/uv"}
    args[field] = "/tmp/path\nExecStart=/bin/false"
    with pytest.raises(ValueError, match="control characters"):
        build_systemd_unit(**args)

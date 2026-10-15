# tests/worker/test_cli.py
import json

import pytest

from typer.testing import CliRunner

from auto_app.cli import app


def test_worker_run_screen_only_reports_json(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    monkeypatch.delenv("AUTO_APP_DISCORD_WEBHOOK_URL", raising=False)
    db = tmp_path / "data" / "auto_app.db"
    result = CliRunner().invoke(app, ["worker", "run", "--db", str(db), "--steps", "screen"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output.strip().splitlines()[-1])
    assert report["status"] == "ok" and list(report["steps"]) == ["screen"]
    status = CliRunner().invoke(app, ["worker", "status", "--db", str(db)])
    assert json.loads(status.output)["status"] == "ok"


def test_unknown_steps_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["worker", "run", "--db", str(tmp_path / "x.db"), "--steps", "screen,judge"])
    assert result.exit_code != 0 and "judge" in result.output


def test_empty_steps_and_excess_requests_are_rejected_before_creating_db(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    for options in (["--steps", ", "], ["--enrich-limit", "201"]):
        db = tmp_path / "invalid.db"
        result = CliRunner().invoke(app, ["worker", "run", "--db", str(db), *options])
        assert result.exit_code != 0
        assert not db.exists()


def test_screen_only_never_constructs_a_discord_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    monkeypatch.setenv("AUTO_APP_DISCORD_WEBHOOK_URL", "a-value-the-screen-step-must-not-read")
    def forbidden(*args, **kwargs):
        raise AssertionError("screen-only must not initialize notification transport")
    monkeypatch.setattr("auto_app.core.discord.Discord", forbidden)
    result = CliRunner().invoke(app, ["worker", "run", "--db", str(tmp_path / "jobs.db"), "--steps", "screen"])
    assert result.exit_code == 0, result.output


def test_worker_commands_do_not_load_model_credentials(tmp_path, monkeypatch):
    from auto_app import cli
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    monkeypatch.delenv("AUTO_APP_DISCORD_WEBHOOK_URL", raising=False)
    env_reads = []
    monkeypatch.setattr(cli, "load_env_file", lambda path: env_reads.append(path))
    for command in (["status"], ["run", "--steps", "screen"]):
        result = CliRunner().invoke(app, ["worker", *command, "--db", str(tmp_path / "jobs.db")])
        assert result.exit_code == 0, result.output
    assert env_reads == []


def test_bad_notify_since_does_not_echo_credential_shaped_input(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    secret = "https://discord.com/api/webhooks/1/fictional-test-token"
    for command in ("run", "watch"):
        result = CliRunner().invoke(app, ["worker", command, "--steps", "screen", "--notify-since", secret,
                                         "--db", str(tmp_path / "invalid.db")])
        assert result.exit_code != 0
        assert "fictional-test-token" not in result.output and "webhooks/1" not in result.output
        assert not (tmp_path / "invalid.db").exists()


def test_explicit_cutoff_is_persisted_to_the_worker_loop_state(tmp_path, monkeypatch):
    from auto_app.worker import loop
    from auto_app.worker.cli import _round_fn
    from auto_app.worker.round import WorkerOptions
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    monkeypatch.setattr(loop, "STATE", ".fixture-worker-state.json")
    def interrupted(*a, **kw):
        raise RuntimeError("stop")
    monkeypatch.setattr("auto_app.worker.round.run_round", interrupted)
    db = tmp_path / "jobs.db"
    cutoff = "2026-10-12T00:00:00+00:00"
    with pytest.raises(RuntimeError, match="stop"):
        _round_fn(db, WorkerOptions(tmp_path / "screening.json", steps=("screen",)), cutoff)({})
    assert loop.status(db)["notify_since"] == cutoff


def test_bot_mode_keeps_webhook_alerts_without_a_digest(tmp_path, monkeypatch):
    from auto_app.worker.cli import _round_fn
    from auto_app.worker.round import WorkerOptions

    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    monkeypatch.setenv("AUTO_APP_DISCORD_WEBHOOK_URL", "fixture-webhook")
    posts = []

    class Alerts:
        def __init__(self, url):
            pass
        def post(self, content=None, embeds=None):
            posts.append((content, embeds))
        def close(self):
            pass

    monkeypatch.setattr("auto_app.core.discord.Discord", Alerts)
    monkeypatch.setattr("auto_app.worker.round.run_round", lambda *a, **kw: {
        "status": "partial", "steps": {"enrich": {"status": "error", "error": "Employer API unavailable"}}})
    opts = WorkerOptions(tmp_path / "screening.json", steps=("screen", "enrich"))
    report = _round_fn(tmp_path / "jobs.db", opts, None)({})
    assert len(posts) == 1 and "failed" in posts[0][0] and posts[0][1] is None
    assert "notify" not in report["steps"] and report["alerts"]


def test_an_invalid_control_file_is_reported_and_alerted(tmp_path, monkeypatch):
    import auto_app.core.discord as discord_module
    from auto_app.worker.control import CONTROL

    posts = []

    class Recorder:
        def __init__(self, url, **kwargs):
            pass

        def post(self, content=None, embeds=None):
            posts.append(content)

        def close(self):
            pass

    monkeypatch.setattr(discord_module, "Discord", Recorder)
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    monkeypatch.setenv("AUTO_APP_DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/token")
    db = tmp_path / "data" / "auto_app.db"
    db.parent.mkdir(parents=True)
    db.with_name(db.name + CONTROL).write_text("broken")
    result = CliRunner().invoke(app, ["worker", "run", "--db", str(db), "--steps", "screen"])
    assert result.exit_code == 1 and "control_invalid" in result.output
    assert len(posts) == 1 and "control file" in posts[0]

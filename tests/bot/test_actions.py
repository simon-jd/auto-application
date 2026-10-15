"""Discord decisions and configuration use the same library service as chat tools."""

import os
from pathlib import Path

import pytest

from auto_app.bot.actions import (
    BotSettings,
    custom_id,
    jobs_summary,
    parse_custom_id,
    record_action,
)
from tests.imported_jobs import ingest, job_id_of, make_store, record

OWNER = 1234567890


@pytest.fixture
def db(tmp_path):
    store = make_store(tmp_path)
    ingest(store, [record("101", "Software Engineer, New Grad")])
    job = job_id_of(store, "Software Engineer, New Grad")
    store.engine.dispose()
    return tmp_path / "data" / "auto_app.db", job


def test_custom_ids_reject_unrecognized_or_malformed_actions():
    assert parse_custom_id(custom_id(41, "want")) == (41, "want")
    for bad in ("job:41:delete", "job:x:want", "other:41:want", "job:41:want:extra"):
        assert parse_custom_id(bad) is None


def test_only_owner_can_record_a_manual_decision(db):
    path, job = db
    assert "only" in record_action(path, 999, OWNER, job, "want").lower()
    store = make_store(path.parent.parent)
    assert store.job(job)["decision"] is None
    store.engine.dispose()
    assert "want_to_apply" in record_action(path, OWNER, OWNER, job, "want")
    store = make_store(path.parent.parent)
    assert store.job(job)["decision"] == "want_to_apply"
    store.engine.dispose()


def test_jobs_summary_filters_company_and_excludes_handled_jobs(db):
    path, job = db
    assert [row["job_id"] for row in jobs_summary(path, company="Acme")] == [job]
    assert jobs_summary(path, company="Missing") == []
    record_action(path, OWNER, OWNER, job, "skip")
    assert jobs_summary(path, company="Acme") == []


def test_credential_file_requires_mode_0600_and_valid_ids(tmp_path):
    path = tmp_path / "bot.env"
    path.write_text("AUTO_APP_DISCORD_BOT_TOKEN=abc.def.ghi\nAUTO_APP_DISCORD_OWNER_ID=123\n"
                    "AUTO_APP_DISCORD_GUILD_ID=456\nAUTO_APP_DISCORD_CHANNEL_ID=789\n")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        BotSettings.from_file(path, tmp_path / "x.db")
    path.chmod(0o600)
    settings = BotSettings.from_file(path, tmp_path / "x.db")
    assert (settings.owner_id, settings.guild_id, settings.channel_id) == (123, 456, 789)
    path.write_text(path.read_text().replace("CHANNEL_ID=789", "CHANNEL_ID=bad"))
    with pytest.raises(ValueError, match="CHANNEL_ID"):
        BotSettings.from_file(path, tmp_path / "x.db")


def test_credential_file_rejects_shell_syntax_without_executing_it(tmp_path):
    path = tmp_path / "bot.env"
    marker = tmp_path / "marker"
    path.write_text("AUTO_APP_DISCORD_BOT_TOKEN=$(touch " + str(marker) + ")\n"
                    "AUTO_APP_DISCORD_OWNER_ID=123\nAUTO_APP_DISCORD_GUILD_ID=456\n"
                    "AUTO_APP_DISCORD_CHANNEL_ID=789\n")
    path.chmod(0o600)
    with pytest.raises(ValueError):
        BotSettings.from_file(path, tmp_path / "x.db")
    assert not marker.exists()


def test_missing_credential_file_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="credential"):
        BotSettings.from_file(tmp_path / "missing.env", tmp_path / "x.db")


def test_token_is_never_in_settings_repr():
    settings = BotSettings("a.private.bot.token", 1, 2, 3, Path("x.db"))
    assert "private" not in repr(settings)


def test_invalid_utf8_credential_file_has_generic_error(tmp_path):
    path = tmp_path / "bot.env"
    path.write_bytes(b"\xff private credential data")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="credential file") as exc:
        BotSettings.from_file(path, tmp_path / "x.db")
    assert "private" not in str(exc.value)


def test_symlink_swap_during_open_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "bot.env"
    secret = tmp_path / "secret.env"
    valid = ("AUTO_APP_DISCORD_BOT_TOKEN=abc.def.ghi\nAUTO_APP_DISCORD_OWNER_ID=123\n"
             "AUTO_APP_DISCORD_GUILD_ID=456\nAUTO_APP_DISCORD_CHANNEL_ID=789\n")
    path.write_text(valid)
    secret.write_text(valid)
    path.chmod(0o600)
    secret.chmod(0o600)
    real_open = os.open

    def swap_before_open(file, flags, *args, **kwargs):
        if str(file) == str(path):
            path.unlink()
            path.symlink_to(secret)
        return real_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)
    with pytest.raises(ValueError, match="credential"):
        BotSettings.from_file(path, tmp_path / "x.db")


def test_rejected_bot_token_exits_with_the_configuration_status(tmp_path, monkeypatch):
    import discord
    from typer.testing import CliRunner

    import auto_app.bot.app as bot_app
    from auto_app.bot.cli import app

    path = tmp_path / "bot.env"
    path.write_text("AUTO_APP_DISCORD_BOT_TOKEN=abc.def.ghi\nAUTO_APP_DISCORD_OWNER_ID=123\n"
                    "AUTO_APP_DISCORD_GUILD_ID=456\nAUTO_APP_DISCORD_CHANNEL_ID=789\n")
    path.chmod(0o600)

    def rejected(settings):
        raise discord.LoginFailure("Improper token has been passed.")

    monkeypatch.setattr(bot_app, "run", rejected)
    result = CliRunner().invoke(app, ["--db", str(tmp_path / "x.db"), "--credentials", str(path)])
    # systemd's RestartPreventExitStatus=2 turns this into a failed unit and an OnFailure alert.
    assert result.exit_code == 2
    assert "Discord rejected the bot token" in result.output
    assert "abc.def.ghi" not in result.output

"""Linux unit generation is a text-only operation, including through the CLI."""

import configparser
from pathlib import Path

import pytest
from typer.testing import CliRunner


def unit_config(text):
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(text)
    return config


def test_system_unit_runs_existing_serve_with_fixed_paths():
    from auto_app.service import build_systemd_unit

    unit = unit_config(build_systemd_unit(Path("/opt/auto app"), Path("/srv/app data"), "/usr/local/bin/uv"))
    service = unit["Service"]
    assert service["ExecStart"] == ':"/usr/local/bin/uv" run --project "/opt/auto app" auto-app serve run'
    assert service["WorkingDirectory"] == "/opt/auto app/"
    assert service["Environment"] == '"AUTO_APP_HOME=/srv/app data"'
    assert service["Type"] == "simple"
    assert service["Restart"] == "on-failure"
    assert service["KillMode"] == "control-group"
    assert service["TimeoutStopSec"] == "60"
    assert unit["Install"]["WantedBy"] == "multi-user.target"


def test_special_characters_remain_literal_systemd_paths():
    from auto_app.service import build_systemd_unit

    unit = unit_config(build_systemd_unit(
        Path('/opt/100% ${PROJECT}/a"b\\c'), Path('/srv/100% ${HOME}/a"b\\c'), '/opt/uv tools/uv',
    ))
    service = unit["Service"]
    # WorkingDirectory takes a single raw path; ExecStart/Environment unquote words.
    assert service["WorkingDirectory"] == '/opt/100%% ${PROJECT}/a"b\\c/'
    assert service["ExecStart"] == ':"/opt/uv tools/uv" run --project "/opt/100%% ${PROJECT}/a\\"b\\\\c" auto-app serve run'
    assert service["Environment"] == '"AUTO_APP_HOME=/srv/100%% ${HOME}/a\\"b\\\\c"'


def test_absolute_target_paths_are_not_resolved_on_the_generating_host(tmp_path):
    from auto_app.service import build_systemd_unit

    actual = tmp_path / "actual"
    actual.mkdir()
    target = tmp_path / "target"
    target.symlink_to(actual, target_is_directory=True)
    service = unit_config(build_systemd_unit(target / "project", target / "home", str(target / "uv")))["Service"]
    assert service["WorkingDirectory"] == f"{target}/project/"
    assert service["Environment"] == f'"AUTO_APP_HOME={target}/home"'
    assert service["ExecStart"] == f':"{target}/uv" run --project "{target}/project" auto-app serve run'


@pytest.mark.parametrize("field", ["project", "home", "uv"])
def test_line_breaks_cannot_add_unit_directives(field):
    from auto_app.service import build_systemd_unit

    kwargs = {"project": Path("/opt/app"), "home": Path("/srv/app"), "uv": "/bin/uv"}
    kwargs[field] = "/tmp/a\nExecStart=/bin/false" if field == "uv" else Path("/tmp/a\nExecStart=/bin/false")
    with pytest.raises(ValueError, match="control characters"):
        build_systemd_unit(**kwargs)


@pytest.fixture
def cli(monkeypatch, tmp_path):
    from auto_app import cli

    monkeypatch.setattr(cli, "load_env_file", lambda path: None)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path / "app home"))
    monkeypatch.chdir(tmp_path)
    return cli.app


def test_cli_prints_unit_without_creating_project_or_home(cli, tmp_path):
    result = CliRunner().invoke(cli, [
        "serve", "systemd", "--project", "new project", "--home", "new home", "--uv", "/usr/bin/uv",
    ])
    assert result.exit_code == 0, result.output
    service = unit_config(result.stdout)["Service"]
    assert service["WorkingDirectory"] == f"{tmp_path}/new project/"
    assert service["Environment"] == f'"AUTO_APP_HOME={tmp_path}/new home"'
    assert list(tmp_path.iterdir()) == []
    assert result.stderr == ""


def test_cli_defaults_keep_auto_app_home_and_resolve_uv_from_path(cli, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nexit 99\n")
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    result = CliRunner().invoke(cli, ["serve", "systemd"])
    assert result.exit_code == 0, result.output
    service = unit_config(result.stdout)["Service"]
    assert service["WorkingDirectory"] == f"{tmp_path}/project/"
    assert service["Environment"] == f'"AUTO_APP_HOME={tmp_path}/app home"'
    assert service["ExecStart"] == f':"{uv}" run --project "{tmp_path}/project" auto-app serve run'


def test_cli_missing_uv_gives_actionable_error_without_unit(cli, monkeypatch):
    monkeypatch.setenv("PATH", "")
    result = CliRunner().invoke(cli, ["serve", "systemd"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "uv" in result.stderr and "--uv" in result.stderr


def test_cli_help_exposes_generation_options(cli):
    result = CliRunner().invoke(cli, ["serve", "systemd", "--help"])
    assert result.exit_code == 0, result.output
    assert all(option in result.output for option in ("--project", "--home", "--uv"))

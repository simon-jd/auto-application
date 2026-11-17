"""The command tree: groups and subcommands; old flat names are gone; a bare group shows its help."""

import pytest
from typer.testing import CliRunner

from auto_app.cli import app

TREE = {
    "handoff": ["queue", "claim", "packet", "record", "release", "hold", "held", "questions", "answers-needed",
                "answers-ready", "requeue", "account", "resume"],
    "events": ["add", "list", "unmatched", "assign", "dismiss"],
    "ops": ["backup", "verify", "prune", "alert"],
    "worker": ["run", "watch", "status", "boards"],
    "bot": ["run"],
    "ingest": ["repository", "email", "run", "watch", "status", "systemd", "auth"],
    "source": ["list", "show", "add", "login", "health", "plan"],
    "collect": ["run", "probe", "stats"],
    "process": ["parse", "normalize", "dedup"],
    "job": ["list", "show", "mark"],
    "filter": ["list", "show", "run", "why", "reparse"],
    "data": ["inspect", "export", "import", "purge"],
    "schedule": ["install", "show", "remove"],
}


def test_every_subcommand_exists_and_has_help():
    runner = CliRunner()
    for group, subs in TREE.items():
        for sub in subs:
            r = runner.invoke(app, [group, sub, "--help"])
            assert r.exit_code == 0, (group, sub, r.output)
    assert runner.invoke(app, ["run", "--help"]).exit_code == 0


@pytest.mark.parametrize("group", list(TREE))
def test_a_bare_group_shows_its_help_in_workflow_order(group):
    r = CliRunner().invoke(app, [group])
    # A command's row starts right after "│ "; a wrapped description continues on an indented line, whose first word
    # can happen to be a command name (handoff's packet help wraps before "account").
    listed = [line.split()[1] for line in r.output.splitlines() if line.startswith("│ ") and line[2:3] != " "
              and len(line.split()) > 2 and line.split()[1] in TREE[group]]
    assert listed == TREE[group] and "Usage:" in r.output


@pytest.mark.parametrize("old", ["login", "plugins", "add-company", "plan", "sources", "probe", "stats", "parse",
                                 "normalize", "dedup", "jobs", "mark", "filters", "select", "why", "inspect", "export",
                                 "import-files", "purge"])
def test_old_flat_names_are_gone(old):
    r = CliRunner().invoke(app, [old])
    assert r.exit_code != 0 and "No such command" in r.output


def test_legacy_option_aliases_are_gone():
    assert "No such option" in CliRunner().invoke(app, ["collect", "run", "-c", "nvidia"]).output
    assert "No such option" in CliRunner().invoke(app, ["source", "plan", "--site", "linkedin"]).output


def test_new_deterministic_commands_do_not_read_model_environment(tmp_path, monkeypatch):
    from auto_app import cli
    from tests.imported_jobs import make_store

    store = make_store(tmp_path)
    store.engine.dispose()
    reads = []
    monkeypatch.setattr(cli, "load_env_file", lambda p: reads.append(p))
    for command in (["handoff", "queue"], ["events", "unmatched"], ["ops", "prune"]):
        result = CliRunner().invoke(app, command, env={"AUTO_APP_HOME": str(tmp_path)})
        assert result.exit_code == 0, result.output
    assert reads == []

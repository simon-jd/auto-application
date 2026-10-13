"""CLI imports to disposable databases and returns machine-readable reports."""

import json

import pytest
from typer.testing import CliRunner

from auto_app.ingest.cli import app

runner = CliRunner()


def invoke(tmp_path, *args):
    return runner.invoke(app, list(args), env={"AUTO_APP_HOME": str(tmp_path)})


def feed(path, *, partial=False):
    rows = [{"id": "one", "url": "https://careers.example.com/jobs/one", "title": "Backend Engineer",
             "company_name": "Example", "active": True, "is_visible": True}]
    if partial:
        rows.append({"id": "broken"})
    path.write_text(json.dumps(rows))
    return path


def test_repository_cli_reports_created_then_unchanged(tmp_path):
    path = feed(tmp_path / "feed.json")
    db = tmp_path / "data" / "jobs.db"
    first = invoke(tmp_path, "repository", str(path), "--source", "repo", "--db", str(db))
    assert first.exit_code == 0, first.output
    assert json.loads(first.stdout)["created"] == 1
    again = invoke(tmp_path, "repository", str(path), "--source", "repo", "--db", str(db))
    assert again.exit_code == 0, again.output
    assert json.loads(again.stdout)["unchanged"] == 1


@pytest.mark.parametrize("case", ["missing_file", "invalid_source", "invalid_config"])
def test_input_errors_are_json_with_nonzero_exit_and_do_not_create_database(tmp_path, case):
    db = tmp_path / "not-created" / "jobs.db"
    if case == "missing_file":
        args = ["repository", str(tmp_path / "missing.json"), "--source", "repo"]
    elif case == "invalid_source":
        args = ["repository", str(feed(tmp_path / "feed.json")), "--source", "Bad Source"]
    else:
        config = tmp_path / "invalid.yaml"
        config.write_text("sources: [invalid]")
        args = ["run", "--config", str(config)]
    result = invoke(tmp_path, *args, "--db", str(db))
    assert result.exit_code != 0
    assert json.loads(result.stdout)["status"] == "error"
    assert result.stderr
    assert not db.exists()


def test_partial_repository_returns_nonzero_and_json_issues(tmp_path):
    path = feed(tmp_path / "partial.json", partial=True)
    result = invoke(tmp_path, "repository", str(path), "--source", "repo", "--db", str(tmp_path / "jobs.db"))
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["status"] == "partial" and report["created"] == 1 and report["issues"]
    assert result.stderr


def test_email_cli_accepts_multiple_files_and_keeps_each_observation(tmp_path):
    paths = []
    for number in (100, 200):
        path = tmp_path / f"{number}.eml"
        path.write_text(f"Message-ID: <{number}@example.com>\nContent-Type: text/plain\n\n"
                        f"https://www.linkedin.com/jobs/view/{number}")
        paths.append(str(path))
    result = invoke(tmp_path, "email", *paths, "--source", "mail", "--db", str(tmp_path / "jobs.db"))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["created"] == 2


def test_config_cli_continues_other_sources_and_exits_nonzero(tmp_path):
    feed(tmp_path / "good.json")
    config = tmp_path / "feeds.yaml"
    config.write_text("sources:\n  bad:\n    kind: repository\n    location: missing.json\n"
                      "  good:\n    kind: repository\n    location: good.json\n")
    result = invoke(tmp_path, "run", "--config", str(config), "--db", str(tmp_path / "jobs.db"))
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["sources"]["good"]["created"] == 1
    assert report["sources"]["bad"]["status"] == "error"
    assert "missing.json" in result.stderr

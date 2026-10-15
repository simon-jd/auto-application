import json
from typer import Typer
from typer.testing import CliRunner

from tests.imported_jobs import ingest, job_id_of, make_store, record


def test_events_add_list_and_invalid_date(tmp_path):
    from auto_app.track.cli import app as events_app
    store = make_store(tmp_path)
    ingest(store, [record("101", "Backend Engineer")])
    job = job_id_of(store, "Backend Engineer")
    store.engine.dispose()
    app = Typer()
    app.add_typer(events_app, name="events")
    env = {"AUTO_APP_HOME": str(tmp_path)}
    runner = CliRunner()
    added = runner.invoke(app, ["events", "add", str(job), "interview", "--note", "phone screen"], env=env)
    assert added.exit_code == 0, added.output
    listed = runner.invoke(app, ["events", "list", str(job)], env=env)
    assert [e["kind"] for e in json.loads(listed.output)] == ["interview"]
    bad = runner.invoke(app, ["events", "add", str(job), "interview", "--at", "invalid"], env=env)
    assert bad.exit_code == 1 and json.loads(bad.output)["status"] == "error"
    unknown = runner.invoke(app, ["events", "add", "99999", "interview"], env=env)
    assert unknown.exit_code == 1 and "unknown job" in json.loads(unknown.output)["error"]


def test_events_cli_interprets_naive_iso_time_as_utc(tmp_path):
    from auto_app.track.cli import app
    store = make_store(tmp_path)
    ingest(store, [record("101", "Backend Engineer")])
    job = job_id_of(store, "Backend Engineer")
    store.engine.dispose()
    result = CliRunner().invoke(app, ["add", str(job), "interview", "--at", "2026-10-12T12:00:00"],
                                env={"AUTO_APP_HOME": str(tmp_path)})
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["occurred_at"] == "2026-10-12T12:00:00+00:00"

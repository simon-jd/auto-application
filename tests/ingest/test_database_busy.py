"""A locked database fails one feed, not the round (twice on 2026-11-19 a "database is locked" in update_health ended
a whole ingest round): connections wait up to 30 seconds for the lock, and a feed that still finds it locked is
reported as an error and read again next round."""

import json
import sqlite3

import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from auto_app.ingest import runner
from auto_app.ingest.runner import run_config
from tests.imported_jobs import make_store


def locked():
    return OperationalError("INSERT INTO source_health ...", {}, sqlite3.OperationalError("database is locked"))


@pytest.fixture
def setup(tmp_path):
    for name in ("one", "two", "three"):
        (tmp_path / f"{name}.json").write_text(json.dumps([]))
    path = tmp_path / "feeds.yaml"
    path.write_text(yaml.safe_dump({"sources": {name: {"kind": "repository", "location": f"{name}.json"}
                                                for name in ("one", "two", "three")}}))
    store = make_store(tmp_path)
    yield store, path
    store.engine.dispose()


def test_connections_wait_thirty_seconds_for_a_lock(setup):
    store, _ = setup
    with store.engine.connect() as conn:
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 30000


def test_a_feed_that_finds_the_database_locked_fails_alone(setup, monkeypatch):
    store, path = setup
    real = runner.run_repository

    def flaky(store, source, *args, **kwargs):
        if source == "two":
            raise locked()
        return real(store, source, *args, **kwargs)

    monkeypatch.setattr(runner, "run_repository", flaky)
    report = run_config(store, path)
    assert report["sources"]["one"]["status"] == "ok" and report["sources"]["three"]["status"] == "ok"
    assert report["sources"]["two"]["status"] == "error" and "database busy" in report["sources"]["two"]["error"]
    assert report["status"] == "partial"


def test_a_locked_health_write_is_skipped_and_the_round_goes_on(setup, monkeypatch):
    store, path = setup
    real = store.update_health

    def flaky(source, **fields):
        if source == "one" and "ingest_run" in fields:
            raise locked()
        return real(source, **fields)

    monkeypatch.setattr(store, "update_health", flaky)
    report = run_config(store, path)
    assert all(report["sources"][name]["status"] == "ok" for name in ("one", "two", "three"))
    assert report["sources"]["one"]["health_saved"] is False
    assert "ingest_run" in store.source_health("three")


def test_other_database_errors_still_stop_the_round(setup, monkeypatch):
    store, path = setup

    def broken(*args, **kwargs):
        raise OperationalError("SELECT ...", {}, sqlite3.OperationalError("no such table: postings"))

    monkeypatch.setattr(runner, "run_repository", broken)
    with pytest.raises(OperationalError):
        run_config(store, path)

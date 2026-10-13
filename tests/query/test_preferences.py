"""Country preferences must filter before pagination and retain audit evidence."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auto_app.query.api import create_app
from auto_app.query.service import QueryError, QueryService
from tests.query.test_cli import database
from tests.query.test_service import add_job, db_path  # noqa: F401


@pytest.fixture
def configured(db_path):
    config = db_path.parent / "preferences.json"
    config.write_text(json.dumps({"exclude_countries": ["Canada"]}))
    with sqlite3.connect(db_path) as conn:
        conn.executemany("INSERT INTO job_locations (job_id,location_norm) VALUES (?,?)",
                         [(1, "toronto, on"), (2, "san jose, ca"), (2, "vancouver, bc"),
                          (3, "ontario, ca"), (5, "remote")])
        # The normalizer drops the country from remote locations. Raw evidence
        # from a non-primary posting must still exclude the whole job.
        add_job(conn, 7, "Remote Engineer", "Other", "email")
        conn.execute("UPDATE postings SET job_id=5, parsed=? WHERE posting_id=7",
                     (json.dumps({"locations": ["Remote in Canada"]}),))
        conn.execute("DELETE FROM jobs WHERE job_id=7")
    return db_path, config


def test_country_exclusions_precede_total_and_cursor_pagination(configured):
    path, config = configured
    with sqlite3.connect(path) as conn:
        before = list(conn.iterdump())
    with QueryService(path, preferences_path=config) as service:
        page = service.jobs(limit=1)
        assert page["total"] == 2
        assert [j["job_id"] for j in page["items"]] == [4]
        second = service.jobs(limit=1, cursor=page["next_cursor"])
        assert [j["job_id"] for j in second["items"]] == [3]
        assert second["next_cursor"] is None
        assert service.jobs(include_excluded=True)["total"] == 5
        assert service.job(2)["screening"]["excluded"] is True
        assert service.job(5)["screening"]["matched_locations"] == ["Remote in Canada"]
        assert service.job(3)["screening"]["excluded"] is False
        context = service.context()
        assert context["preferences"]["exclude_countries"] == ["Canada"]
        assert context["preferences"]["excluded_active_jobs"] == 3
        assert context["preferences"]["included_active_jobs"] == 2
        assert context["counts"]["total"] == 6  # raw library is preserved
        with pytest.raises(QueryError, match="cursor"):
            service.jobs(cursor=page["next_cursor"], include_excluded=True)
    with sqlite3.connect(path) as conn:
        assert list(conn.iterdump()) == before


def test_changed_preferences_invalidate_old_cursor(configured):
    path, config = configured
    with QueryService(path, preferences_path=config) as service:
        cursor = service.jobs(limit=1)["next_cursor"]
    config.write_text('{"exclude_countries": []}')
    with QueryService(path, preferences_path=config) as service:
        with pytest.raises(QueryError, match="cursor"):
            service.jobs(cursor=cursor)


def test_http_adapter_obeys_same_preferences(configured):
    path, config = configured
    with TestClient(create_app(path, preferences_path=config), base_url='http://127.0.0.1:8766') as client:
        assert client.get("/api/jobs").json()["total"] == 2
        assert client.get("/api/jobs", params={"include_excluded": True}).json()["total"] == 5
        assert client.get("/api/jobs/1").json()["screening"]["excluded"] is True


def test_legacy_archive_without_raw_locations_keeps_existing_country_filter(configured):
    path, config = configured
    with sqlite3.connect(path) as conn:
        archive = json.loads(conn.execute("SELECT archive FROM jobs WHERE job_id=6").fetchone()[0])
        assert "raw_locations" not in archive
        archive["locations"] = ["Remote in Canada"]
        conn.execute("UPDATE jobs SET archive=? WHERE job_id=6", (json.dumps(archive),))
    with QueryService(path, preferences_path=config) as service:
        assert service.jobs(archived=True)["total"] == 0
        assert service.jobs(archived=True, include_excluded=True)["total"] == 1
        assert service.job(6)["screening"]["matched_locations"] == ["Remote in Canada"]


def test_stdlib_script_discovers_preferences_next_to_runtime_database(tmp_path):
    path = database(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "preferences.json").write_text('{"exclude_countries": ["Canada"]}')
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO job_locations (job_id,location_norm) VALUES (2,'toronto, on')")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, "-S", str(root / "scripts/job_view.py"),
                             "--db", str(path), "jobs"], cwd=tmp_path,
                            env={**os.environ, "AUTO_APP_HOME": str(tmp_path)},
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert [j["job_id"] for j in json.loads(result.stdout)["items"]] == [1]


@pytest.mark.parametrize("content", ['{"exclude_countries": "Canada"}', '{"exclude_countrys": ["Canada"]}', '{'])
def test_invalid_preferences_fail_visibly(configured, content):
    path, config = configured
    config.write_text(content)
    with pytest.raises(QueryError, match="preferences"):
        QueryService(path, preferences_path=config)

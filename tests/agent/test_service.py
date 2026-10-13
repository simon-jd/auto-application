"""Agent tools record user decisions against disposable, migrated databases."""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest

from tests.query.test_service import STAMP, add_job


@pytest.fixture
def database(tmp_path):
    from auto_app.core.db.store import DbStore
    from auto_app.settings import Paths

    path = tmp_path / "data" / "agent.db"
    store = DbStore(path, Paths(home=tmp_path))
    store.engine.dispose()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "preferences.json").write_text('{"exclude_countries": ["Canada"]}')
    with sqlite3.connect(path) as conn:
        add_job(conn, 1, "Backend Engineer", "Acme", "linkedin", step2="un_recommended", step2_at=STAMP)
        add_job(conn, 2, "Platform Engineer", "Acme", "ashby")
        add_job(conn, 3, "Canadian Engineer", "Acme", "ashby")
        add_job(conn, 4, "Archived Engineer", "Acme", None, archived_at=STAMP)
        add_job(conn, 5, "Duplicate Engineer", "Acme", "linkedin", duplicate_of=1)
        add_job(conn, 6, "Stale Engineer", "Acme", "linkedin", expires_at="2020-01-01 00:00:00.000000")
        conn.execute("INSERT INTO job_locations (job_id, location_norm) VALUES (3, 'toronto, on')")
        for name in ("backend", "seniority"):
            conn.execute("INSERT INTO filter_versions (filter_version, filter, command, created_at) VALUES (?, ?, '', ?)",
                         (f"{name}-v1", name, STAMP))
            conn.execute("""INSERT INTO judgments (job_id, filter_version, filter, status, recommend, created_at)
                            VALUES (1, ?, ?, 'ok', 0, ?)""", (f"{name}-v1", name, STAMP))
        conn.execute("""INSERT INTO feedback (job_id, filter, filter_version, verdict, correct, label, origin,
                        reason, created_at) VALUES (1, 'seniority', 'seniority-v1', 0, 1, 0, 'explicit',
                        'Too senior', ?)""", (STAMP,))
    return path


def service(database, **kwargs):
    from auto_app.agent.service import AgentService

    return AgentService(database, **kwargs)


def snapshot(path):
    with sqlite3.connect(path) as conn:
        return list(conn.iterdump())


def test_query_tools_preserve_preferences_pagination_evidence_and_database(database):
    before = snapshot(database)
    agent = service(database)
    context = agent.context()
    assert context["preferences"]["exclude_countries"] == ["Canada"]
    assert context["capabilities"]["manual_application_tracking"] is True
    assert context["capabilities"]["automated_submission"] is False
    first = agent.search_jobs(company="Acme", source="ashby", limit=1)
    assert [job["job_id"] for job in first["items"]] == [2]
    assert first["total"] == 1
    assert agent.search_jobs(source="ashby", include_excluded=True)["total"] == 2
    all_jobs = agent.search_jobs(limit=1)
    assert [job["job_id"] for job in all_jobs["items"]] == [6]
    second = agent.search_jobs(limit=1, cursor=all_jobs["next_cursor"])
    assert [job["job_id"] for job in second["items"]] == [5]
    assert agent.get_job(3)["screening"]["excluded"] is True
    assert agent.get_job(3)["parsed"]["description_text"] == "JD for Canadian Engineer"
    assert agent.get_job(9999) is None
    assert snapshot(database) == before


def test_explicit_preferences_path_overrides_runtime_default(database, tmp_path):
    preferences = tmp_path / "other-preferences.json"
    preferences.write_text('{"exclude_countries": []}')
    agent = service(database, preferences_path=preferences)
    assert agent.search_jobs(source="ashby")["total"] == 2


def test_want_to_apply_preserves_explicit_feedback_and_implies_positive_label(database):
    agent = service(database)
    before = agent.get_job(1)
    result = agent.record_application_status(1, "want_to_apply", reason="Interested in this team")
    assert result["success"] is True and result["changed"] is True
    assert result["status"] == "want_to_apply"
    assert result["job"] == agent.get_job(1)
    job = result["job"]
    assert (job["decision"], job["state"], job["step2"]) == ("want_to_apply", "want_to_apply", "un_recommended")
    assert job["step2_at"] == before["step2_at"]
    assert job["decided_at"] is not None
    [review] = job["reviews"]
    assert review["reason"] == "Interested in this team"
    assert {(v["filter"], v["version"], v["recommend"]) for v in review["filters"]} == {
        ("backend", "backend-v1", False), ("seniority", "seniority-v1", False)}
    feedback = {row["filter"]: row for row in job["feedback"]}
    assert (feedback["backend"]["origin"], feedback["backend"]["label"], feedback["backend"]["correct"]) == (
        "implied", True, False)
    assert feedback["seniority"] == before["feedback"][0]
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT job_text FROM reviews WHERE job_id=1").fetchone()[0] == (
            "Backend Engineer\nAcme\nJD for Backend Engineer")


@pytest.mark.parametrize("status", ["want_to_apply", "not_applied", "applied"])
def test_identical_retries_do_not_rewrite_timestamps_or_reviews(database, status):
    agent = service(database)
    first = agent.record_application_status(2, status, reason="My decision")
    before = snapshot(database)
    second = agent.record_application_status(2, status, reason="My decision")
    assert second["success"] is True and second["changed"] is False
    assert second["job"] == first["job"]
    assert snapshot(database) == before


def test_changed_reason_is_audited_without_resetting_when_status_was_decided(database):
    agent = service(database)
    first = agent.record_application_status(2, "not_applied", reason="Location")
    second = agent.record_application_status(2, "not_applied", reason="Location and role fit")
    assert second["changed"] is True
    assert second["job"]["decided_at"] == first["job"]["decided_at"]
    assert [review["reason"] for review in second["job"]["reviews"]] == ["Location", "Location and role fit"]
    assert agent.record_application_status(2, "not_applied", reason="Location and role fit")["changed"] is False


def test_applied_records_manual_outcome_without_creating_application_attempts(database):
    agent = service(database)
    agent.record_application_status(1, "want_to_apply")
    result = agent.record_application_status(1, "applied", reason="Submitted manually on company website")
    assert result["job"]["state"] == "applied"
    assert [row["decision"] for row in result["job"]["reviews"]] == ["want_to_apply", "applied"]
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


@pytest.mark.parametrize("status", ["want_to_apply", "not_applied"])
def test_applied_cannot_be_reverted_through_status_tool(database, status):
    agent = service(database)
    agent.record_application_status(1, "applied")
    before = snapshot(database)
    with pytest.raises(ValueError, match="applied"):
        agent.record_application_status(1, status)
    assert snapshot(database) == before


@pytest.mark.parametrize("job_id, message", [(9999, "missing"), (4, "archived"), (5, "duplicate")])
def test_missing_archived_and_duplicate_jobs_are_rejected_without_writes(database, job_id, message):
    before = snapshot(database)
    with pytest.raises(ValueError, match=message):
        service(database).record_application_status(job_id, "want_to_apply")
    assert snapshot(database) == before


@pytest.mark.parametrize("job_id", [0, -1, True, "1", 9223372036854775808])
def test_invalid_job_ids_are_rejected(database, job_id):
    before = snapshot(database)
    with pytest.raises(ValueError, match="job_id"):
        service(database).record_application_status(job_id, "applied")
    assert snapshot(database) == before


@pytest.mark.parametrize("status", ["recommended", "submitted", "", None, ["applied"]])
def test_only_manual_application_statuses_are_accepted(database, status):
    before = snapshot(database)
    with pytest.raises(ValueError, match="status"):
        service(database).record_application_status(1, status)
    assert snapshot(database) == before


def test_invalid_reason_does_not_partially_write(database):
    before = snapshot(database)
    with pytest.raises(ValueError, match="reason"):
        service(database).record_application_status(1, "want_to_apply", reason={"text": "yes"})
    assert snapshot(database) == before


@pytest.mark.parametrize("table", ["reviews", "feedback"])
def test_status_review_and_feedback_rollback_together(database, table):
    with sqlite3.connect(database) as conn:
        conn.execute(f"CREATE TRIGGER fail_insert BEFORE INSERT ON {table} "
                     "BEGIN SELECT RAISE(ABORT, 'test storage failure'); END")
    before = snapshot(database)
    with pytest.raises(Exception, match="test storage failure"):
        service(database).record_application_status(1, "want_to_apply")
    assert snapshot(database) == before


def test_concurrent_identical_decisions_create_one_review(database):
    barrier = Barrier(2)

    def decide():
        barrier.wait(timeout=5)
        return service(database).record_application_status(2, "want_to_apply", reason="Same request")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: decide(), range(2)))
    assert sorted(result["changed"] for result in results) == [False, True]
    assert len(service(database).get_job(2)["reviews"]) == 1


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_recording_does_not_change_existing_database_journal_mode(database, journal_mode):
    # Concurrent readers/writers already rely on this database-wide setting.
    # Changing DELETE to WAL per request can race with another connection's change.
    with sqlite3.connect(database) as conn:
        assert conn.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0] == journal_mode
    conn.close()
    result = service(database).record_application_status(2, "want_to_apply")
    assert result["job"]["state"] == "want_to_apply"
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == journal_mode


def test_decision_does_not_expire_unrelated_jobs(database):
    agent = service(database)
    before = agent.get_job(6)
    agent.record_application_status(2, "not_applied")
    assert agent.get_job(6) == before


def test_missing_database_is_not_created_or_migrated(tmp_path):
    path = tmp_path / "missing" / "agent.db"
    with pytest.raises(ValueError, match="database"):
        service(path).record_application_status(1, "applied")
    assert not path.parent.exists()


def test_tools_do_not_load_secrets_cli_models_or_browser(database):
    code = '''
import importlib.abc
import json
from pathlib import Path
import sys

blocked = ("auto_app.cli", "auto_app.apply", "anthropic", "claude_agent_sdk", "playwright")
class RejectRuntimeImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError("Forbidden runtime import: " + fullname)

def inspect_open(event, args):
    if event == "open" and isinstance(args[0], str):
        parts = Path(args[0]).parts
        if ".env" in parts or "profile" in parts:
            raise AssertionError("Forbidden secret read")

sys.meta_path.insert(0, RejectRuntimeImports())
sys.addaudithook(inspect_open)
from auto_app.agent.service import AgentService
agent = AgentService(Path(sys.argv[1]))
agent.context()
agent.search_jobs()
agent.get_job(2)
print(json.dumps(agent.record_application_status(2, "applied")))
'''
    result = subprocess.run([sys.executable, "-c", code, str(database)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["job"]["state"] == "applied"

"""Manual application entry points share durable, idempotent timeline accounting."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update
from typer.testing import CliRunner

from auto_app.agent.service import AgentService, apply_manual_decision_in_transaction
from auto_app.apply.policy import ApplyPolicy
from auto_app.apply.queue import Applications
from auto_app.core.db import tables as T
from auto_app.track.events import events_for, weekly_summary
from tests.imported_jobs import ingest, job_id_of, make_store, record

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


@pytest.fixture
def manual_job(tmp_path):
    store = make_store(tmp_path)
    ingest(store, [record("701", "Backend Engineer")])
    job_id = job_id_of(store, "Backend Engineer")
    store.mark_job(job_id, "want_to_apply")
    yield store, job_id, Applications(store, ApplyPolicy(), {})
    store.engine.dispose()


def test_cli_applied_mark_is_counted_once(manual_job, tmp_path, monkeypatch):
    from auto_app.cli import app

    store, job_id, _ = manual_job
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    args = ["job", "mark", str(job_id), "--as", "applied", "--db", str(store.db_path)]
    assert CliRunner().invoke(app, args).exit_code == 0
    original = store.job(job_id)["decided_at"]
    assert CliRunner().invoke(app, args).exit_code == 0
    assert [event["kind"] for event in events_for(store, job_id)] == ["applied"]
    assert store.job(job_id)["decided_at"] == original
    summary = weekly_summary(store, datetime.now(timezone.utc) - timedelta(days=7))
    assert summary["by_kind"] == {"applied": 1}
    assert summary["applied_total"] == 1


def test_manual_inbox_records_one_application_and_event_on_retry(manual_job):
    store, job_id, apps = manual_job
    application_id = apps.applied_by_hand(job_id)
    assert apps.applied_by_hand(job_id) == application_id
    apps.applied(application_id)
    AgentService(store.db_path).record_application_status(job_id, "applied", "same submission")
    assert len(apps.rows()) == len(apps.attempts(application_id)) == 1
    assert apps.get(application_id)["status"] == "submitted"
    assert [event["kind"] for event in events_for(store, job_id)] == ["applied"]
    assert weekly_summary(store, datetime.now(timezone.utc) - timedelta(days=7))["by_kind"] == {"applied": 1}


@pytest.mark.parametrize("entry", ["mark", "manual_inbox", "handoff"])
def test_event_failure_rolls_back_each_manual_entry(manual_job, monkeypatch, entry):
    from auto_app.track import events

    store, job_id, apps = manual_job
    if entry == "handoff":
        [application_id] = apps.enqueue([job_id])
    def fail(*args, **kwargs):
        raise RuntimeError("fictional event failure")
    monkeypatch.setattr(events, "record_event", fail)
    with pytest.raises(RuntimeError, match="event failure"):
        if entry == "mark":
            store.mark_job(job_id, "applied")
        elif entry == "manual_inbox":
            apps.applied_by_hand(job_id)
        else:
            apps.applied(application_id)
    assert store.job(job_id)["decision"] == "want_to_apply"
    assert events_for(store, job_id) == []
    if entry == "handoff":
        assert apps.get(application_id)["status"] == "queued"
        assert apps.attempts(application_id) == []
    else:
        assert apps.rows() == []


def test_agent_metadata_survives_shared_event_accounting(manual_job):
    store, job_id, _ = manual_job
    with store.transaction(immediate=True):
        apply_manual_decision_in_transaction(store, job_id, "applied", "confirmed by owner",
                                             event_source="agent", occurred_at=NOW)
    [event] = events_for(store, job_id)
    assert (event["source"], event["occurred_at"], event["note"]) == (
        "agent", NOW.isoformat(), "confirmed by owner")


def test_weekly_summary_includes_legacy_evidence_without_inventing_dates(manual_job):
    store, job_id, apps = manual_job
    ingest(store, [record("702", "ML Engineer"), record("703", "Data Engineer")])
    undated = job_id_of(store, "ML Engineer")
    recent = job_id_of(store, "Data Engineer")
    [application_id] = apps.enqueue([job_id])
    # Historical records predate event accounting. submitted_at is stronger evidence
    # than a later repeat of the job mark command, which used to reset decided_at.
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).where(T.jobs.c.job_id == job_id).values(decision="applied", decided_at=NOW))
        conn.execute(update(T.applications).where(T.applications.c.application_id == application_id)
                     .values(status="submitted", submitted_at=NOW - timedelta(days=30)))
        conn.execute(update(T.jobs).where(T.jobs.c.job_id == undated).values(decision="applied", decided_at=None))
        conn.execute(update(T.jobs).where(T.jobs.c.job_id == recent)
                     .values(decision="applied", decided_at=NOW - timedelta(days=1)))
    assert weekly_summary(store, NOW - timedelta(days=7)) == {
        "by_kind": {"applied": 1}, "applied_total": 3, "active_pipeline": {}}
    with store.engine.connect() as conn:
        assert conn.execute(select(T.application_events)).all() == []

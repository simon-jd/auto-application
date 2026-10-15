"""A recorded applied decision and its timeline are one durable change."""

import pytest
from sqlalchemy import select

from auto_app.agent.service import AgentService
from auto_app.core.db import tables as T
from tests.imported_jobs import make_store, ingest, record, job_id_of


def fixture_job(tmp_path):
    store = make_store(tmp_path)
    ingest(store, [record("711", "Software Engineer")])
    return store, job_id_of(store, "Software Engineer")


def test_applied_decision_creates_one_manual_event(tmp_path):
    store, job = fixture_job(tmp_path)
    agent = AgentService(tmp_path / "data" / "auto_app.db")
    agent.record_application_status(job, "applied", "reported by owner")
    agent.record_application_status(job, "applied", "reported by owner")
    with store.engine.connect() as conn:
        events = conn.execute(select(T.application_events)).mappings().all()
    assert [(e["job_id"], e["kind"], e["source"]) for e in events] == [(job, "applied", "manual")]
    store.engine.dispose()


def test_event_failure_rolls_back_the_decision_and_review(tmp_path, monkeypatch):
    from auto_app.track import events

    store, job = fixture_job(tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError("fictional event failure")
    monkeypatch.setattr(events, "record_event", fail)
    with pytest.raises(RuntimeError, match="event failure"):
        AgentService(tmp_path / "data" / "auto_app.db").record_application_status(job, "applied")
    assert store.job(job)["decision"] is None
    assert store.reviews(job) == []
    store.engine.dispose()

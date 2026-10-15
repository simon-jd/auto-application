from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from tests.imported_jobs import ingest, job_id_of, make_store, record

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)

@pytest.fixture
def store(tmp_path):
    result = make_store(tmp_path)
    ingest(result, [record("101", "Backend Engineer")])
    yield result
    result.engine.dispose()


def test_events_are_recorded_once_and_listed_in_order(store):
    from auto_app.track.events import events_for, record_event
    job = job_id_of(store, "Backend Engineer")
    first = record_event(store, job, "assessment", occurred_at=NOW, source="email")
    assert first == record_event(store, job, "assessment", occurred_at=NOW + timedelta(hours=2), source="email")
    record_event(store, job, "interview", occurred_at=NOW + timedelta(days=3))
    assert [e["kind"] for e in events_for(store, job)] == ["assessment", "interview"]
    assert events_for(store, job)[0]["occurred_at"] == NOW.isoformat()


def test_record_event_reuses_transaction_and_rolls_back(store):
    from auto_app.track.events import events_for, record_event
    job = job_id_of(store, "Backend Engineer")
    with pytest.raises(RuntimeError):
        with store.transaction(immediate=True):
            record_event(store, job, "interview")
            assert len(events_for(store, job)) == 1
            raise RuntimeError("rollback")
    assert events_for(store, job) == []


@pytest.mark.parametrize("kwargs", [{"job_id": True}, {"job_id": 0}, {"job_id": "1"},
    {"job_id": 99999}, {"kind": "ghosted"}, {"kind": []}, {"source": "bad"},
    {"occurred_at": "2026-10-12"}, {"occurred_at": datetime(2026, 10, 12)}, {"note": 4}])
def test_invalid_events_do_not_write(store, kwargs):
    from auto_app.track.events import events_for, record_event
    job = job_id_of(store, "Backend Engineer")
    with pytest.raises(ValueError):
        record_event(store, **{"job_id": job, "kind": "interview", **kwargs})
    assert events_for(store, job) == []


def test_concurrent_records_share_one_event(store, tmp_path):
    from auto_app.track.events import events_for, record_event
    job = job_id_of(store, "Backend Engineer")
    def record_once(_):
        from auto_app.core.db.store import DbStore
        other = DbStore(store.db_path, store.paths, migrate=False)
        try:
            return record_event(other, job, "interview", occurred_at=NOW)
        finally:
            other.engine.dispose()
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(record_once, range(2)))
    assert ids[0] == ids[1]
    assert len(events_for(store, job)) == 1


def test_get_job_includes_the_timeline(store, tmp_path):
    from auto_app.query.service import QueryService
    from auto_app.track.events import record_event
    job = job_id_of(store, "Backend Engineer")
    record_event(store, job, "rejected", occurred_at=NOW, source="email")
    with QueryService(tmp_path / "data" / "auto_app.db") as query:
        assert [e["kind"] for e in query.job(job)["events"]] == ["rejected"]


def test_weekly_summary_uses_latest_event_and_counts_unique_applications(store):
    from auto_app.track.events import record_event, weekly_summary
    job = job_id_of(store, "Backend Engineer")
    record_event(store, job, "applied", occurred_at=NOW - timedelta(days=10))
    record_event(store, job, "applied", occurred_at=NOW - timedelta(days=8))
    record_event(store, job, "interview", occurred_at=NOW - timedelta(days=1))
    summary = weekly_summary(store, NOW - timedelta(days=7))
    assert summary == {"by_kind": {"interview": 1}, "applied_total": 1, "active_pipeline": {"interview": 1}}
    record_event(store, job, "rejected", occurred_at=NOW)
    assert weekly_summary(store, NOW - timedelta(days=7))["active_pipeline"] == {}

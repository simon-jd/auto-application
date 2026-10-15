"""Tracking migration preserves application history independently of job retention."""

import pytest
from sqlalchemy import inspect, insert, delete
from sqlalchemy.exc import IntegrityError

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from tests.imported_jobs import make_store


def test_tracking_schema_and_mail_event_uniqueness(tmp_path):
    store = make_store(tmp_path)
    try:
        inspector = inspect(store.engine)
        assert {c["name"] for c in inspector.get_columns("mail_updates")} == {
            "update_id", "source", "message_key", "received_at", "sender", "subject", "kind", "job_id", "status", "created_at"}
        assert {c["name"] for c in inspector.get_columns("application_events")} == {
            "event_id", "job_id", "kind", "occurred_at", "source", "note", "mail_update_id", "created_at"}
        now = utcnow()
        with store.engine.begin() as conn:
            conn.execute(insert(T.jobs).values(job_id=701))
            conn.execute(insert(T.application_events).values(job_id=701, kind="rejected", occurred_at=now,
                         source="email", mail_update_id=12, created_at=now))
            conn.execute(delete(T.jobs).where(T.jobs.c.job_id == 701))
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM application_events WHERE job_id=701").scalar() == 1
        with pytest.raises(IntegrityError), store.engine.begin() as conn:
            conn.execute(insert(T.application_events).values(job_id=701, kind="rejected", occurred_at=now,
                         source="email", mail_update_id=12, created_at=now))
    finally:
        store.engine.dispose()

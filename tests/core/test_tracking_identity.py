"""A retained timeline cannot attach to a new job after its old job expires."""
from datetime import timedelta

from alembic import command
from alembic.config import Config
from sqlalchemy import insert, select, inspect

from auto_app.core.db import tables as T
from auto_app.core.db.engine import MIGRATIONS
from auto_app.core.db.job_ttl import expire_jobs, JobTTL
from auto_app.core.db.store import DbStore
from auto_app.core.models import utcnow
from auto_app.track.events import events_for, record_event
from tests.imported_jobs import make_store


def test_old_database_upgrade_preserves_references_and_never_reuses_expired_identity(tmp_path):
    from auto_app.core.db.engine import make_engine
    from auto_app.settings import Paths
    path = tmp_path / "data" / "auto_app.db"
    paths = Paths(home=tmp_path)
    engine = make_engine(path)
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "7c3d9e1f4a20")
        assert "AUTOINCREMENT" not in conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='jobs'").scalar().upper()
        now = utcnow()
        conn.execute(insert(T.jobs).values(job_id=701, company_norm="old", title_norm="Old Engineer", last_seen_at=now - timedelta(days=400)))
        # Handoff history already outlives old jobs before tracking is installed.
        conn.execute(insert(T.resume_artifacts).values(job_id=5010, path="fictional.pdf", sha256="0" * 64, bytes=1, created_at=now))
        conn.execute(insert(T.jobs).values(job_id=702, company_norm="keep", title_norm="Keep Engineer", decision="applied"))
        conn.execute(insert(T.reviews).values(job_id=702, decision="applied", reason="retain", filters=[], created_at=now))
        before_indexes = inspect(conn).get_indexes("jobs")
        before_fk = inspect(conn).get_foreign_keys("jobs")
    engine.dispose()
    upgraded = DbStore(path, paths)
    try:
        with upgraded.engine.connect() as conn:
            assert inspect(conn).get_indexes("jobs") == before_indexes
            assert inspect(conn).get_foreign_keys("jobs") == before_fk
            assert conn.execute(select(T.jobs.c.job_id).order_by(T.jobs.c.job_id)).scalars().all() == [701, 702]
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        record_event(upgraded, 701, "interview", occurred_at=now)
        report = expire_jobs(upgraded, JobTTL(), now=now)
        assert [job.job_id for job in report.deleted] == [701]
        # The allocator also respects retained references above live MAX(job_id).
        with upgraded.engine.begin() as conn:
            conn.execute(insert(T.jobs).values(company_norm="unrelated", title_norm="New Engineer"))
            new_id = conn.execute(select(T.jobs.c.job_id).where(T.jobs.c.company_norm == "unrelated")).scalar_one()
        assert new_id > 5010
        assert events_for(upgraded, new_id) == []
        assert [event["kind"] for event in events_for(upgraded, 701)] == ["interview"]
        with upgraded.engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        upgraded.engine.dispose()


def test_fresh_database_does_not_reuse_its_deleted_highest_job(tmp_path):
    from sqlalchemy import delete
    store = make_store(tmp_path)
    try:
        with store.engine.begin() as conn:
            first = conn.execute(insert(T.jobs).values(company_norm="old")).inserted_primary_key[0]
        record_event(store, first, "interview")
        with store.engine.begin() as conn:
            conn.execute(delete(T.jobs).where(T.jobs.c.job_id == first))
            second = conn.execute(insert(T.jobs).values(company_norm="new")).inserted_primary_key[0]
        assert second > first
        assert events_for(store, second) == []
    finally:
        store.engine.dispose()

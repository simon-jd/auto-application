# tests/core/test_worker_tables.py
"""The Linux worker's tables exist after migrating a new database."""

from sqlalchemy import inspect

from auto_app.core.db.store import DbStore
from auto_app.settings import Paths


def test_enrichment_and_screening_tables_after_migration(tmp_path):
    store = DbStore(tmp_path / "data" / "auto_app.db", Paths(home=tmp_path))
    try:
        insp = inspect(store.engine)
        assert {c["name"] for c in insp.get_columns("enrichments")} == {
            "url", "platform", "status", "attempts", "next_attempt_at", "fetched_at", "data", "error", "updated_at"}
        assert {c["name"] for c in insp.get_columns("job_screenings")} == {
            "job_id", "rules_version", "facts_hash", "outcome", "reasons", "signals", "screened_at"}
        assert any(ix["column_names"] == ["outcome"] for ix in insp.get_indexes("job_screenings"))
    finally:
        store.engine.dispose()


def test_upgrade_preserves_existing_jobs_decisions_and_review_rows(tmp_path):
    import sqlite3
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import insert
    from auto_app.core.db import tables as T
    from auto_app.core.db.engine import MIGRATIONS
    from auto_app.core.models import utcnow

    path = tmp_path / "data" / "auto_app.db"
    store = DbStore(path, Paths(home=tmp_path))
    with store.engine.begin() as conn:
        conn.execute(insert(T.jobs).values(job_id=7, company_norm="fictional", decision="want_to_apply"))
        conn.execute(insert(T.reviews).values(job_id=7, decision="want_to_apply", reason="user chose this",
                                             filters=[], created_at=utcnow()))
    # Use the real downgrade chain, so later migrations do not leave tables behind
    # while the version is artificially reset to the pre-worker revision.
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    with store.engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.downgrade(cfg, "c2f14e8a903b")
    store.engine.dispose()
    with sqlite3.connect(path) as conn:
        before_jobs = conn.execute("SELECT * FROM jobs").fetchall()
        before_reviews = conn.execute("SELECT * FROM reviews").fetchall()
    migrated = DbStore(path, Paths(home=tmp_path))
    migrated.engine.dispose()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == ScriptDirectory.from_config(cfg).get_current_head()
        assert conn.execute("SELECT * FROM jobs").fetchall() == before_jobs
        assert conn.execute("SELECT * FROM reviews").fetchall() == before_reviews
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

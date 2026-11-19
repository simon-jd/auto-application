"""SQLite engine + schema migrations (Alembic, run automatically on open)."""

from pathlib import Path

from sqlalchemy import Engine, create_engine, event

from auto_app.settings import PROJECT_ROOT

MIGRATIONS = PROJECT_ROOT / "migrations"


# How long a connection waits for another writer's lock. Five seconds was too short: enrichment's projection
# write and the feed worker's health writes collided twice on 2026-11-19 ("database is locked").
BUSY_TIMEOUT_MS = 30_000


def make_engine(db_path: Path, *, configure_journal: bool = True) -> Engine:
    """Create an engine; existing-database clients can retain its journal mode.

    Switching DELETE to WAL is database-wide and can conflict with another
    connection doing the same. Request handlers must not reconfigure it.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        if configure_journal:
            cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def upgrade(engine: Engine) -> None:
    """Bring the schema to the latest migration — all of it, or nothing.

    Runs on its own connection that (1) turns foreign-key checks off, because SQLite migrations rebuild
    tables that other tables reference (e.g. dropping a `jobs` column while postings point at jobs), and
    (2) makes DDL transactional: Python's sqlite3 driver otherwise commits before each ALTER/CREATE, so a
    failing migration would leave a half-changed schema behind."""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    migrator = create_engine(engine.url, future=True)

    @event.listens_for(migrator, "connect")
    def _connect(dbapi_conn, _):
        dbapi_conn.isolation_level = None  # the driver stops managing transactions; we BEGIN ourselves
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cur.close()

    @event.listens_for(migrator, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN")

    try:
        with migrator.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            bad = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if bad:
                raise RuntimeError(f"migration left broken foreign keys: {bad[:5]}")
    finally:
        migrator.dispose()

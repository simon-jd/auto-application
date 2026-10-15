from sqlalchemy import inspect

from auto_app.core.db.store import DbStore
from auto_app.settings import Paths


def test_handoff_tables_after_migration(tmp_path):
    store = DbStore(tmp_path / 'data' / 'auto_app.db', Paths(home=tmp_path))
    try:
        inspector = inspect(store.engine)
        assert {c['name'] for c in inspector.get_columns('handoff_claims')} == {
            'job_id', 'agent', 'token_hash', 'claimed_at', 'lease_until'}
        assert {c['name'] for c in inspector.get_columns('resume_artifacts')} == {
            'artifact_id', 'job_id', 'path', 'sha256', 'bytes', 'note', 'created_at'}
    finally:
        store.engine.dispose()

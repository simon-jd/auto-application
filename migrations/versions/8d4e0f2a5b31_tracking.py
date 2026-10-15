"""Application timelines and metadata-only mail updates.

Revision ID: 8d4e0f2a5b31
Revises: 7c3d9e1f4a20
"""

from alembic import op
import sqlalchemy as sa

revision = '8d4e0f2a5b31'
down_revision = '7c3d9e1f4a20'
branch_labels = None
depends_on = None


def upgrade():
    # Timelines/handoff history survive job TTL deletion. SQLite's default
    # INTEGER PRIMARY KEY can recycle a deleted ID; AUTOINCREMENT cannot.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    high = 0
    for name in inspector.get_table_names():
        if any(column["name"] == "job_id" for column in inspector.get_columns(name)):
            table = sa.table(name, sa.column("job_id", sa.Integer()))
            high = max(high, bind.execute(sa.select(sa.func.max(table.c.job_id))).scalar() or 0)
    if "sqlite_sequence" in inspector.get_table_names(sqlite_include_internal=True):
        high = max(high, bind.execute(sa.text("SELECT seq FROM sqlite_sequence WHERE name='jobs'")).scalar() or 0)
    with op.batch_alter_table('jobs', recreate='always', table_kwargs={"sqlite_autoincrement": True}):
        pass
    # Seed beyond retained job references, even when that job no longer exists.
    changed = bind.execute(sa.text("UPDATE sqlite_sequence SET seq=max(seq, :high) WHERE name='jobs'"), {"high": high}).rowcount
    if not changed:
        bind.execute(sa.text("INSERT INTO sqlite_sequence(name,seq) VALUES ('jobs',:high)"), {"high": high})
    op.create_table(
        'mail_updates',
        sa.Column('update_id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('message_key', sa.String(), nullable=False, unique=True),
        sa.Column('received_at', sa.DateTime(timezone=True)),
        sa.Column('sender', sa.Text()),
        sa.Column('subject', sa.Text()),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('job_id', sa.Integer()),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        'application_events',
        sa.Column('event_id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('job_id', sa.Integer(), nullable=False, index=True),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('note', sa.Text()),
        sa.Column('mail_update_id', sa.Integer(), unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    # Keep non-reused job identities after removing tracking: other retained
    # history (handoff/resume records) relies on the same identity guarantee.
    op.drop_table('application_events')
    op.drop_table('mail_updates')

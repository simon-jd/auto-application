"""Cheat-sheet requests: the user's general answers for Claude to add to apply-kit/answers.md (2026-11-18)."""
from alembic import op
import sqlalchemy as sa

revision = 'b8d4f0a2e3c5'
down_revision = 'a7c3e9f1d2b4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('cheatsheet_requests',
        sa.Column('request_id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('agent', sa.String(), nullable=False),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('done_at', sa.DateTime(timezone=True)),
        sa.Column('outcome', sa.String()),
        sa.Column('note', sa.Text()))


def downgrade():
    op.drop_table('cheatsheet_requests')

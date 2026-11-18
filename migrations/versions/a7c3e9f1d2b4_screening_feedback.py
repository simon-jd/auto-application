"""Screening feedback: the user's "shouldn't recommend" reasons from Discord (docs/updates/2026-11-18-phone-control.md)."""
from alembic import op
import sqlalchemy as sa

revision = 'a7c3e9f1d2b4'
down_revision = 'e5a1c9d0b7f2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('screening_feedback',
        sa.Column('feedback_id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('title', sa.Text()),
        sa.Column('company', sa.Text()),
        sa.Column('signals', sa.JSON()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('proposal_id', sa.String()),
        sa.Column('resolved_at', sa.DateTime(timezone=True)),
        sa.Column('resolution', sa.String()),
        sa.Column('resolution_note', sa.Text()),
        sa.Column('posted_at', sa.DateTime(timezone=True)))
    op.create_index('ix_screening_feedback_job_id', 'screening_feedback', ['job_id'])


def downgrade():
    op.drop_index('ix_screening_feedback_job_id', table_name='screening_feedback')
    op.drop_table('screening_feedback')

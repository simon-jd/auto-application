# migrations/versions/5b1e0c7d2a91_enrichment_and_screening.py
"""Linux worker: JD enrichment cache and deterministic screening verdicts.

Revision ID: 5b1e0c7d2a91
Revises: c2f14e8a903b
"""

from alembic import op
import sqlalchemy as sa

revision = '5b1e0c7d2a91'
down_revision = 'c2f14e8a903b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'enrichments',
        sa.Column('url', sa.Text(), primary_key=True),
        sa.Column('platform', sa.String()),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True)),
        sa.Column('fetched_at', sa.DateTime(timezone=True)),
        sa.Column('data', sa.JSON()),
        sa.Column('error', sa.Text()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        'job_screenings',
        sa.Column('job_id', sa.Integer(), primary_key=True),
        sa.Column('rules_version', sa.String(), nullable=False),
        sa.Column('facts_hash', sa.String(), nullable=False),
        sa.Column('outcome', sa.String(), nullable=False),
        sa.Column('reasons', sa.JSON(), nullable=False),
        sa.Column('signals', sa.JSON(), nullable=False),
        sa.Column('screened_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_job_screenings_outcome', 'job_screenings', ['outcome'])


def downgrade():
    op.drop_index('ix_job_screenings_outcome', table_name='job_screenings')
    op.drop_table('job_screenings')
    op.drop_table('enrichments')

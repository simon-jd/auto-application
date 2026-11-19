"""Board proposals: unconnected employer boards offered on Discord each week (2026-11-19)."""
from alembic import op
import sqlalchemy as sa

revision = 'd1f6b2c4e7a8'
down_revision = 'c9e5a1b3f4d6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('board_proposals',
        sa.Column('proposal_id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('platform', sa.String(), nullable=False),
        sa.Column('board', sa.String(), nullable=False),
        sa.Column('company', sa.Text()),
        sa.Column('location', sa.Text(), nullable=False),
        sa.Column('kept_jobs', sa.Integer(), nullable=False),
        sa.Column('applied', sa.Integer(), nullable=False),
        sa.Column('wanted', sa.Integer(), nullable=False),
        sa.Column('skipped', sa.Integer(), nullable=False),
        sa.Column('examples', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('posted_at', sa.DateTime(timezone=True)),
        sa.Column('message_id', sa.String()),
        sa.Column('decision', sa.String()),
        sa.Column('decided_at', sa.DateTime(timezone=True)),
        sa.Column('feed', sa.String()))


def downgrade():
    op.drop_table('board_proposals')

"""Owner questions: what Claude needs the owner to decide, asked and answered on Discord (2026-11-18)."""
from alembic import op
import sqlalchemy as sa

revision = 'c9e5a1b3f4d6'
down_revision = 'b8d4f0a2e3c5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('owner_questions',
        sa.Column('question_id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('job_id', sa.Integer()),
        sa.Column('asked_by', sa.String(), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('context', sa.Text()),
        sa.Column('options', sa.JSON(), nullable=False),
        sa.Column('recommended', sa.Integer()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('posted_at', sa.DateTime(timezone=True)),
        sa.Column('message_id', sa.String()),
        sa.Column('answer', sa.Text()),
        sa.Column('answered_at', sa.DateTime(timezone=True)),
        sa.Column('outcome', sa.String()),
        sa.Column('handled_at', sa.DateTime(timezone=True)),
        sa.Column('handled_note', sa.Text()),
        sa.Column('reported_at', sa.DateTime(timezone=True)))
    op.create_index('ix_owner_questions_job_id', 'owner_questions', ['job_id'])


def downgrade():
    op.drop_index('ix_owner_questions_job_id', table_name='owner_questions')
    op.drop_table('owner_questions')

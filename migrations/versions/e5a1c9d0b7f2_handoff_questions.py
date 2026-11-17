"""Agent handoff: open questions an agent hands back for answers (docs/agent-handoff.md)."""
from alembic import op
import sqlalchemy as sa

revision = 'e5a1c9d0b7f2'
down_revision = '8d4e0f2a5b31'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('handoff_questions',
        sa.Column('question_set_id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('application_id', sa.Integer(), nullable=False),
        sa.Column('agent', sa.String(), nullable=False),
        sa.Column('questions', sa.JSON(), nullable=False),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('answered_at', sa.DateTime(timezone=True)))
    op.create_index('ix_handoff_questions_job_id', 'handoff_questions', ['job_id'])


def downgrade():
    op.drop_index('ix_handoff_questions_job_id', table_name='handoff_questions')
    op.drop_table('handoff_questions')

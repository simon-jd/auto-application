"""Agent handoff: application claims and registered resumes."""
from alembic import op
import sqlalchemy as sa

revision = '7c3d9e1f4a20'
down_revision = '5b1e0c7d2a91'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('handoff_claims',
        sa.Column('job_id', sa.Integer(), primary_key=True),
        sa.Column('agent', sa.String(), nullable=False),
        sa.Column('token_hash', sa.String(), nullable=False, unique=True),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=False))
    op.create_table('resume_artifacts',
        sa.Column('artifact_id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('path', sa.Text(), nullable=False),
        sa.Column('sha256', sa.String(), nullable=False),
        sa.Column('bytes', sa.Integer(), nullable=False),
        sa.Column('note', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_resume_artifacts_job_id', 'resume_artifacts', ['job_id'])


def downgrade():
    op.drop_table('resume_artifacts')
    op.drop_table('handoff_claims')

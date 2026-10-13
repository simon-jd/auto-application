"""Successful mailbox message receipts for resumable scans.

Revision ID: c2f14e8a903b
Revises: a9c17e02b641
"""

from alembic import op
import sqlalchemy as sa

revision = 'c2f14e8a903b'
down_revision = 'a9c17e02b641'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'mail_messages',
        sa.Column('source', sa.String(), primary_key=True),
        sa.Column('provider', sa.String(), primary_key=True),
        sa.Column('account_key', sa.String(), primary_key=True),
        sa.Column('message_id', sa.String(), primary_key=True),
        sa.Column('received_at', sa.DateTime(timezone=True)),
        sa.Column('imported_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('job_links', sa.Integer(), nullable=False),
    )


def downgrade():
    op.drop_table('mail_messages')

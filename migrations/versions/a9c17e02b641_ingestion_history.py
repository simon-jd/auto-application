"""Repository/email observations and content history.

Revision ID: a9c17e02b641
Revises: 4d6750b14a8d
"""

from alembic import op
import sqlalchemy as sa

revision = "a9c17e02b641"
down_revision = "4d6750b14a8d"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ingest_entries",
        sa.Column("source", sa.String(), primary_key=True),
        sa.Column("external_id", sa.String(), primary_key=True),
        sa.Column("posting_id", sa.Integer(), sa.ForeignKey("postings.posting_id", ondelete="SET NULL")),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("revision", sa.String()),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("needs_reassessment", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_ingest_entries_posting_id", "ingest_entries", ["posting_id"])
    op.create_table(
        "ingest_changes",
        sa.Column("change_id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("posting_id", sa.Integer(), sa.ForeignKey("postings.posting_id", ondelete="SET NULL")),
        sa.Column("event", sa.String(), nullable=False),
        sa.Column("revision", sa.String()),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ingest_changes_source_external_id", "ingest_changes", ["source", "external_id"])


def downgrade():
    op.drop_table("ingest_changes")
    op.drop_table("ingest_entries")

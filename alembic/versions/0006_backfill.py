"""Backfill engine: source tracking on files + backfill_jobs table.

- files gains source_channel_id / source_message_id so the backfill worker
  can skip already-indexed source messages before forwarding them.
- backfill_jobs holds chunked, resumable backfill work claimed by indexer
  workers (one row per message-id chunk).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("files", sa.Column("source_channel_id", sa.BigInteger(),
                                     nullable=True))
    op.add_column("files", sa.Column("source_message_id", sa.BigInteger(),
                                     nullable=True))
    op.create_index("ix_files_source", "files",
                    ["source_channel_id", "source_message_id"])

    op.create_table(
        "backfill_jobs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_token", sa.Text(), nullable=False),
        sa.Column("channel_ref", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("channel_title", sa.Text(), nullable=True),
        sa.Column("min_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("max_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("skip", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("offset_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("worker", sa.Integer(), nullable=True),
        sa.Column("stats", JSONB(), server_default=sa.text("'{}'::jsonb"),
                  nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_backfill_jobs_run", "backfill_jobs",
                    ["run_token", "status"])


def downgrade() -> None:
    op.drop_index("ix_backfill_jobs_run", table_name="backfill_jobs")
    op.drop_table("backfill_jobs")
    op.drop_index("ix_files_source", table_name="files")
    op.drop_column("files", "source_message_id")
    op.drop_column("files", "source_channel_id")

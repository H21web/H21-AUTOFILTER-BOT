"""Backfill jobs table — resumable state for channel history backfill."""

revision = "0008_backfill_jobs"
down_revision = "0007_file_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from alembic import op
    import sqlalchemy as sa

    op.create_table(
        "backfill_jobs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("channel_ref", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("skip", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("min_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("max_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("resume_msg_id", sa.BigInteger(), nullable=True),
        sa.Column("scanned", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("forwarded", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("errors", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("error_msg", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_backfill_jobs_status", "backfill_jobs", ["status"])


def downgrade() -> None:
    from alembic import op

    op.drop_index("ix_backfill_jobs_status", table_name="backfill_jobs")
    op.drop_table("backfill_jobs")

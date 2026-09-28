"""Feature tables: tmdb_cache, search_logs, requests, bot_settings.

Also adds ``files.title_key`` (normalized grouping key used for result
cards and new-movie detection) with a best-effort backfill for rows
indexed before this migration.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tmdb_cache",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "search_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("hits", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_search_logs_created_at", "search_logs", ["created_at"])
    op.create_table(
        "requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_name", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending",
                  nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_requests_status", "requests", ["status"])
    op.create_table(
        "bot_settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    op.add_column("files", sa.Column("title_key", sa.Text(), nullable=True))
    op.create_index("ix_files_title_key", "files", ["title_key"])

    # Best-effort backfill: strip extension, quality/lang/year/noise tokens.
    op.execute(
        """
        UPDATE files
        SET title_key = regexp_replace(
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        lower(regexp_replace(file_name, '\\.[a-z0-9]{2,4}$', '')),
                        '\\m(480p|720p|1080p|2160p|4320p|4k|8k)\\M', '', 'g'),
                    '\\m(hindi|malayalam|mallu|tamil|telugu|kannada|english|multi|dual audio|dual-audio)\\M', '', 'g'),
                '\\m(19|20)[0-9]{2}\\M', '', 'g'),
            '\\s+', ' ', 'g')
        WHERE title_key IS NULL;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_files_title_key", table_name="files")
    op.drop_column("files", "title_key")
    op.drop_table("bot_settings")
    op.drop_index("ix_requests_status", table_name="requests")
    op.drop_table("requests")
    op.drop_index("ix_search_logs_created_at", table_name="search_logs")
    op.drop_table("search_logs")
    op.drop_table("tmdb_cache")

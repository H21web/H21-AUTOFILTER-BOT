"""Initial schema: files, users, groups.

- Enables pg_trgm + unaccent extensions.
- files.search_vector is a tsvector kept fresh by a trigger over
  (file_name, caption).
- GIN(search_vector) for full-text search; GIN trgm index on file_name
  for typo-tolerant search.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent;")

    op.create_table(
        "files",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("file_id", sa.Text(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("quality", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_id"),
    )
    op.create_index(
        "ix_files_search_vector",
        "files",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_files_file_name_trgm",
        "files",
        ["file_name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"file_name": "gin_trgm_ops"},
    )

    op.create_table(
        "users",
        sa.Column("user_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("full_name", sa.Text(), nullable=True),
        sa.Column("banned", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "groups",
        sa.Column("group_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("group_id"),
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION files_search_vector_update()
        RETURNS trigger AS $$
        BEGIN
            NEW.search_vector :=
                to_tsvector('english',
                    coalesce(NEW.file_name, '') || ' ' || coalesce(NEW.caption, ''));
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_files_search_vector
        BEFORE INSERT OR UPDATE OF file_name, caption ON files
        FOR EACH ROW EXECUTE FUNCTION files_search_vector_update();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_files_search_vector ON files;")
    op.execute("DROP FUNCTION IF EXISTS files_search_vector_update();")
    op.drop_table("groups")
    op.drop_table("users")
    op.drop_index(
        "ix_files_file_name_trgm", table_name="files", postgresql_using="gin"
    )
    op.drop_index("ix_files_search_vector", table_name="files", postgresql_using="gin")
    op.drop_table("files")

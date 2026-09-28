"""Trigram index on files.caption for word-containment search.

The v1-style matcher requires every query word to appear (case-insensitive)
in ``file_name`` or ``caption``. ``file_name`` already has a pg_trgm GIN
index (0001); this adds the same for ``caption`` so caption-side
``ILIKE '%word%'`` checks stay index-assisted at scale.
"""
from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_files_caption_trgm",
        "files",
        ["caption"],
        postgresql_using="gin",
        postgresql_ops={"caption": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_files_caption_trgm", table_name="files",
                  postgresql_using="gin")

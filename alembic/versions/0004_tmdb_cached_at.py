"""Rename tmdb_cache.created_at -> cached_at to match the ORM model.

Revision 0002 created the column as ``created_at`` while ``app/models.py``
(and ``app/services/tmdb.py``) expect ``cached_at``. The rename fixes the
``UndefinedColumnError`` that crashed the movie-detail (mv:) callback.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("tmdb_cache", "created_at", new_column_name="cached_at",
                    existing_type=sa.DateTime(timezone=True),
                    existing_nullable=False,
                    existing_server_default=sa.func.now())


def downgrade() -> None:
    op.alter_column("tmdb_cache", "cached_at", new_column_name="created_at",
                    existing_type=sa.DateTime(timezone=True),
                    existing_nullable=False,
                    existing_server_default=sa.func.now())

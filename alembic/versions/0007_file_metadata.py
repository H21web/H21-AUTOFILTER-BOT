"""File metadata extracted while indexing (no download needed).

Telegram ships this with every file: video resolution/duration/streaming
flag, audio duration, and message stats (post date, views, forward count).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_COLS = [
    ("width", sa.Integer()),
    ("height", sa.Integer()),
    ("duration", sa.Integer()),
    ("supports_streaming", sa.Boolean()),
    ("posted_at", sa.DateTime(timezone=True)),
    ("views", sa.Integer()),
    ("forwards", sa.Integer()),
]


def upgrade() -> None:
    for name, typ in _COLS:
        op.add_column("files", sa.Column(name, typ, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLS):
        op.drop_column("files", name)

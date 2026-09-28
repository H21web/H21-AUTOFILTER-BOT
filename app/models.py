"""SQLAlchemy ORM models (PostgreSQL)."""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class File(Base):
    """One indexed Telegram file."""

    __tablename__ = "files"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(sa.Text, unique=True, nullable=False)
    file_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    file_size: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    caption: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    channel_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    source_channel_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    """Original channel id when the file arrived via forward (backfill)."""
    source_message_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    """Original message id in the source channel (backfill dedupe)."""
    quality: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    language: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    title_key: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    """Normalized title used for grouping files of the same movie."""
    width: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    """Video width in px (from Telegram metadata, no download)."""
    height: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    """Video height in px."""
    duration: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    """Media duration in seconds."""
    supports_streaming: Mapped[bool | None] = mapped_column(
        sa.Boolean, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True)
    """Original Telegram message date."""
    views: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    """Channel post view count (MTProto only)."""
    forwards: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    """Channel post forward count (MTProto only)."""
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.Index("ix_files_source", "source_channel_id", "source_message_id"),
    )


class TmdbCache(Base):
    """Cached TMDB metadata keyed by normalized title (+year). 30-day TTL."""

    __tablename__ = "tmdb_cache"

    key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    cached_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class SearchLog(Base):
    """Every search query, powering stats + trending."""

    __tablename__ = "search_logs"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    query: Mapped[str] = mapped_column(sa.Text, nullable=False)
    user_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    hits: Mapped[int] = mapped_column(
        sa.Integer, server_default="0", default=0, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.Index("ix_search_logs_created_at", "created_at"),
    )


class BackfillJob(Base):
    """One chunk of a channel backfill, claimed by a single indexer worker.

    Jobs survive restarts: offset_id is checkpointed as the worker walks, so
    a crash resumes mid-chunk instead of from the start.
    """

    __tablename__ = "backfill_jobs"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    run_token: Mapped[str] = mapped_column(sa.Text, nullable=False)
    channel_ref: Mapped[str] = mapped_column(sa.Text, nullable=False)
    channel_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    channel_title: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    min_id: Mapped[int] = mapped_column(sa.BigInteger, server_default="0", default=0, nullable=False)
    max_id: Mapped[int] = mapped_column(sa.BigInteger, server_default="0", default=0, nullable=False)
    skip: Mapped[int] = mapped_column(sa.Integer, server_default="0", default=0, nullable=False)
    """Skip this many media files from the newest end (only on newest chunk)."""
    status: Mapped[str] = mapped_column(
        sa.Text, server_default="pending", default="pending", nullable=False
    )  # pending | running | done | cancelled | error
    offset_id: Mapped[int] = mapped_column(sa.BigInteger, server_default="0", default=0, nullable=False)
    """Resume point: walk continues below this message id."""
    worker: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    """Index of the indexer session working this job."""
    stats: Mapped[dict] = mapped_column(
        JSONB, server_default=sa.text("'{}'::jsonb"), default=dict, nullable=False
    )
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(),
        onupdate=sa.func.now(), nullable=False,
    )

    __table_args__ = (
        sa.Index("ix_backfill_jobs_run", "run_token", "status"),
    )


class MovieRequest(Base):
    """User movie request (from the 'Request this movie' button)."""

    __tablename__ = "requests"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    user_name: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.Text, server_default="pending", default="pending", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.Index("ix_requests_status", "status"),
    )


class BotSetting(Base):
    """Global key/value settings editable from /settings + dashboard."""

    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)


class User(Base):
    """Bot user."""

    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=False)
    full_name: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    banned: Mapped[bool] = mapped_column(
        sa.Boolean, server_default=sa.false(), default=False, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


class Group(Base):
    """Group/chat the bot operates in, with per-group settings."""

    __tablename__ = "groups"

    group_id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=False)
    title: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    settings: Mapped[dict] = mapped_column(
        JSONB,
        server_default=sa.text("'{}'::jsonb"),
        default=dict,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )

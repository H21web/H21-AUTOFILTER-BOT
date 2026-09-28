"""12-factor configuration.

Every setting comes from environment variables — nothing host- or
vendor-specific is hardcoded. Copy ``.env.example`` to ``.env`` for local
development; in production set the variables on the hosting provider.
"""
from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_int_csv(value: str) -> list[int]:
    """Parse a comma-separated string of ints, tolerating blanks."""
    return [int(part.strip()) for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Required ---------------------------------------------------------
    BOT_TOKEN: str
    """Telegram bot token from @BotFather."""

    DATABASE_URL: str
    """PostgreSQL URL. ``postgresql://`` is auto-rewritten to
    ``postgresql+asyncpg://`` for the async driver."""

    WEBHOOK_URL: str
    """Public HTTPS URL Telegram posts updates to, e.g. https://<host>/webhook."""

    # --- Optional ---------------------------------------------------------
    WEBHOOK_SECRET: str = ""
    """Secret sent as ``secret_token`` to setWebhook and verified against the
    ``X-Telegram-Bot-Api-Secret-Token`` header. Empty = no verification."""

    TMDB_API_KEY: str = ""
    """TMDB API key for movie metadata/posters. Empty = metadata disabled."""

    ADMIN_IDS: str = ""
    """Comma-separated Telegram user ids, e.g. "123,456"."""

    INDEXER_IDS: str = ""
    """Comma-separated Telegram user ids trusted to feed files to the bot
    for indexing (e.g. the dedicated indexer account). Same power as an
    admin for PM media indexing only."""

    TG_API_ID: str = ""
    """Telegram API id for the optional MTProto channel indexer."""

    TG_API_HASH: str = ""
    """Telegram API hash for the optional MTProto channel indexer."""

    TG_SESSION: str = ""
    """Telethon StringSession for the indexer account. Empty = the
    server-side auto-index feature is disabled. Generate it locally with
    make-session.py and paste the value into the host's env vars."""

    TG_SESSIONS: str = ""
    """Extra indexer sessions, comma- or newline-separated. Each session is
    one parallel backfill worker (needs its own spare Telegram account).
    Falls back to TG_SESSION when empty."""

    BACKFILL_MODE: str = "direct"
    """Backfill method: "direct" packs Bot API file_ids straight from
    channel history (fastest, ~500-1000 files/sec/account, no PM flood);
    "forward" forwards every file to the bot first (proven fallback)."""

    BACKFILL_DELAY: float = 0.5
    """Seconds between forwarded files, per indexer account (forward mode
    only). Lower = faster but more FloodWaits; 0.5 is the safe sweet spot."""

    BACKFILL_CHUNK: int = 200000
    """Message-id range per backfill job. Smaller chunks = finer resume
    granularity after a crash, more DB rows."""

    INDEX_CHANNELS: str = ""
    """Comma-separated channel ids whose new posts are auto-indexed,
    e.g. "-1001234567890,-1009876543210" (old MoovidexFilterBot CHANNELS
    list logic). Empty = channel auto-index disabled."""

    LOG_CHANNEL: str = ""
    """Channel id where database save errors and unexpected auto-index
    failures are reported. Empty = alerts disabled (console log only)."""

    FORCE_SUB_CHANNELS: str = ""
    """Comma-separated channel ids/usernames users must join, e.g. "@chan,-100123"."""

    MAIN_CHANNEL_ID: str = ""
    """Channel id (e.g. -1001234567890) where new-movie alerts are posted.
    Empty = alerts disabled."""

    AI_API_KEY: str = ""
    """OpenAI-compatible chat API key for smart replies. Empty = templates."""

    AI_BASE_URL: str = "https://api.openai.com/v1"
    """Base URL of the OpenAI-compatible chat API."""

    AI_MODEL: str = "gpt-4o-mini"
    """Chat model used for AI-generated replies."""

    ENABLE_STREAM_PLAYER: bool = True
    """Serve /watch + /dl stream links. Disable to save server bandwidth."""

    STREAM_LINK_TTL_HOURS: int = 6
    """How long signed stream/download links stay valid."""

    ADMIN_USERNAME: str = "admin"
    """Dashboard login username."""

    ADMIN_PASSWORD: str = ""
    """Dashboard login password. Empty = dashboard login disabled."""

    LOG_LEVEL: str = "INFO"
    PORT: int = 8000

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _use_asyncpg_driver(cls, v: str) -> str:
        if isinstance(v, str) and v.startswith("postgresql://"):
            return "postgresql+asyncpg://" + v.removeprefix("postgresql://")
        return v

    @property
    def admin_ids(self) -> list[int]:
        """ADMIN_IDS parsed to a list of ints."""
        return _parse_int_csv(self.ADMIN_IDS)

    @property
    def indexer_ids(self) -> list[int]:
        """INDEXER_IDS parsed to a list of ints."""
        return _parse_int_csv(self.INDEXER_IDS)

    @property
    def force_sub_channels(self) -> list[str]:
        """FORCE_SUB_CHANNELS parsed to a list of raw channel refs."""
        return [p.strip() for p in self.FORCE_SUB_CHANNELS.split(",") if p.strip()]

    @property
    def main_channel_id(self) -> int | None:
        """MAIN_CHANNEL_ID parsed to int, or None when unset/invalid."""
        try:
            return int(self.MAIN_CHANNEL_ID.strip())
        except (ValueError, AttributeError):
            return None

    @property
    def index_channels(self) -> list[int]:
        """INDEX_CHANNELS parsed to a list of chat ids."""
        return _parse_int_csv(self.INDEX_CHANNELS)

    @property
    def log_channel_id(self) -> int | None:
        """LOG_CHANNEL parsed to int, or None when unset/invalid."""
        try:
            return int(self.LOG_CHANNEL.strip())
        except (ValueError, AttributeError):
            return None

    @property
    def tg_sessions(self) -> list[str]:
        """All indexer sessions: TG_SESSIONS split, else [TG_SESSION]."""
        raw = (self.TG_SESSIONS or "").replace("\n", ",")
        sessions = [s.strip() for s in raw.split(",") if s.strip()]
        if not sessions and self.TG_SESSION.strip():
            sessions = [self.TG_SESSION.strip()]
        return sessions


settings = Settings()

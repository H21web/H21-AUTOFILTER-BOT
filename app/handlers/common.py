"""Shared handler helpers: user/group upserts, admin guard, settings cache,
force-subscribe membership checks and the join prompt.

Importing this module must never import other handler modules (avoids
circular imports) — cross-handler calls use deferred imports at call time.
"""
from __future__ import annotations

import logging
from functools import wraps

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from telegram import Update
from telegram.ext import ContextTypes

from app.config import settings
from app.db import get_session_factory
from app.models import BotSetting, Group, User
from app.state import settings_cache_get, settings_cache_set
from app import ui

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ users ---

async def ensure_user(tg_user) -> None:
    """Insert/update the bot user row (idempotent)."""
    if tg_user is None or tg_user.is_bot:
        return
    factory = get_session_factory(settings.DATABASE_URL)
    try:
        async with factory() as session:
            stmt = pg_insert(User).values(
                user_id=tg_user.id,
                full_name=(tg_user.full_name or "")[:200],
            ).on_conflict_do_update(
                index_elements=["user_id"],
                set_={"full_name": (tg_user.full_name or "")[:200]},
            )
            await session.execute(stmt)
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        log.debug("ensure_user failed: %s", exc)


async def ensure_group(chat) -> None:
    """Insert the group row if this is a group/supergroup chat."""
    if chat is None or chat.type not in ("group", "supergroup"):
        return
    factory = get_session_factory(settings.DATABASE_URL)
    try:
        async with factory() as session:
            stmt = pg_insert(Group).values(
                group_id=chat.id, title=(chat.title or "")[:200]
            ).on_conflict_do_nothing(index_elements=["group_id"])
            await session.execute(stmt)
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        log.debug("ensure_group failed: %s", exc)


async def is_banned(user_id: int) -> bool:
    factory = get_session_factory(settings.DATABASE_URL)
    try:
        async with factory() as session:
            row = await session.get(User, user_id)
            return bool(row and row.banned)
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ admin ---

def is_admin(user_id: int | None) -> bool:
    return bool(user_id) and user_id in settings.admin_ids


def admin_only(func):
    """Decorator: restrict a command handler to ADMIN_IDS."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not is_admin(user.id if user else None):
            msg = update.effective_message
            if msg:
                await msg.reply_text("⛔ <b>Admins only.</b>", parse_mode="HTML")
            return
        return await func(update, context)

    return wrapper


# ---------------------------------------------------------------- settings ---

async def get_setting(key: str, default=None):
    """Global bot setting with a 60s in-memory cache (bot_settings table)."""
    cached = settings_cache_get(f"setting:{key}")
    if cached is not None:
        return cached
    factory = get_session_factory(settings.DATABASE_URL)
    try:
        async with factory() as session:
            row = await session.get(BotSetting, key)
            value = row.value.get("v") if row else default
    except Exception:  # noqa: BLE001
        value = default
    settings_cache_set(f"setting:{key}", value)
    return value


async def set_setting(key: str, value) -> None:
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        row = await session.get(BotSetting, key)
        if row:
            row.value = {"v": value}
        else:
            session.add(BotSetting(key=key, value={"v": value}))
        await session.commit()
    from app.state import settings_cache_invalidate
    settings_cache_invalidate(f"setting:{key}")


async def force_sub_enabled() -> bool:
    val = await get_setting("force_sub_enabled", True)
    return bool(val)


async def stream_enabled() -> bool:
    val = await get_setting("stream_enabled", settings.ENABLE_STREAM_PLAYER)
    return bool(val)


async def effective_force_sub_channels() -> list[str]:
    val = await get_setting("force_sub_channels", None)
    if val is None:
        return settings.force_sub_channels
    if isinstance(val, str):
        return [p.strip() for p in val.split(",") if p.strip()]
    return list(val)


async def effective_main_channel() -> int | None:
    val = await get_setting("main_channel_id", None)
    if val is None:
        return settings.main_channel_id
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------- force-sub ---

async def get_missing_channels(bot, user_id: int) -> list[str]:
    """Channels from config the user hasn't joined. Never blocks on errors."""
    if not await force_sub_enabled():
        return []
    channels = await effective_force_sub_channels()
    if not channels:
        return []
    missing: list[str] = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch, user_id)
            if member.status in ("left", "kicked"):
                missing.append(ch)
        except Exception as exc:  # noqa: BLE001 - e.g. bot not admin there
            log.debug("force-sub check failed for %s: %s", ch, exc)
    return missing


async def send_join_prompt(bot, chat_id: int, user_id: int, pending: dict,
                        reply_to: int | None = None) -> None:
    """Store the pending action and ask the user to join channels."""
    from app.state import set_pending

    missing = await get_missing_channels(bot, user_id)
    if not missing:
        return
    pkey = set_pending(user_id, pending)
    await bot.send_message(
        chat_id=chat_id,
        text=ui.join_prompt_text(missing),
        reply_markup=ui.join_keyboard(missing, pkey),
        parse_mode="HTML",
        reply_to_message_id=reply_to,
    )

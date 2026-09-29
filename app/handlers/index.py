"""Auto-index — fresh, simple, crash-proof.

Watches CHANNELS for new documents/videos/audio and saves them to the DB
with native Bot API file_ids.

Features:
- Detects documents, videos, audio (and photos/animations/voice as fallback)
- Only processes channels in the CHANNELS list
- Validates filename before saving
- Copies caption with file details
- Dedupe by file_id (DB unique constraint + ON CONFLICT DO NOTHING)
- DB errors -> alert to LOG_CHANNEL
- All unexpected errors caught — bot never crashes
- Debug logs for monitoring
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import time as _time

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from telegram import Update
from telegram.ext import Application

from app.config import settings
from app.db import get_session_factory
from app.models import File
from app.services.textutil import (
    detect_quality_language,
    title_key,
)

log = logging.getLogger(__name__)


# ------------------------------------------------------------ core save ---

def _valid_file_name(msg, doc) -> str:
    """Extract a usable filename, or '' when invalid."""
    name = getattr(doc, "file_name", None)
    if name and name.strip():
        return name.strip()
    # Fallback: build from caption or mime
    cap = (getattr(msg, "caption", None) or "").strip()
    if cap:
        first = cap.split("\n")[0].strip()[:80]
        if first:
            return first
    mime = getattr(doc, "mime_type", "") or ""
    ext = mime.split("/")[-1] if "/" in mime else "bin"
    return f"file_{getattr(msg, 'message_id', 0)}.{ext}"


async def save_file(msg, doc, bot=None, source_channel_id=None,
                    source_message_id=None, quiet=False) -> tuple[str, str, bool]:
    """Save a file — validate → dedupe → insert.

    Returns (status, file_name, is_new_title):
      status: "saved" | "duplicate" | "invalid" | "error"
    Never raises.
    """
    try:
        file_id = getattr(doc, "file_id", None)
        if not file_id:
            log.debug("save_file: no file_id, skipping")
            return "invalid", "", False

        file_name = _valid_file_name(msg, doc)
        if not file_name:
            log.debug("save_file: invalid filename, skipping")
            return "invalid", "", False

        caption = (getattr(msg, "caption", None) or "").strip() or None
        file_size = getattr(doc, "file_size", None)
        mime_type = getattr(doc, "mime_type", None)
        channel_id = getattr(getattr(msg, "chat", None), "id", None)
        message_id = getattr(msg, "message_id", None)
        posted_at = getattr(msg, "date", None)
        # Video/audio metadata (no download)
        width = getattr(doc, "width", None)
        height = getattr(doc, "height", None)
        duration = getattr(doc, "duration", None)

        quality, language = detect_quality_language(
            f"{file_name} {caption or ''}")
        tk = title_key(file_name)

        # New-title check (for alerts) — cheap EXISTS query
        is_new_title = False
        if not quiet and tk:
            try:
                sf = get_session_factory()
                async with sf() as s:
                    exists = await s.scalar(
                        select(func.count())
                        .select_from(File)
                        .where(File.title_key == tk))
                    is_new_title = not exists
            except Exception:  # noqa: BLE001
                pass

        # Insert with ON CONFLICT DO NOTHING — DB handles dedupe atomically.
        # Fast even with lakhs of rows (unique index on file_id).
        sf = get_session_factory()
        async with sf() as s:
            stmt = pg_insert(File).values(
                file_id=file_id,
                file_name=file_name,
                file_size=file_size,
                mime_type=mime_type,
                caption=caption,
                channel_id=channel_id,
                message_id=message_id,
                source_channel_id=source_channel_id,
                source_message_id=source_message_id,
                quality=quality,
                language=language,
                title_key=tk,
                width=width,
                height=height,
                duration=duration,
                posted_at=posted_at,
            ).on_conflict_do_nothing(index_elements=["file_id"])
            result = await s.execute(stmt)
            await s.commit()
            if result.rowcount == 0:
                log.debug("save_file: duplicate %r", file_name[:60])
                return "duplicate", file_name, False

        log.debug("save_file: saved %r (%s)", file_name[:60], quality)
        return "saved", file_name, is_new_title

    except Exception as exc:  # noqa: BLE001
        log.exception("save_file crashed: %s", exc)
        if bot and not quiet:
            await _alert_log_channel(
                bot, f"⚠️ <b>DB save failed</b>\n<code>{_html.escape(str(exc)[:300])}</code>")
        return "error", "", False


async def _alert_log_channel(bot, text: str) -> None:
    """Send alert to LOG_CHANNEL. Never raises."""
    try:
        cid = settings.log_channel_id
        if cid:
            await bot.send_message(chat_id=cid, text=text, parse_mode="HTML")
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------- auto-index ---

def _get_media(msg):
    """Return the first media attachment, or None."""
    return (msg.document or msg.video or msg.audio or msg.photo
            or msg.animation or msg.voice or msg.video_note)


async def on_channel_post(update: Update, context) -> None:
    """Auto-index new channel posts. Crash-proof wrapper."""
    try:
        msg = update.channel_post
        if not msg:
            return
        if msg.chat_id not in settings.index_channels:
            log.debug("auto-index: ignore post from unlisted %s", msg.chat_id)
            return
        doc = _get_media(msg)
        if doc is None:
            return
        # photo comes as a list — take the largest
        if isinstance(doc, (list, tuple)):
            doc = doc[-1]
        status, file_name, new_title = await save_file(msg, doc, bot=context.bot)
        log.debug("auto-index: %s %r", status, file_name[:60] if file_name else "")
        if status == "saved" and new_title:
            await _post_new_movie_alert(context.bot, file_name)
    except Exception as exc:  # noqa: BLE001
        log.exception("on_channel_post crashed: %s", exc)


async def _post_new_movie_alert(bot, file_name: str) -> None:
    """Alert LOG_CHANNEL about a new title. Never raises."""
    try:
        cid = settings.log_channel_id
        if cid:
            await bot.send_message(
                chat_id=cid,
                text=f"🎬 <b>New:</b> {_html.escape(file_name[:100])}",
                parse_mode="HTML")
    except Exception:  # noqa: BLE001
        pass


async def on_admin_pm_media(update: Update, context) -> None:
    """Index files an admin forwards/sends to the bot in PM.

    Bulk forwards show a live counter (debounced, not per-file spam).
    """
    from app.handlers.common import is_admin
    try:
        user = update.effective_user
        uid = user.id if user else None
        if not (is_admin(uid) or (uid and uid in settings.indexer_ids)):
            return
        msg = update.effective_message
        if not msg:
            return
        doc = _get_media(msg)
        if doc is None:
            return
        if isinstance(doc, (list, tuple)):
            doc = doc[-1]

        is_backfill = bool(uid and uid in settings.indexer_ids)
        src_ch, src_msg_id = None, None
        fo = getattr(msg, "forward_origin", None)
        if fo is not None and getattr(fo, "type", None) == "channel":
            chat = getattr(fo, "chat", None)
            if chat is not None:
                src_ch, src_msg_id = chat.id, getattr(fo, "message_id", None)

        status, file_name, new_title = await save_file(
            msg, doc, bot=context.bot,
            source_channel_id=src_ch, source_message_id=src_msg_id,
            quiet=is_backfill)
        if status == "saved" and new_title:
            await _post_new_movie_alert(context.bot, file_name)

        # Live counter (throttled during backfill floods)
        data = context.chat_data
        data["idx_count"] = data.get("idx_count", 0) + 1
        n = data["idx_count"]
        if is_backfill and n % 25:
            return
        old_task = data.get("idx_task")
        if old_task and not old_task.done():
            old_task.cancel()
        try:
            if "idx_msg_id" in data:
                await context.bot.edit_message_text(
                    f"📥 Indexing… <b>{n}</b>", chat_id=msg.chat_id,
                    message_id=data["idx_msg_id"], parse_mode="HTML")
            else:
                sent = await msg.reply_text(f"📥 Indexing… <b>{n}</b>",
                                            parse_mode="HTML")
                data["idx_msg_id"] = sent.message_id
        except Exception:  # noqa: BLE001
            pass
        data["idx_task"] = asyncio.create_task(
            _finalize_index_summary(context.bot, msg.chat_id, data))
    except Exception as exc:  # noqa: BLE001
        log.exception("on_admin_pm_media crashed: %s", exc)


async def _finalize_index_summary(bot, chat_id: int, chat_data: dict) -> None:
    """Edit the counter message once a burst goes quiet."""
    try:
        await asyncio.sleep(4)
        n = chat_data.pop("idx_count", 0)
        msg_id = chat_data.pop("idx_msg_id", None)
        chat_data.pop("idx_task", None)
        if n and msg_id:
            try:
                await bot.edit_message_text(
                    f"✅ <b>Indexed {n} file(s).</b> Searchable now. 🔍",
                    chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
            except Exception:  # noqa: BLE001
                pass
    except asyncio.CancelledError:
        pass


def register(application: Application) -> None:
    """Register auto-index handlers (no backfill)."""
    from telegram.ext import MessageHandler, filters
    # Channel posts -> auto-index new files
    application.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST, on_channel_post))
    # Admin PM media -> index forwarded/sent files
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.Document.ALL
                                    | filters.VIDEO | filters.AUDIO),
        on_admin_pm_media))

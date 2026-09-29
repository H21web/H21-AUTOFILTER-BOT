"""Channel auto-indexing (old MoovidexFilterBot logic) + manual indexing.

Auto-index working:
- Detects documents/videos/audio posted in the INDEX_CHANNELS list only.
- Validates the file has a usable filename before processing.
- Copies the message caption and saves it with the file details.
- save_file() writes to the DB: explicit duplicate check first, skips
  duplicates without saving again.
- DB save errors are reported to LOG_CHANNEL; unexpected errors are caught
  so the bot never crashes on a bad post.
- Debug details go to the console (processing / duplicates / errors).

Manual admin indexing (unchanged): forward files to the bot in PM.
New-movie alerts still go to MAIN_CHANNEL_ID.
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import re as _re
import time as _time

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import settings
from app.db import get_session_factory
from app.handlers.common import effective_main_channel, is_admin
from app.models import File
from app.services.textutil import (
    clean_title,
    detect_quality_language,
    extract_year,
    title_key,
)
from app.services import tmdb

log = logging.getLogger(__name__)


def _valid_file_name(msg, doc) -> str:
    """Return a usable filename, or "" when there is none.

    Prefers the Telegram file_name; PTB Video/Audio objects carry no
    file_name, so the caption's first line is the fallback (same as before).
    Empty/blank -> the file is skipped, never indexed.
    """
    name = getattr(doc, "file_name", None)
    if name and str(name).strip():
        return str(name).strip()[:500]
    if msg.caption:
        first = msg.caption.strip().split("\n")[0].strip()[:500]
        if first:
            return first
    return ""


async def _is_new_title(session, tk: str, file_id: str) -> bool:
    stmt = (select(func.count())
            .select_from(File)
            .where(File.title_key == tk, File.file_id != file_id))
    return (await session.execute(stmt)).scalar() == 0


async def _alert_log_channel(bot, text: str) -> None:
    """Report an auto-index failure to LOG_CHANNEL (if configured)."""
    channel_id = settings.log_channel_id
    if not channel_id or bot is None:
        return
    try:
        await bot.send_message(channel_id, text, parse_mode="HTML")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not alert LOG_CHANNEL: %s", exc)


async def save_file(msg, doc, bot=None, source_channel_id=None,
                  source_message_id=None, quiet=False) -> tuple[str, str, bool]:
    """Save a file to the DB — validate → duplicate check → insert.

    Mirrors the MoovidexFilterBot (Tech VJ) save_file pattern:
    - Explicit pre-check for duplicates (by file_id)
    - Uses INSERT ... ON CONFLICT DO NOTHING RETURNING id so we can
      reliably detect whether a row was actually written (asyncpg always
      returns rowcount=-1 for ON CONFLICT DO NOTHING without RETURNING).
    - DB errors are reported to LOG_CHANNEL and never crash the bot.

    source_channel_id/message_id track where a forwarded file came from
    (backfill dedupe). quiet=True skips the new-title alert (backfill floods).
    Returns (status, file_name, is_new_title); status is one of:
        "saved"     - new row inserted
        "duplicate" - file_id already in DB, skipped without saving
        "no_name"   - no valid filename, skipped
        "error"     - DB error (alert sent to LOG_CHANNEL)
    Always returns 3 values, even on failure.
    """
    file_name = _valid_file_name(msg, doc)
    if not file_name:
        log.debug("auto-index skip: no valid filename (chat %s msg %s)",
                  getattr(msg, 'chat_id', '?'), getattr(msg, 'message_id', '?'))
        return "no_name", "", False

    file_id = doc.file_id
    log.debug("auto-index processing %r (chat %s msg %s)",
              file_name, getattr(msg, 'chat_id', '?'),
              getattr(msg, 'message_id', '?'))

    factory = get_session_factory(settings.DATABASE_URL)
    try:
        async with factory() as session:
            # Explicit duplicate check first — same as Tech VJ's save_file.
            exists = (await session.execute(
                select(File.id).where(File.file_id == file_id).limit(1)
            )).first()
            if exists:
                log.debug("auto-index skip duplicate %r", file_name)
                return "duplicate", file_name, False

            caption = (msg.caption or "")[:1000]
            quality, language = detect_quality_language(f"{file_name} {caption}")
            tk = title_key(file_name)

            # INSERT … ON CONFLICT DO NOTHING RETURNING id
            # asyncpg always yields rowcount=-1 without RETURNING, so we must
            # use fetchone() to detect whether the row was actually inserted.
            stmt = (
                pg_insert(File)
                .values(
                    file_id=file_id,
                    file_name=file_name,
                    file_size=getattr(doc, "file_size", None),
                    mime_type=getattr(doc, "mime_type", None),
                    caption=caption or None,
                    channel_id=getattr(msg, 'chat_id', None),
                    message_id=getattr(msg, 'message_id', None),
                    source_channel_id=source_channel_id,
                    source_message_id=source_message_id,
                    quality=quality,
                    language=language,
                    title_key=tk or None,
                    width=getattr(doc, "width", None),
                    height=getattr(doc, "height", None),
                    duration=getattr(doc, "duration", None),
                    supports_streaming=getattr(doc, "supports_streaming", None),
                    posted_at=getattr(msg, "date", None),
                    # views/forwards only exist via MTProto, not the Bot API
                    views=None,
                    forwards=None,
                )
                .on_conflict_do_nothing(index_elements=["file_id"])
                .returning(File.id)  # <- REQUIRED: rowcount is -1 without this
            )
            result = await session.execute(stmt)
            inserted_id = result.fetchone()  # None if conflict (duplicate)
            await session.commit()

            if inserted_id is None:
                # ON CONFLICT fired — a concurrent insert beat us.
                log.debug("auto-index skip duplicate (race) %r", file_name)
                return "duplicate", file_name, False

            log.info("auto-index saved %r (id=%s)", file_name, inserted_id[0])

        # Check new-title in a fresh session (previous one is already closed).
        if (not quiet) and bool(tk):
            async with factory() as session2:
                new_title = await _is_new_title(session2, tk, file_id)
        else:
            new_title = False

        return "saved", file_name, bool(new_title)

    except Exception as exc:  # noqa: BLE001
        log.warning("auto-index DB error for %r: %s", file_name, exc)
        await _alert_log_channel(
            bot,
            "🗄️ <b>Auto-index DB error</b>\n"
            f"📄 {_html.escape(file_name)}\n"
            f"🆔 <code>{getattr(msg, 'chat_id', '?')}</code> "
            f"/ msg {getattr(msg, 'message_id', '?')}\n"
            f"❌ <code>{_html.escape(str(exc))}</code>")
        return "error", file_name, False


async def _post_new_movie_alert(bot, file_name: str) -> None:
    channel_id = await effective_main_channel()
    if not channel_id:
        return
    title = clean_title(file_name) or "New upload"
    year = extract_year(file_name)
    meta = await tmdb.get_movie(title, year)
    try:
        me = await bot.get_me()
        bot_username = me.username or ""
    except Exception:  # noqa: BLE001
        bot_username = ""

    display = (meta or {}).get("title") or title
    year_s = f" ({(meta or {}).get('year') or year or ''})".rstrip(" ()")
    if year_s == " ()":
        year_s = ""
    rating = (meta or {}).get("rating") or 0
    rating_s = f" ⭐ <b>{rating}</b>" if rating else ""
    plot = ((meta or {}).get("plot") or "")[:250]
    plot_s = f"\n\n📝 {plot}" if plot else ""
    text = (f"🆕 <b>{display}</b>{year_s}{rating_s}{plot_s}\n\n"
            f"✅ Now available — search for it in the bot! 👇")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🤖 Search Movies",
                             url=f"https://t.me/{bot_username}?start=search"
                             if bot_username else "https://t.me/"),
    ]])
    try:
        poster = (meta or {}).get("poster_url")
        if poster:
            await bot.send_photo(channel_id, photo=poster, caption=text,
                                 reply_markup=kb, parse_mode="HTML")
        else:
            await bot.send_message(channel_id, text, reply_markup=kb,
                                   parse_mode="HTML")
        log.info("new-movie alert posted for %r", title)
    except Exception as exc:  # noqa: BLE001
        log.warning("new-movie alert failed: %s", exc)


async def on_channel_post(update: Update,
                          context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-index new channel posts — Tech VJ / MoovidexFilterBot logic.

    Matches the reference index.py behaviour:
    - Only processes channels listed in INDEX_CHANNELS.
    - Accepts document, video, audio, and video_note attachments.
    - Calls save_file() which does a proper duplicate check + RETURNING insert.
    - Posts a new-movie alert to MAIN_CHANNEL_ID on the first file of a title.
    - The whole handler is wrapped so one bad post never crashes the bot.
    """
    try:
        msg = update.channel_post
        if not msg:
            return
        if not settings.index_channels:
            # INDEX_CHANNELS not set — auto-index is disabled.
            return
        if msg.chat_id not in settings.index_channels:
            log.debug("auto-index ignore post from unlisted channel %s",
                      msg.chat_id)
            return
        # Accept all media types the reference bot accepts.
        doc = msg.document or msg.video or msg.audio or msg.video_note
        if doc is None:
            return
        status, file_name, new_title = await save_file(
            msg, doc, bot=context.bot)
        if status == "saved":
            log.info("auto-index [%s] saved %r", msg.chat_id, file_name)
            if new_title:
                await _post_new_movie_alert(context.bot, file_name)
        elif status == "duplicate":
            log.debug("auto-index [%s] duplicate %r", msg.chat_id, file_name)
        elif status == "no_name":
            log.debug("auto-index [%s] no_name msg %s",
                      msg.chat_id, msg.message_id)
        # "error" is already logged + alerted inside save_file.
    except Exception as exc:  # noqa: BLE001 - never let a post kill the bot
        log.exception("auto-index unexpected error (caught): %s", exc)
        try:
            await _alert_log_channel(
                context.bot,
                "⚠️ <b>Auto-index unexpected error</b>\n"
                f"❌ <code>{_html.escape(str(exc))}</code>")
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------- manual admin indexing ---

async def _finalize_index_summary(bot, chat_id: int, chat_data: dict) -> None:
    """Edit the debounced status message once a forward burst goes quiet."""
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


async def on_admin_pm_media(update: Update,
                            context: ContextTypes.DEFAULT_TYPE) -> None:
    """Index files an admin sends/forwards to the bot in PM.

    Supports bulk multi-forward: a live counter message is updated instead
    of spamming one reply per file.
    """
    user = update.effective_user
    uid = user.id if user else None
    if not (is_admin(uid) or (uid and uid in settings.indexer_ids)):
        return
    msg = update.effective_message
    if not msg:
        return
    doc = msg.document or msg.video or msg.audio
    if doc is None:
        return

    # Backfill floods come from the indexer account(s): extract the original
    # channel/message from the forward header for source tracking, save
    # quietly (no per-file alerts/counters at 2+ files/sec).
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

    data = context.chat_data
    data["idx_count"] = data.get("idx_count", 0) + 1
    n = data["idx_count"]
    # throttle counter edits during backfill floods (every 25 files)
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
            status = await msg.reply_text(f"📥 Indexing… <b>{n}</b>",
                                          parse_mode="HTML")
            data["idx_msg_id"] = status.message_id
    except Exception:  # noqa: BLE001
        pass
    data["idx_task"] = asyncio.create_task(
        _finalize_index_summary(context.bot, msg.chat_id, data))




# ------------------------------------------ fresh /index (simple + fast) ---

def _setup_text() -> str:
    return (
        "⚠️ <b>Channel indexer isn't configured.</b>\n\n"
        "Set these env vars and restart:\n"
        "1. <code>INDEXER_BOT_MODE=true</code>\n"
        "2. <code>BOT_TOKEN</code>, <code>TG_API_ID</code>, "
        "<code>TG_API_HASH</code>\n"
        "3. <code>INDEX_CHANNELS</code> — dump channel ID (bot must be admin)\n\n"
        "The bot must also be a member of the source channel.")


async def index_command(update: Update,
                        context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fresh /index — simple channel backfill.

    /index <channel> — start indexing (admin only)
    /index — show status
    /index cancel — stop the running index
    """
    from app.services import indexer as idx

    user = update.effective_user
    uid = user.id if user else 0
    msg = update.effective_message
    if not is_admin(uid):
        await msg.reply_text(
            "⛔ Admins only. Search-il movie name adicholu. 🔍",
            parse_mode="HTML")
        return

    args = context.args or []
    if not args:
        st = idx.run_status()
        if st:
            s = st.get("stats", {})
            await msg.reply_text(
                f"📥 <b>Indexing:</b> {_html.escape(st.get('title', ''))}\n"
                f"🔍 Scanned: <b>{s.get('scanned', 0)}</b> | "
                f"📤 Forwarded: <b>{s.get('forwarded', 0)}</b>\n\n"
                f"<i>/index cancel — to stop</i>",
                parse_mode="HTML")
        else:
            await msg.reply_text(
                "📥 <b>Channel Indexer</b>\n\n"
                "Usage: <code>/index @channel</code> or "
                "<code>/index https://t.me/channel</code>\n\n"
                "The bot walks the channel history and forwards media to "
                "the dump channel — files get indexed with native Bot API "
                "file_ids automatically.",
                parse_mode="HTML")
        return

    if args[0].lower() == "cancel":
        stopped = await idx.cancel_run()
        await msg.reply_text(
            "🛑 Indexing cancelled." if stopped else "No active index run.")
        return

    if not idx.indexer_configured():
        await msg.reply_text(_setup_text(), parse_mode="HTML")
        return

    if idx.run_status():
        await msg.reply_text(
            "⚠️ An index run is already active. "
            "<i>/index cancel</i> to stop it first.",
            parse_mode="HTML")
        return

    raw_ref = " ".join(args)
    wait = await msg.reply_text("🔍 <i>Resolving channel…</i>",
                                parse_mode="HTML")
    try:
        info = await idx.resolve_channel(raw_ref)
    except Exception as exc:  # noqa: BLE001
        await wait.edit_text(
            f"❌ Couldn't read that channel: "
            f"<code>{_html.escape(str(exc)[:200])}</code>\n"
            "Make sure the bot is a member of the channel.",
            parse_mode="HTML")
        return

    await wait.edit_text(
        f"📥 <b>Indexing:</b> {_html.escape(info['title'])}\n"
        f"🔢 Last message: <b>{info['last_msg_id']}</b>\n\n"
        f"<i>Forwarding media to dump channel…</i>\n"
        f"<i>/index cancel — to stop</i>",
        parse_mode="HTML")

    async def _progress(s: dict):
        try:
            await wait.edit_text(
                f"📥 <b>Indexing:</b> {_html.escape(s.get('title', info['title']))}\n"
                f"🔍 Scanned: <b>{s.get('scanned', 0)}</b> | "
                f"📤 Forwarded: <b>{s.get('forwarded', 0)}</b> | "
                f"⚠️ Errors: <b>{s.get('errors', 0)}</b>",
                parse_mode="HTML")
        except Exception:  # noqa: BLE001
            pass

    res = await idx.start_run(raw_ref, progress_cb=_progress)
    if res.get("error") == "already_running":
        await wait.edit_text("⚠️ An index run is already active.")
        return

    # Wait for completion and show final stats.
    run = idx._current_run
    if run:
        try:
            await run["task"]
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    st = (idx._current_run or {}).get("stats", {}) if idx._current_run else {}
    # _current_run may be cleared; use the last progress stats.
    final = getattr(_progress, "_last", None) or st
    try:
        await wait.edit_text(
            f"✅ <b>Index done:</b> {_html.escape(info['title'])}\n"
            f"🔍 Scanned: <b>{final.get('scanned', 0)}</b>\n"
            f"📤 Forwarded: <b>{final.get('forwarded', 0)}</b>\n"
            f"⚠️ Errors: <b>{final.get('errors', 0)}</b>\n\n"
            f"<i>Files are being indexed from the dump channel…</i>",
            parse_mode="HTML")
    except Exception:  # noqa: BLE001
        pass


async def _on_idx_cancel_cmd(update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> None:
    """Legacy /cancel hook — cancels an active index run."""
    from app.services import indexer as idx
    user = update.effective_user
    if not is_admin(user.id if user else 0):
        return
    stopped = await idx.cancel_run()
    await update.effective_message.reply_text(
        "🛑 Indexing cancelled." if stopped else "No active index run.")


def register(application: Application) -> None:
    from telegram.ext import CommandHandler, MessageHandler, filters
    application.add_handler(CommandHandler("index", index_command))
    application.add_handler(CommandHandler("cancel", _on_idx_cancel_cmd))
    # Channel posts -> auto-index
    application.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST, on_channel_post))
    # Admin PM media -> index
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.Document.ALL
                                    | filters.VIDEO | filters.AUDIO),
        on_admin_pm_media))

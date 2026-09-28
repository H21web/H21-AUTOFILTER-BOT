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
    """Old-bot style save: validate -> duplicate check -> insert.

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
                  msg.chat_id, msg.message_id)
        return "no_name", "", False

    file_id = doc.file_id
    log.debug("auto-index processing %r (chat %s msg %s)",
              file_name, msg.chat_id, msg.message_id)

    factory = get_session_factory(settings.DATABASE_URL)
    try:
        async with factory() as session:
            # explicit duplicate check first (old-bot logic)
            exists = (await session.execute(
                select(File.id).where(File.file_id == file_id).limit(1)
            )).first()
            if exists:
                log.debug("auto-index skip duplicate %r", file_name)
                return "duplicate", file_name, False

            caption = (msg.caption or "")[:1000]
            quality, language = detect_quality_language(f"{file_name} {caption}")
            tk = title_key(file_name)
            stmt = pg_insert(File).values(
                file_id=file_id,
                file_name=file_name,
                file_size=getattr(doc, "file_size", None),
                mime_type=getattr(doc, "mime_type", None),
                caption=caption or None,
                channel_id=msg.chat_id,
                message_id=msg.message_id,
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
            ).on_conflict_do_nothing(index_elements=["file_id"])
            result = await session.execute(stmt)
            await session.commit()
            if not (result.rowcount or 0):
                # lost a race with a concurrent insert -> treat as duplicate
                log.debug("auto-index skip duplicate (race) %r", file_name)
                return "duplicate", file_name, False
            log.debug("auto-index saved %r", file_name)
            new_title = ((not quiet) and bool(tk)
                         and await _is_new_title(session, tk, file_id))
            return "saved", file_name, bool(new_title)
    except Exception as exc:  # noqa: BLE001
        log.warning("auto-index DB error for %r: %s", file_name, exc)
        await _alert_log_channel(
            bot,
            "🗄️ <b>Auto-index DB error</b>\n"
            f"📄 {_html.escape(file_name)}\n"
            f"🆔 <code>{msg.chat_id}</code> / msg {msg.message_id}\n"
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
    """Auto-index new channel posts — old MoovidexFilterBot logic.

    Only channels in INDEX_CHANNELS are processed. The whole handler is
    wrapped so one bad post can never crash the bot.
    """
    try:
        msg = update.channel_post
        if not msg:
            return
        if msg.chat_id not in settings.index_channels:
            log.debug("auto-index ignore post from unlisted channel %s",
                      msg.chat_id)
            return
        doc = msg.document or msg.video or msg.audio
        if doc is None:
            return
        status, file_name, new_title = await save_file(msg, doc,
                                                       bot=context.bot)
        log.debug("auto-index result: %s %r", status, file_name)
        if status == "saved" and new_title:
            await _post_new_movie_alert(context.bot, file_name)
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


async def index_command(update: Update,
                        context: ContextTypes.DEFAULT_TYPE) -> None:
    """/index — admin: control panel (or /index <channel> quick backfill).

    Non-admins (PM only): request a channel for indexing — moderators
    approve/reject in LOG_CHANNEL.
    """
    user = update.effective_user
    uid = user.id if user else 0
    if not is_admin(uid):
        if update.effective_chat.type != "private":
            await update.effective_message.reply_text(
                "💡 <i>PM-il vannu /index adicholu.</i>", parse_mode="HTML")
            return
        await _start_index_request(update, context)
        return
    if context.args:
        await _quick_index_flow(update, context, " ".join(context.args))
        return
    text, kb = await _panel_data(uid)
    msg = await update.effective_message.reply_text(
        text, parse_mode="HTML", reply_markup=kb)
    if user:
        _IDX_PANEL[uid] = (msg.chat_id, msg.message_id)


# ------------------------------------------ server-side channel backfill ---

import secrets as _secrets

_PENDING_INDEX: dict[str, dict] = {}
_INDEX_RUNS: dict[str, dict] = {}
_INDEX_CFG: dict[int, dict] = {}
_IDX_AWAIT: dict[int, str] = {}
_IDX_PANEL: dict[int, tuple[int, int]] = {}


def _get_cfg(admin_id: int) -> dict:
    """Per-admin backfill settings (in-memory; resets on bot restart)."""
    return _INDEX_CFG.setdefault(
        admin_id,
        {"channels": [], "skip": 0, "from_id": 0, "to_id": 0, "limit": 0},
    )


def _cfg_summary(cfg: dict) -> str:
    rng = (f"{cfg['from_id']}–{cfg['to_id']}"
           if cfg["from_id"] or cfg["to_id"] else "full history")
    lim = str(cfg["limit"]) if cfg["limit"] else "no limit"
    return (f"📡 Channels: <b>{len(cfg['channels'])}</b>\n"
            f"⏭️ Skip first: <b>{cfg['skip']}</b> file(s) per channel\n"
            f"↔️ Range: <b>{rng}</b>\n"
            f"🔢 Max files: <b>{lim}</b>")


def _setup_text() -> str:
    return (
        "⚠️ <b>Channel backfill isn't configured.</b>\n\n"
        "The server needs a dedicated indexer account:\n"
        "1. Run <code>make-session.py</code> on your PC with a spare Telegram account.\n"
        "2. Set <code>TG_SESSION</code>, <code>TG_API_ID</code>, "
        "<code>TG_API_HASH</code>, <code>INDEXER_IDS</code> in the site's "
        "environment and restart.\n"
        "3. Have that account /start the bot once.\n\n"
        "Until then, forward files to me in PM to index them.")


async def _panel_data(admin_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from app.services import mtproto_index
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        total = (await session.execute(select(func.count())
                                       .select_from(File))).scalar() or 0
        channels = (await session.execute(
            select(func.count(func.distinct(File.channel_id)))
        )).scalar() or 0
    cfg = _get_cfg(admin_id)
    auto = ("✅ ready"
            if mtproto_index.indexer_configured() else
            "⚠️ not configured — set TG_SESSION on the server")
    text = (
        "📥 <b>Index control panel</b>\n\n"
        "1️⃣ <b>Auto</b> — new files posted in INDEX_CHANNELS are indexed "
        "automatically (set the env var; empty = channel auto-index off).\n"
        "2️⃣ <b>Manual</b> — forward files/channel posts to me here in PM "
        "(multi-select up to 100 at once), or upload files directly.\n"
        f"3️⃣ <b>Backfill</b> — {auto}.\n\n"
        f"{_cfg_summary(cfg)}\n\n"
        f"📁 Files indexed: <b>{total}</b>  •  📡 Source chats: <b>{channels}</b>")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add channel", callback_data="idx:add"),
         InlineKeyboardButton(f"📋 Channels ({len(cfg['channels'])})",
                              callback_data="idx:list")],
        [InlineKeyboardButton(f"⏭ Skip: {cfg['skip']}",
                              callback_data="idx:skip"),
         InlineKeyboardButton("↔️ Range", callback_data="idx:range")],
        [InlineKeyboardButton(f"🔢 Limit: {cfg['limit'] or '∞'}",
                              callback_data="idx:limit"),
         InlineKeyboardButton("🔄 Reset", callback_data="idx:reset")],
        [InlineKeyboardButton("▶️ Start indexing", callback_data="idx:start")],
    ])
    return text, kb


async def _edit_panel(query, admin_id: int) -> None:
    text, kb = await _panel_data(admin_id)
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:  # noqa: BLE001
        pass


def _bar(pct: float, width: int = 12) -> str:
    pct = max(0.0, min(1.0, pct))
    fill = int(round(pct * width))
    return "▓" * fill + "░" * (width - fill)


async def _quick_index_flow(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            raw_ref: str) -> None:
    """Resolve one channel and ask for confirmation (admin only)."""
    from app.services import mtproto_index
    msg = update.effective_message
    uid = update.effective_user.id if update.effective_user else 0
    if not mtproto_index.indexer_configured():
        await msg.reply_text(_setup_text(), parse_mode="HTML")
        return
    ref, hint = mtproto_index.parse_channel_ref(raw_ref)
    wait = await msg.reply_text("🔍 <i>Resolving channel…</i>", parse_mode="HTML")
    try:
        info = await mtproto_index.resolve_channel(ref)
    except Exception as exc:  # noqa: BLE001
        await wait.edit_text(
            f"❌ Couldn't read that channel: <code>{_html.escape(str(exc))}</code>\n"
            "Make sure the indexer account can access it.", parse_mode="HTML")
        return
    last = hint or info["last_msg_id"]
    cfg = _get_cfg(uid)
    token = _secrets.token_hex(4)
    _PENDING_INDEX[token] = {"channels": [ref], "cfg": dict(cfg),
                             "chat_id": msg.chat_id}
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, index it",
                              callback_data=f"idx:go:{token}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"idx:no:{token}")],
    ])
    await wait.edit_text(
        f"📥 <b>Index this channel?</b>\n\n"
        f"📌 {_html.escape(info['title'])}\n"
        f"🆔 <code>{info['chat_id']}</code>\n"
        f"🔢 Last message: <b>{last}</b>\n\n"
        f"{_cfg_summary(cfg)}\n\n"
        "<i>Every video/audio/document will be indexed directly from history. "
        "Change skip/range/limit from the /index panel first if needed.</i>",
        parse_mode="HTML", reply_markup=kb)


async def _on_index_callback(update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    uid = user.id if user else None
    if not query or not is_admin(uid):
        if query:
            await query.answer("⛔ Admins only.", show_alert=True)
        return
    await query.answer()
    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""

    if action == "cancel":
        # cancel a running backfill (workers + DB jobs)
        from app.services import mtproto_index
        run = _INDEX_RUNS.get(arg)
        if run:
            run["event"].set()
        try:
            await mtproto_index.cancel_run(arg)
        except Exception:  # noqa: BLE001
            pass
        await query.answer("Cancelling…", show_alert=True)
        return

    if action in ("reqaccept", "reqreject"):
        # moderator decision on a user-submitted index request
        req = _REQ_INDEX.pop(arg, None)
        if not req:
            await query.answer("⚠️ Expired.", show_alert=True)
            return
        mod = (update.effective_user.full_name
               if update.effective_user else "moderator")
        title = req["title"]
        if action == "reqreject":
            try:
                await query.edit_message_text(
                    f"❌ <b>Rejected</b> by {_html.escape(mod)}\n"
                    f"📌 {_html.escape(title)}",
                    parse_mode="HTML")
            except Exception:  # noqa: BLE001
                pass
            try:
                await context.bot.send_message(
                    req["from_user"],
                    f"❌ Your indexing request for <b>{_html.escape(title)}</b> "
                    f"was declined by the moderators.",
                    parse_mode="HTML")
            except Exception:  # noqa: BLE001
                pass
            await query.answer("Rejected.")
            return
        # accepted: queue the channel into the parallel backfill engine
        try:
            await query.edit_message_text(
                f"✅ <b>Accepted</b> by {_html.escape(mod)} — indexing…\n"
                f"📌 {_html.escape(title)}",
                parse_mode="HTML")
        except Exception:  # noqa: BLE001
            pass
        try:
            await context.bot.send_message(
                req["from_user"],
                f"✅ Your request for <b>{_html.escape(title)}</b> was "
                f"<b>accepted</b>! Files will be indexed soon.",
                parse_mode="HTML")
        except Exception:  # noqa: BLE001
            pass
        token = _secrets.token_hex(4)
        await query.answer("Accepted — starting backfill.")
        await _begin_backfill(
            context, token, [req["ref"]],
            {"channels": [req["ref"]], "skip": 0, "from_id": 0,
             "to_id": 0, "limit": 0},
            chat_id=settings.log_channel_id)
        return

    cfg = _get_cfg(uid)

    if action == "add":
        if query.message.chat.type != "private":
            await query.answer("Open my PM to add channels.", show_alert=True)
            return
        _IDX_AWAIT[uid] = "channel"
        await query.message.reply_text(
            "📡 <b>Send the channel</b>\n"
            "• Private channel → its <b>-100 ID</b> "
            "(e.g. <code>-1001234567890</code>),\n"
            "  or copy any message link from it (<code>t.me/c/…/…</code>)\n"
            "• Public channel → @username or t.me link\n"
            "Send /cancel to abort.", parse_mode="HTML")
        return

    if action == "list":
        if not cfg["channels"]:
            await query.answer("No channels added yet — tap ➕ Add channel.",
                               show_alert=True)
            return
        rows = [[InlineKeyboardButton(f"❌ {c}", callback_data=f"idx:rm:{i}")]
                for i, c in enumerate(cfg["channels"])]
        rows.append([InlineKeyboardButton("◀️ Back", callback_data="idx:back")])
        await query.edit_message_text(
            "📡 <b>Channels queued:</b>\n<i>Tap ❌ to remove one.</i>",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == "rm":
        try:
            cfg["channels"].pop(int(arg))
        except (ValueError, IndexError):
            pass
        if cfg["channels"]:
            rows = [[InlineKeyboardButton(f"❌ {c}", callback_data=f"idx:rm:{i}")]
                    for i, c in enumerate(cfg["channels"])]
            rows.append([InlineKeyboardButton("◀️ Back",
                                              callback_data="idx:back")])
            try:
                await query.edit_message_text(
                    "📡 <b>Channels queued:</b>\n<i>Tap ❌ to remove one.</i>",
                    parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
            except Exception:  # noqa: BLE001
                pass
        else:
            await _edit_panel(query, uid)
        return

    if action in ("skip", "range", "limit"):
        if query.message.chat.type != "private":
            await query.answer("Open my PM to change settings.",
                               show_alert=True)
            return
        _IDX_AWAIT[uid] = action
        prompts = {
            "skip": ("⏭️ <b>Skip files</b>\n\nHow many files to skip from the "
                     "newest, per channel? (0 = none)\n/cancel to abort."),
            "range": ("↔️ <b>Message range</b>\n\nSend <code>from_id to_id</code> "
                      "— e.g. <code>100 500</code> — to index only that range.\n"
                      "Send <code>0 0</code> for full history.\n/cancel to abort."),
            "limit": ("🔢 <b>Max files</b>\n\nHow many files to index in total? "
                      "(0 = no limit)\n/cancel to abort."),
        }
        await query.message.reply_text(prompts[action], parse_mode="HTML")
        return

    if action == "reset":
        _INDEX_CFG[uid] = {"channels": [], "skip": 0, "from_id": 0,
                           "to_id": 0, "limit": 0}
        await _edit_panel(query, uid)
        await query.answer("Settings reset ✓")
        return

    if action == "start":
        from app.services import mtproto_index
        if not mtproto_index.indexer_configured():
            await query.message.reply_text(_setup_text(), parse_mode="HTML")
            return
        if not cfg["channels"]:
            await query.answer("Add a channel first! ➕", show_alert=True)
            return
        token = _secrets.token_hex(4)
        _PENDING_INDEX[token] = {"channels": list(cfg["channels"]),
                                 "cfg": dict(cfg),
                                 "chat_id": query.message.chat_id}
        ch_lines = "\n".join(f"• <code>{_html.escape(c)}</code>"
                             for c in cfg["channels"])
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, start",
                                  callback_data=f"idx:go:{token}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"idx:no:{token}")],
        ])
        await query.edit_message_text(
            f"📥 <b>Start backfill?</b>\n\n{ch_lines}\n\n{_cfg_summary(cfg)}\n\n"
            "<i>Media will be indexed directly from history.</i>",
            parse_mode="HTML", reply_markup=kb)
        return

    if action == "go":
        params = _PENDING_INDEX.pop(arg, None)
        if not params:
            await query.edit_message_text("⚠️ <i>Expired — run /index again.</i>",
                                          parse_mode="HTML")
            return
        await _launch_index_run(query, context, arg, params["channels"],
                                params["cfg"])
        return

    if action in ("no", "back"):
        _PENDING_INDEX.pop(arg, None)
        await _edit_panel(query, uid)
        return


class _AwaitingFilter(filters.MessageFilter):
    """Matches private text only while that admin is mid-settings-input.

    Registered before the search text handler (same group): when it matches,
    this input is consumed here and never reaches search.
    """

    def filter(self, message) -> bool:
        u = message.from_user
        return bool(u and is_admin(u.id) and _IDX_AWAIT.get(u.id))


async def _on_idx_text_input(update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> None:
    """Consume one settings value for the /index panel (admin, PM only)."""
    from app.services import mtproto_index
    uid = update.effective_user.id
    kind = _IDX_AWAIT.get(uid)
    if not kind:
        return
    msg = update.effective_message
    text = (msg.text or "").strip()
    cfg = _get_cfg(uid)

    async def refresh(note: str) -> None:
        _IDX_AWAIT.pop(uid, None)
        await msg.reply_text(note, parse_mode="HTML")
        panel = _IDX_PANEL.get(uid)
        if panel:
            ptext, pkb = await _panel_data(uid)
            try:
                await context.bot.edit_message_text(
                    ptext, chat_id=panel[0], message_id=panel[1],
                    parse_mode="HTML", reply_markup=pkb)
            except Exception:  # noqa: BLE001
                pass

    if kind == "channel":
        ref, _ = mtproto_index.parse_channel_ref(text)
        if ref in cfg["channels"]:
            await refresh(f"ℹ️ <code>{_html.escape(ref)}</code> is already queued.")
        else:
            cfg["channels"].append(ref)
            await refresh(f"✅ Queued <code>{_html.escape(ref)}</code>.")
        return
    if kind in ("skip", "limit"):
        if not text.isdigit():
            await msg.reply_text("🔢 Numbers only please — try again or /cancel.",
                                 parse_mode="HTML")
            return
        cfg[kind] = int(text)
        label = {"skip": "Skip", "limit": "Max files"}[kind]
        await refresh(f"✅ {label} set to <b>{int(text)}</b>.")
        return
    if kind == "range":
        nums: list[str] = []
        for tok in _re.split(r"[\s,]+", text):
            nums.extend(p for p in tok.split("-") if p)
        if len(nums) != 2 or not all(n.isdigit() for n in nums):
            await msg.reply_text(
                "↔️ Send as <code>from_id to_id</code> — e.g. <code>100 500</code> "
                "— or /cancel.", parse_mode="HTML")
            return
        a, b = int(nums[0]), int(nums[1])
        if a > b:
            await msg.reply_text("↔️ from_id should be ≤ to_id — try again or /cancel.",
                                 parse_mode="HTML")
            return
        cfg["from_id"], cfg["to_id"] = a, b
        await refresh(f"✅ Range set to <b>{a}–{b}</b>."
                      if (a or b) else "✅ Range reset to full history.")
        return


async def _on_idx_cancel_cmd(update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> None:
    """Abort a pending /index settings input or index request."""
    user = update.effective_user
    uid = user.id if user else None
    if not uid or (uid not in _IDX_AWAIT and uid not in _REQ_AWAIT):
        return
    _IDX_AWAIT.pop(uid, None)
    _REQ_AWAIT.discard(uid)
    await update.effective_message.reply_text("🚫 <i>Input cancelled.</i>",
                                              parse_mode="HTML")
    panel = _IDX_PANEL.get(uid)
    if panel:
        ptext, pkb = await _panel_data(uid)
        try:
            await context.bot.edit_message_text(
                ptext, chat_id=panel[0], message_id=panel[1],
                parse_mode="HTML", reply_markup=pkb)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------- moderator approval workflow ---

_REQ_AWAIT: set[int] = set()      # user ids composing an index request
_REQ_INDEX: dict[str, dict] = {}  # token -> pending request


async def _start_index_request(update: Update,
                               context: ContextTypes.DEFAULT_TYPE) -> None:
    """Non-admin /index (PM): ask for the channel to request."""
    from app.services import mtproto_index
    user = update.effective_user
    uid = user.id if user else 0
    msg = update.effective_message
    if not mtproto_index.indexer_configured():
        await msg.reply_text(
            "⚠️ <i>Indexing isn't set up on the server yet — try again later.</i>",
            parse_mode="HTML")
        return
    if not settings.log_channel_id:
        await msg.reply_text(
            "⚠️ <i>Index requests aren't set up yet (no LOG_CHANNEL).</i>",
            parse_mode="HTML")
        return
    _REQ_AWAIT.add(uid)
    await msg.reply_text(
        "📥 <b>Request a channel for indexing</b>\n\n"
        "Send the channel's <b>last post link</b> "
        "(e.g. <code>t.me/c/123…/456</code>),\n"
        "or simply <b>forward any message</b> from that channel here.\n\n"
        "Moderators will review it. /cancel to abort.",
        parse_mode="HTML")


class _ReqAwaitFilter(filters.MessageFilter):
    """Matches PM text/forwards while the user is composing an index request."""

    def filter(self, message) -> bool:
        u = message.from_user
        return bool(u and u.id in _REQ_AWAIT
                    and (message.text or message.forward_from_chat))


async def _on_req_input(update: Update,
                        context: ContextTypes.DEFAULT_TYPE) -> None:
    """Consume one index-request input (link or forwarded channel message)."""
    from app.services import mtproto_index
    user = update.effective_user
    uid = user.id if user else 0
    msg = update.effective_message
    _REQ_AWAIT.discard(uid)

    ref = None
    if msg.forward_from_chat and getattr(msg.forward_from_chat, "type",
                                         None) == "channel":
        fwd = msg.forward_from_chat
        ref = f"@{fwd.username}" if getattr(fwd, "username", None) else str(fwd.id)
    elif msg.text:
        ref, _ = mtproto_index.parse_channel_ref(msg.text)
    if not ref:
        await msg.reply_text("❌ <i>Send a channel link or forward a "
                             "channel message. Try /index again.</i>",
                             parse_mode="HTML")
        return
    try:
        info = await mtproto_index.resolve_channel(ref)
    except Exception as exc:  # noqa: BLE001
        await msg.reply_text(
            f"❌ Couldn't read that channel: "
            f"<code>{_html.escape(str(exc))}</code>\n"
            f"<i>Try /index again.</i>", parse_mode="HTML")
        return

    token = _secrets.token_hex(4)
    _REQ_INDEX[token] = {
        "ref": ref, "title": info["title"], "chat_id": info["chat_id"],
        "last_msg_id": info["last_msg_id"],
        "from_user": uid,
        "from_name": (user.full_name if user else str(uid)),
    }
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Accept & index",
                              callback_data=f"idx:reqaccept:{token}")],
        [InlineKeyboardButton("❌ Reject",
                              callback_data=f"idx:reqreject:{token}")],
    ])
    try:
        await context.bot.send_message(
            settings.log_channel_id,
            f"#IndexRequest\n\n"
            f"By: {_html.escape(_REQ_INDEX[token]['from_name'])} "
            f"(<code>{uid}</code>)\n"
            f"📌 {_html.escape(info['title'])}\n"
            f"🆔 <code>{_html.escape(ref)}</code>\n"
            f"🔢 Last message: <b>{info['last_msg_id']}</b>",
            parse_mode="HTML", reply_markup=kb)
    except Exception as exc:  # noqa: BLE001
        _REQ_INDEX.pop(token, None)
        await msg.reply_text(
            f"❌ Couldn't forward the request to moderators: "
            f"<code>{_html.escape(str(exc))}</code>", parse_mode="HTML")
        return
    await msg.reply_text(
        "✅ <b>Request sent!</b>\nModerators will review it — "
        "I'll notify you of the decision.", parse_mode="HTML")


def _agg_text(agg: dict, started: float = 0.0,
              cfg: dict | None = None, workers: int = 0) -> str:
    """Live progress card for a parallel backfill run (DB-aggregated)."""
    lines = ["📥 <b>Backfill running…</b>"]
    if agg.get("titles"):
        shown = ", ".join(agg["titles"][:3])
        more = "…" if len(agg["titles"]) > 3 else ""
        lines.append(f"📌 {_html.escape(shown)}{more}")
    if workers:
        lines.append(f"👷 Workers: <b>{workers}</b>")
    lines.append("")
    if cfg and cfg.get("limit"):
        pct = min(1.0, agg["saved"] / cfg["limit"])
        lines.append(f"{_bar(pct)} <b>{pct * 100:.0f}%</b>")
        lines.append("")
    saved_label = "📨 Forwarded" if agg.get("mode") == "forward" else "💾 Saved"
    lines.append(f"{saved_label}: <b>{agg['saved']}</b>")
    lines.append(f"⏭️ Skipped: <b>{agg['skipped']}</b>")
    if agg.get("dupes"):
        lines.append(f"♻️ Already indexed: <b>{agg['dupes']}</b>")
    lines.append(f"❌ Errors: <b>{agg['errors']}</b>")
    if agg.get("last_error"):
        lines.append(f"⚠️ <code>{_html.escape(agg['last_error'][:180])}</code>")
    finished = agg.get("done", 0) + agg.get("cancelled", 0) + agg.get("error", 0)
    lines.append(f"🧩 Jobs: <b>{finished}/{agg.get('total', 0)}</b> done")
    if started:
        el = _time.monotonic() - started
        rate = agg["saved"] / (el / 60) if el > 5 else 0
        lines.append(f"\n⏱ {int(el // 60)}m {int(el % 60):02d}s"
                     f" • ⚡ {rate:.0f}/min")
    return "\n".join(lines)


async def _begin_backfill(context: ContextTypes.DEFAULT_TYPE, token: str,
                          channels: list, cfg: dict, chat_id: int,
                          edit_msg_id: int | None = None) -> None:
    """Create chunked jobs and launch the parallel backfill engine.

    Shared by the /index panel flow and accepted moderator requests.
    Progress is shown by editing one status message (existing or new).
    """
    from app.services import mtproto_index
    mode = mtproto_index.backfill_mode()
    # direct mode packs file_ids from history — no bot username needed.
    # forward mode needs the bot's username to forward files to it.
    bot_username = ""
    if mode == "forward":
        try:
            me = await context.bot.get_me()
            bot_username = "@" + (me.username or "")
        except Exception:  # noqa: BLE001
            bot_username = ""
        if not bot_username or bot_username == "@":
            await context.bot.send_message(
                chat_id,
                "❌ <i>Bot has no username — set one in BotFather.</i>",
                parse_mode="HTML")
            return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🛑 Cancel", callback_data=f"idx:cancel:{token}")]])
    cancel_event = asyncio.Event()
    if edit_msg_id:
        await context.bot.edit_message_text(
            "📥 <b>Indexing… starting</b>", chat_id=chat_id,
            message_id=edit_msg_id, parse_mode="HTML", reply_markup=kb)
        status_id = edit_msg_id
    else:
        status = await context.bot.send_message(
            chat_id, "📥 <b>Indexing… starting</b>",
            parse_mode="HTML", reply_markup=kb)
        status_id = status.message_id
    run = {"event": cancel_event, "chat_id": chat_id,
           "msg_id": status_id, "start": _time.monotonic(),
           "token": token}
    _INDEX_RUNS[token] = run

    try:
        created = await mtproto_index.create_jobs(
            token, channels, skip=cfg["skip"],
            from_id=cfg["from_id"], to_id=cfg["to_id"])
    except Exception as exc:  # noqa: BLE001
        _INDEX_RUNS.pop(token, None)
        try:
            await context.bot.edit_message_text(
                f"❌ <b>Couldn't start:</b> "
                f"<code>{_html.escape(str(exc))}</code>",
                chat_id=chat_id, message_id=status_id, parse_mode="HTML")
        except Exception:  # noqa: BLE001
            pass
        return
    if not created["jobs"]:
        _INDEX_RUNS.pop(token, None)
        try:
            await context.bot.edit_message_text(
                "ℹ️ <i>Nothing to index — channels are empty or out of range.</i>",
                chat_id=chat_id, message_id=status_id, parse_mode="HTML")
        except Exception:  # noqa: BLE001
            pass
        return
    workers = mtproto_index.session_count()

    async def push(agg: dict) -> None:
        try:
            await context.bot.edit_message_text(
                _agg_text(agg, started=run["start"], cfg=cfg,
                          workers=workers),
                chat_id=chat_id, message_id=status_id, parse_mode="HTML",
                reply_markup=kb)
        except Exception:  # noqa: BLE001
            pass

    async def runner() -> None:
        try:
            totals = await mtproto_index.run_backfill(
                token, bot_username, cancel_event, progress_cb=push,
                limit=cfg["limit"] or 0)
        except Exception as exc:  # noqa: BLE001
            totals = {"fatal": str(exc), "saved": 0}
        _INDEX_RUNS.pop(token, None)
        if totals.get("fatal"):
            tail = (f"❌ <b>Failed:</b> "
                    f"<code>{_html.escape(totals['fatal'])}</code>")
        elif totals.get("cancelled"):
            tail = "🛑 <i>Cancelled.</i>"
        elif totals.get("limit_hit"):
            tail = "✅ <b>Done — file limit reached.</b>"
        else:
            tail = "✅ <b>Done.</b>"
        final = (f"📥 <b>Backfill finished</b> ({totals.get('mode', '?')} mode)\n\n"
                 f"{'📨 Forwarded' if totals.get('mode') == 'forward' else '💾 Saved'}: "
                 f"<b>{totals.get('saved', 0)}</b>\n"
                 f"⏭️ Skipped: <b>{totals.get('skipped', 0)}</b>\n"
                 f"♻️ Already indexed: <b>{totals.get('dupes', 0)}</b>\n"
                 f"❌ Errors: <b>{totals.get('errors', 0)}</b>\n"
                 f"👷 Workers: <b>{totals.get('workers', workers)}</b>\n\n"
                 f"{tail}")
        if totals.get("errors"):
            try:
                errs = await mtproto_index.job_errors(token)
            except Exception:  # noqa: BLE001
                errs = []
            if errs:
                detail = "\n".join(
                    f"• {_html.escape((e['title'] or '?')[:40])}: "
                    f"<code>{_html.escape((e['error'] or '')[:180])}</code>"
                    for e in errs)
                final += f"\n\n⚠️ <b>What failed:</b>\n{detail}"
        final += "\n\n<i>Send /index for the new total.</i>"
        try:
            await context.bot.edit_message_text(
                final, chat_id=chat_id, message_id=status_id,
                parse_mode="HTML")
        except Exception:  # noqa: BLE001
            pass

    run["task"] = asyncio.create_task(runner())


async def _launch_index_run(query, context: ContextTypes.DEFAULT_TYPE,
                            token: str, channels: list, cfg: dict) -> None:
    """Panel 'go' entry point — edits the confirmation message in place."""
    await _begin_backfill(context, token, channels, cfg,
                          query.message.chat_id,
                          edit_msg_id=query.message.message_id)


def register(application: Application) -> None:
    application.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST & filters.ATTACHMENT,
        on_channel_post,
    ))
    application.add_handler(CommandHandler("index", index_command))
    application.add_handler(CommandHandler("cancel", _on_idx_cancel_cmd))
    application.add_handler(CallbackQueryHandler(_on_index_callback,
                                                pattern=r"^idx:"))
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND
        & _AwaitingFilter(),
        _on_idx_text_input,
    ))
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND & _ReqAwaitFilter(),
        _on_req_input,
    ))
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.ATTACHMENT,
        on_admin_pm_media,
    ))

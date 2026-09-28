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
from app.handlers.common import admin_only, effective_main_channel, is_admin
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


async def save_file(msg, doc, bot=None) -> tuple[str, str, bool]:
    """Old-bot style save: validate -> duplicate check -> insert.

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
                quality=quality,
                language=language,
                title_key=tk or None,
            ).on_conflict_do_nothing(index_elements=["file_id"])
            result = await session.execute(stmt)
            await session.commit()
            if not (result.rowcount or 0):
                # lost a race with a concurrent insert -> treat as duplicate
                log.debug("auto-index skip duplicate (race) %r", file_name)
                return "duplicate", file_name, False
            log.debug("auto-index saved %r", file_name)
            new_title = bool(tk) and await _is_new_title(session, tk, file_id)
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

    status, file_name, new_title = await save_file(msg, doc,
                                                     bot=context.bot)
    if status == "saved" and new_title:
        await _post_new_movie_alert(context.bot, file_name)

    data = context.chat_data
    data["idx_count"] = data.get("idx_count", 0) + 1
    n = data["idx_count"]
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


@admin_only
async def index_command(update: Update,
                        context: ContextTypes.DEFAULT_TYPE) -> None:
    """/index — control panel; /index <channel> for a quick backfill.

    The panel queues channels and sets skip / message-range / max-files
    options. Starting launches the server-side MTProto backfill
    (needs TG_SESSION configured) with live progress and cancel.
    """
    if context.args:
        await _quick_index_flow(update, context, " ".join(context.args))
        return
    user = update.effective_user
    uid = user.id if user else 0
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


def _stats_text(s: dict, title: str | None = None, ch_idx: int = 0,
                ch_total: int = 1, started: float = 0.0,
                cfg: dict | None = None) -> str:
    """Live progress card for a backfill run."""
    lines = ["📥 <b>Indexing…</b>"]
    if ch_total > 1:
        lines.append(f"📡 Channel {ch_idx + 1}/{ch_total}")
    if title:
        lines.append(f"📌 {_html.escape(title)}")
    lines.append("")
    if cfg and cfg.get("limit"):
        pct = s["forwarded"] / cfg["limit"]
        lines.append(f"{_bar(pct)} <b>{pct * 100:.0f}%</b>")
        lines.append("")
    lines.append(f"📨 Forwarded: <b>{s['forwarded']}</b>")
    lines.append(f"⏭️ Skipped: <b>{s['skipped']}</b>")
    lines.append(f"❌ Errors: <b>{s['errors']}</b>")
    if started:
        el = _time.monotonic() - started
        rate = s["forwarded"] / (el / 60) if el > 5 else 0
        lines.append(f"\n⏱ {int(el // 60)}m {int(el % 60):02d}s • ⚡ {rate:.0f}/min")
    if s.get("cancelled"):
        lines.append("\n🛑 <i>Cancelled.</i>")
    return "\n".join(lines)


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
        "<i>Every video/audio/document will be forwarded to me and indexed. "
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
        # cancel a running backfill
        run = _INDEX_RUNS.get(arg)
        if run:
            run["event"].set()
            await query.answer("Cancelling…", show_alert=True)
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
            "<i>Media will be forwarded to me and indexed.</i>",
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
    """Abort a pending /index settings input."""
    user = update.effective_user
    uid = user.id if user else None
    if not uid or uid not in _IDX_AWAIT:
        return
    _IDX_AWAIT.pop(uid, None)
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


async def _launch_index_run(query, context: ContextTypes.DEFAULT_TYPE,
                            token: str, channels: list, cfg: dict) -> None:
    from app.services import mtproto_index
    try:
        me = await context.bot.get_me()
        bot_username = "@" + (me.username or "")
    except Exception:  # noqa: BLE001
        bot_username = ""
    if not bot_username or bot_username == "@":
        await query.edit_message_text(
            "❌ <i>Bot has no username — set one in BotFather.</i>",
            parse_mode="HTML")
        return
    cancel_event = asyncio.Event()
    chat_id = query.message.chat_id
    status = await query.edit_message_text(
        "📥 <b>Indexing… starting</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🛑 Cancel", callback_data=f"idx:cancel:{token}")]]))
    run = {"event": cancel_event, "chat_id": chat_id,
           "msg_id": status.message_id, "start": _time.monotonic()}
    _INDEX_RUNS[token] = run

    async def push(stats: dict, title: str, ch_idx: int) -> None:
        try:
            await context.bot.edit_message_text(
                _stats_text(stats, title=title, ch_idx=ch_idx,
                            ch_total=len(channels), started=run["start"],
                            cfg=cfg),
                chat_id=chat_id, message_id=run["msg_id"], parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "🛑 Cancel", callback_data=f"idx:cancel:{token}")]]))
        except Exception:  # noqa: BLE001
            pass

    async def runner() -> None:
        totals = {"scanned": 0, "forwarded": 0, "skipped": 0, "errors": 0,
                  "cancelled": False, "limit_hit": False}
        try:
            remaining = cfg["limit"] or 0
            for i, ref in enumerate(channels):
                if cancel_event.is_set():
                    totals["cancelled"] = True
                    break
                try:
                    info = await mtproto_index.resolve_channel(ref)
                    title = info["title"]
                except Exception as exc:  # noqa: BLE001
                    log.warning("index: resolve failed for %s: %s", ref, exc)
                    totals["errors"] += 1
                    continue
                stats = await mtproto_index.walk_and_forward(
                    ref, info["last_msg_id"], bot_username, cancel_event,
                    progress_cb=lambda s, t=title, d=i: push(s, t, d),
                    skip=cfg["skip"], min_id=cfg["from_id"],
                    max_id=cfg["to_id"], limit=remaining)
                for k in ("scanned", "forwarded", "skipped", "errors"):
                    totals[k] += stats[k]
                if remaining:
                    remaining = max(0, remaining - stats["forwarded"])
                if stats.get("cancelled"):
                    totals["cancelled"] = True
                    break
                if stats.get("limit_hit"):
                    totals["limit_hit"] = True
                    break
        except Exception as exc:  # noqa: BLE001
            totals["fatal"] = str(exc)
        _INDEX_RUNS.pop(token, None)
        if totals.get("fatal"):
            tail = (f"❌ <b>Failed:</b> "
                    f"<code>{_html.escape(totals['fatal'])}</code>")
        elif totals["cancelled"]:
            tail = "🛑 <i>Cancelled.</i>"
        elif totals["limit_hit"]:
            tail = "✅ <b>Done — file limit reached.</b>"
        else:
            tail = "✅ <b>Done.</b>"
        try:
            await context.bot.edit_message_text(
                _stats_text(totals, started=run["start"], cfg=cfg)
                + f"\n\n{tail}\n\n<i>Send /index for the new total.</i>",
                chat_id=chat_id, message_id=run["msg_id"], parse_mode="HTML")
        except Exception:  # noqa: BLE001
            pass

    run["task"] = asyncio.create_task(runner())


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
        filters.ChatType.PRIVATE & filters.ATTACHMENT,
        on_admin_pm_media,
    ))

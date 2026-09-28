"""Admin commands (ADMIN_IDS only).

/stats — users, files, searches today, top queries, pending requests
/broadcast — reply to any message to send it to all users (with progress)
/ban <id> / /unban <id>
/users — total + recent users
/settings — view + toggle force-sub / stream player
/requests — pending movie requests with fulfill/delete buttons
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from app import ui
from app.config import settings
from app.db import get_session_factory
from app.handlers.common import (
    admin_only,
    effective_force_sub_channels,
    effective_main_channel,
    force_sub_enabled,
    get_setting,
    set_setting,
    stream_enabled,
)
from app.models import File, Group, MovieRequest, SearchLog, User
from app.state import settings_cache_invalidate
from sqlalchemy import func, select

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ stats ---

@admin_only
async def stats_command(update: Update,
                        context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        users = (await session.execute(select(func.count())
                                       .select_from(User))).scalar() or 0
        files = (await session.execute(select(func.count())
                                       .select_from(File))).scalar() or 0
        groups = (await session.execute(select(func.count())
                                        .select_from(Group))).scalar() or 0
        today = datetime.now(timezone.utc).replace(hour=0, minute=0,
                                                   second=0, microsecond=0)
        searches_today = (await session.execute(
            select(func.count()).select_from(SearchLog)
            .where(SearchLog.created_at >= today))).scalar() or 0
        top = (await session.execute(
            select(SearchLog.query, func.count().label("c"))
            .where(SearchLog.created_at >= today)
            .group_by(SearchLog.query).order_by(func.count().desc()).limit(5)
        )).all()
        pending = (await session.execute(
            select(func.count()).select_from(MovieRequest)
            .where(MovieRequest.status == "pending"))).scalar() or 0
        storage = (await session.execute(
            select(func.coalesce(func.sum(File.file_size), 0)))).scalar() or 0

    lines = [
        ui.header("Bot Stats"),
        "",
        f"👥 Users: <b>{users}</b>",
        f"📁 Files indexed: <b>{files}</b>",
        f"👥 Groups: <b>{groups}</b>",
        f"🔍 Searches today: <b>{searches_today}</b>",
        f"📩 Pending requests: <b>{pending}</b>",
        f"💾 Indexed size: <b>{ui.human_size(storage)}</b>",
    ]
    if top:
        lines += ["", "<b>🔥 Top today:</b>"]
        lines += [f"• {q} <i>({c})</i>" for q, c in top]
    await msg.reply_text("\n".join(lines), parse_mode="HTML")


# -------------------------------------------------------------- broadcast ---

@admin_only
async def broadcast_command(update: Update,
                            context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.reply_to_message:
        await msg.reply_text("↩️ <i>Reply to a message with /broadcast to send "
                             "it to all users.</i>", parse_mode="HTML")
        return
    src = msg.reply_to_message
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        user_ids = (await session.execute(
            select(User.user_id).where(User.banned.is_(False)))).scalars().all()

    total = len(user_ids)
    status = await msg.reply_text(f"📢 Broadcasting… <b>0/{total}</b>",
                                  parse_mode="HTML")
    ok = fail = 0
    for i, uid in enumerate(user_ids, 1):
        try:
            await context.bot.copy_message(chat_id=uid,
                                           from_chat_id=src.chat_id,
                                           message_id=src.message_id)
            ok += 1
        except Exception as exc:  # noqa: BLE001 - blocked/deleted etc.
            fail += 1
            log.debug("broadcast to %s failed: %s", uid, exc)
        if i % 20 == 0:
            try:
                await status.edit_text(f"📢 Broadcasting… <b>{i}/{total}</b>",
                                       parse_mode="HTML")
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(0.05)
    await status.edit_text(f"📢 <b>Done.</b> ✅ {ok} delivered, ❌ {fail} failed.",
                           parse_mode="HTML")


# ------------------------------------------------------------ ban / users ---

@admin_only
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("Usage: <code>/ban &lt;user_id&gt;</code>",
                             parse_mode="HTML")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await msg.reply_text("⚠️ User id must be numeric.")
        return
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        row = await session.get(User, target)
        if row:
            row.banned = True
        else:
            session.add(User(user_id=target, banned=True))
        await session.commit()
    await msg.reply_text(f"🚫 Banned <code>{target}</code>.", parse_mode="HTML")


@admin_only
async def unban_command(update: Update,
                        context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("Usage: <code>/unban &lt;user_id&gt;</code>",
                             parse_mode="HTML")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await msg.reply_text("⚠️ User id must be numeric.")
        return
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        row = await session.get(User, target)
        if row:
            row.banned = False
            await session.commit()
    await msg.reply_text(f"✅ Unbanned <code>{target}</code>.", parse_mode="HTML")


@admin_only
async def users_command(update: Update,
                        context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        total = (await session.execute(select(func.count())
                                       .select_from(User))).scalar() or 0
        recent = (await session.execute(
            select(User).order_by(User.created_at.desc()).limit(10))).scalars().all()
    lines = [ui.header(f"Users ({total})"), ""]
    for u in recent:
        flag = "🚫" if u.banned else "✅"
        lines.append(f"{flag} <code>{u.user_id}</code> — {u.full_name or '?'}")
    await msg.reply_text("\n".join(lines), parse_mode="HTML")


# --------------------------------------------------------------- settings ---

async def _settings_text() -> str:
    fs = await force_sub_enabled()
    st = await stream_enabled()
    chans = await effective_force_sub_channels()
    main_ch = await effective_main_channel()
    return (
        f"{ui.header('Settings')}\n\n"
        f"🔐 Force-subscribe: <b>{'ON' if fs else 'OFF'}</b>\n"
        f"▶️ Stream player: <b>{'ON' if st else 'OFF'}</b>\n"
        f"📢 Force-sub channels: <code>{', '.join(chans) or '—'}</code>\n"
        f"📣 Main channel: <code>{main_ch or '—'}</code>\n\n"
        "<i>Channels / main channel are edited in the web dashboard.</i>"
    )


def _settings_keyboard(fs: bool, st: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔐 Force-sub: {'ON' if fs else 'OFF'}",
                              callback_data="st:fs")],
        [InlineKeyboardButton(f"▶️ Stream player: {'ON' if st else 'OFF'}",
                              callback_data="st:stream")],
    ])


@admin_only
async def settings_command(update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    fs = await force_sub_enabled()
    st = await stream_enabled()
    await msg.reply_text(await _settings_text(), parse_mode="HTML",
                         reply_markup=_settings_keyboard(fs, st))


async def _on_settings_callback(update: Update,
                                context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    from app.handlers.common import is_admin
    if not is_admin(user.id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    data = query.data or ""
    await query.answer()
    if data == "st:fs":
        await set_setting("force_sub_enabled", not await force_sub_enabled())
    elif data == "st:stream":
        await set_setting("stream_enabled", not await stream_enabled())
    fs = await force_sub_enabled()
    st = await stream_enabled()
    settings_cache_invalidate()
    await query.edit_message_text(await _settings_text(), parse_mode="HTML",
                                  reply_markup=_settings_keyboard(fs, st))


# --------------------------------------------------------------- requests ---

# TEMPORARY migration feasibility probe — remove after the legacy file_id
# delivery test concludes.
@admin_only
async def fidtest_command(update: Update,
                          context: ContextTypes.DEFAULT_TYPE) -> None:
    """Test whether a legacy old-bot file_id can be delivered by this bot.

    Usage: /fidtest <legacy_file_id>
    """
    msg = update.effective_message
    if not context.args:
        await msg.reply_text("Usage: /fidtest <legacy_file_id>")
        return
    legacy = context.args[0].strip()
    try:
        from app.services.fileid_conv import to_bot_api
        new_fid = to_bot_api(legacy)
    except Exception as exc:  # noqa: BLE001
        await msg.reply_text(f"❌ Conversion failed: {exc}")
        return
    try:
        await msg.reply_document(
            document=new_fid,
            caption="✅ Legacy file_id delivered — migration is viable.",
        )
    except Exception as exc:  # noqa: BLE001
        # TEMPORARY diagnostic: show the converted id so we can verify which
        # converter version the live bot actually ran.
        await msg.reply_text(f"❌ Delivery failed: {exc}\nConverted id was: {new_fid}")


@admin_only
async def requests_command(update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        rows = (await session.execute(
            select(MovieRequest)
            .where(MovieRequest.status == "pending")
            .order_by(MovieRequest.created_at.desc()).limit(15)
        )).scalars().all()
    if not rows:
        await msg.reply_text("📩 <i>No pending requests. All caught up! ✨</i>",
                             parse_mode="HTML")
        return
    for r in rows:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Done", callback_data=f"rq:done:{r.id}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"rq:del:{r.id}"),
        ]])
        await msg.reply_text(
            f"📩 <b>#{r.id}</b> — <b>{r.title}</b>\n"
            f"👤 {r.user_name or '?'} (<code>{r.user_id}</code>)",
            parse_mode="HTML", reply_markup=kb)


async def _on_request_admin_callback(update: Update,
                                     context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    from app.handlers.common import is_admin
    if not is_admin(user.id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    data = query.data or ""
    await query.answer()
    try:
        _, action, rid_s = data.split(":")
        rid = int(rid_s)
    except ValueError:
        return
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        req = await session.get(MovieRequest, rid)
        if not req:
            await query.answer("Already handled.", show_alert=True)
            return
        if action == "done":
            req.status = "done"
            note = f"🎉 Good news! <b>{req.title}</b> is now available — search for it! 🔍"
            try:
                await context.bot.send_message(req.user_id, note,
                                               parse_mode="HTML")
            except Exception:  # noqa: BLE001
                pass
            await query.edit_message_text(f"✅ Request #{rid} marked done.")
        else:
            await session.delete(req)
            await query.edit_message_text(f"🗑 Request #{rid} deleted.")
        await session.commit()


def register(application: Application) -> None:
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("requests", requests_command))
    # TEMPORARY: legacy file_id delivery probe (remove after test).
    application.add_handler(CommandHandler("fidtest", fidtest_command))
    application.add_handler(CallbackQueryHandler(_on_settings_callback,
                                                pattern=r"^st:"))
    application.add_handler(CallbackQueryHandler(_on_request_admin_callback,
                                                pattern=r"^rq:"))

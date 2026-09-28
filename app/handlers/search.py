"""Auto-filter: any text message (group or PM) triggers an instant search.

Flow: force-sub check -> two-stage DB search -> movie list (paginated) ->
movie detail (TMDB poster + quality buttons) -> file delivery. Misses get
spell suggestions + a request button (+ AI-crafted reply when configured).
"""
from __future__ import annotations

import logging
import math

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.error import BadRequest, Forbidden
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from app import ui
from app.config import settings
from app.db import get_session_factory
from app.handlers.common import (
    ensure_group,
    ensure_user,
    get_missing_channels,
    is_banned,
    send_join_prompt,
    stream_enabled,
)
from app.models import File
from app.services import ai
from app.services.search import (
    get_recent_queries,
    group_by_title,
    search_files,
)
from app.services.spell import suggest
from app.services.textutil import clean_title
from app.state import store_search

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ search ---

def _list_body(movies: list[dict], page: int) -> str:
    start = page * ui.PAGE_SIZE
    return "\n".join(
        ui.movie_row_label(i, m)
        for i, m in enumerate(movies[start:start + ui.PAGE_SIZE], start + 1)
    )


async def run_search(bot, chat_id: int, query: str, user_id: int,
                     reply_to: int | None = None,
                     is_group: bool = False) -> None:
    """Execute a search and deliver results (or smart not-found)."""
    query = (query or "").strip()[:200]
    if len(query) < 2:
        return
    if await is_banned(user_id):
        return

    items, parsed = await search_files(query, user_id=user_id)

    if items:
        movies = group_by_title(items)
        total_pages = max(1, math.ceil(len(movies) / ui.PAGE_SIZE))
        sid = store_search({"movies": movies, "query": query, "page": 0})
        text = (
            ui.movie_list_text(query, 0, total_pages, len(movies))
            + "\n" + _list_body(movies, 0)
            + ui.FOOTER_TIP
        )
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=ui.movie_list_keyboard(sid, movies, 0, total_pages),
            parse_mode="HTML",
            reply_to_message_id=reply_to,
        )
        return

    # ---- miss: spell suggestions + request button (+ AI reply) ------------
    factory = get_session_factory(settings.DATABASE_URL)
    suggestions: list[str] = []
    try:
        async with factory() as session:
            suggestions = await suggest(session, parsed["query"] or query)
    except Exception as exc:  # noqa: BLE001
        log.debug("suggest failed: %s", exc)

    sid = store_search({"movies": [], "query": query,
                        "suggestions": suggestions, "page": 0})
    text = await ai.not_found_message(query, suggestions)
    try:
        history = await get_recent_queries(user_id)
        tip = await ai.personalized_line(history)
        if tip:
            text += f"\n\n✨ <i>{tip}</i>"
    except Exception:  # noqa: BLE001
        pass
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=ui.suggestions_keyboard(sid, suggestions),
        parse_mode="HTML",
        reply_to_message_id=reply_to,
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat or user.is_bot:
        return
    text = (msg.text or "").strip()
    if len(text) < 2 or len(text) > 200:
        return

    await ensure_user(user)
    await ensure_group(chat)
    if await is_banned(user.id):
        return

    is_group = chat.type in ("group", "supergroup")
    missing = await get_missing_channels(context.bot, user.id)
    if missing:
        await send_join_prompt(
            context.bot, chat.id, user.id,
            {"type": "search", "query": text, "chat_id": chat.id,
             "is_group": is_group},
            reply_to=msg.message_id,
        )
        return

    await run_search(context.bot, chat.id, text, user.id,
                     reply_to=msg.message_id, is_group=is_group)


# ---------------------------------------------------------------- delivery ---

async def deliver_file(bot, chat_id: int, user_id: int, file_db_id: int,
                       reply_to: int | None = None) -> None:
    """Send a file by DB id, honoring force-subscribe first."""
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        f = await session.get(File, file_db_id)
        row = None
        if f:
            row = {c: getattr(f, c) for c in (
                "id", "file_id", "file_name", "file_size", "mime_type",
                "quality", "language", "created_at", "source_channel_id")}
    if not row:
        await bot.send_message(chat_id, "⚠️ This file is no longer available.")
        return

    missing = await get_missing_channels(bot, user_id)
    if missing:
        await send_join_prompt(
            bot, chat_id, user_id,
            {"type": "file", "chat_id": chat_id, "file_db_id": file_db_id},
            reply_to=reply_to,
        )
        return

    is_admin = user_id in settings.admin_ids
    title = clean_title(row["file_name"]) or "File"
    meta_bits = [b for b in (row["quality"], row["language"]) if b]
    meta_s = f" ({' • '.join(meta_bits)})" if meta_bits else ""
    caption = (f"🎬 <b>{title}</b>{meta_s}\n"
               f"💾 {ui.human_size(row['file_size'])}")
    if is_admin:
        # Debug line for admins: verify indexing source/mode.
        fid = row["file_id"] or ""
        created = row["created_at"]
        created_s = created.strftime("%Y-%m-%d %H:%M") if created else "?"
        src = row["source_channel_id"] or "auto"
        caption += (f"\n\n🔧 <code>db:{row['id']} src:{src} "
                    f"idx:{created_s}</code>\n"
                    f"<code>fid:{fid[:50]}</code>")

    kb = None
    if await stream_enabled():
        from app.player import stream_links_for
        watch_url, _dl_url = stream_links_for(row["id"])
        # WebApp button: opens the watch page *inside* Telegram
        # (player + external players + download with progress).
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Watch / Download",
                                  web_app=WebAppInfo(url=watch_url))],
        ])

    try:
        await bot.send_document(
            chat_id=chat_id,
            document=row["file_id"],
            caption=caption,
            parse_mode="HTML",
            reply_markup=kb,
            reply_to_message_id=reply_to,
        )
    except Forbidden:
        log.info("forbidden sending to %s", chat_id)
    except BadRequest as exc:
        log.warning("send_document failed: %s", exc)
        err_msg = ("⚠️ Couldn't deliver that file — it may have been "
                   "removed from Telegram.")
        if is_admin:
            fid = row["file_id"] or ""
            err_msg += f"\n\n🔧 <code>{exc}</code>\n<code>fid:{fid[:60]}</code>"
        await bot.send_message(chat_id, err_msg, parse_mode="HTML")


def register(application: Application) -> None:
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

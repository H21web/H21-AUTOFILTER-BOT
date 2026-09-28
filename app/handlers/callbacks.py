"""Inline-button callbacks: result pagination, movie detail, file delivery,
did-you-mean re-search, trending taps and stream/download choice buttons.
"""
from __future__ import annotations

import logging
import math

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from app import ui
from app.handlers.common import ensure_user, is_banned, stream_enabled
from app.handlers.search import deliver_file, run_search
from app.services import tmdb
from app.state import get_search

log = logging.getLogger(__name__)


def _list_message(sid: str, query: str, movies: list[dict],
                  page: int) -> tuple[str, InlineKeyboardMarkup]:
    total_pages = max(1, math.ceil(len(movies) / ui.PAGE_SIZE))
    text = (
        ui.movie_list_text(query, page, total_pages, len(movies))
        + "\n"
        + "\n".join(
            ui.movie_row_label(i, m)
            for i, m in enumerate(movies[page * ui.PAGE_SIZE:
                                         page * ui.PAGE_SIZE + ui.PAGE_SIZE],
                                  start=page * ui.PAGE_SIZE + 1)
        )
        + ui.FOOTER_TIP
    )
    kb = ui.movie_list_keyboard(sid, movies, page, total_pages)
    return text, kb


async def _send_movie_list(bot, chat_id: int, sid: str, page: int,
                           reply_to: int | None = None) -> None:
    sess = get_search(sid)
    if not sess:
        await bot.send_message(chat_id, "⌛ That search expired — search again 🙂")
        return
    sess["page"] = page
    text, kb = _list_message(sid, sess["query"], sess["movies"], page)
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML",
                           reply_to_message_id=reply_to)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    data = query.data or ""
    await ensure_user(user)
    if await is_banned(user.id):
        await query.answer("🚫 You are banned.", show_alert=True)
        return

    try:
        if data.startswith("sl:"):
            _, sid, page_s = data.split(":")
            await query.answer()
            sess = get_search(sid)
            if not sess:
                await query.answer("Expired — search again 🙂", show_alert=True)
                return
            page = max(0, int(page_s))
            sess["page"] = page
            movies = sess["movies"]
            text, kb = _list_message(sid, sess["query"], movies, page)
            await query.edit_message_text(text, reply_markup=kb,
                                          parse_mode="HTML")

        elif data.startswith("mv:"):
            _, sid, idx_s = data.split(":")
            await query.answer("Loading… ⚡")
            sess = get_search(sid)
            if not sess:
                await query.answer("Expired — search again 🙂", show_alert=True)
                return
            movies = sess["movies"]
            idx = int(idx_s)
            if not 0 <= idx < len(movies):
                return
            movie = movies[idx]
            meta = await tmdb.get_movie(movie["display"], movie.get("year"))
            watch_urls: dict[int, tuple[str, str]] = {}
            if await stream_enabled():
                from app.player import stream_links_for
                for f in movie["files"]:
                    watch_urls[f["id"]] = stream_links_for(f["id"])
            kb = ui.detail_keyboard(sid, idx, sess.get("page", 0),
                                    movie["files"], watch_urls)
            caption = ui.detail_text(movie["display"], meta, movie["files"])
            poster = (meta or {}).get("poster_url")
            if poster:
                await query.message.reply_photo(
                    photo=poster, caption=caption, reply_markup=kb,
                    parse_mode="HTML")
            else:
                await query.message.reply_text(
                    caption, reply_markup=kb, parse_mode="HTML")

        elif data.startswith("bk:"):
            _, sid, page_s = data.split(":")
            await query.answer()
            try:
                await query.message.delete()
            except Exception:  # noqa: BLE001
                pass
            await _send_movie_list(context.bot, query.message.chat_id, sid,
                                   int(page_s))

        elif data.startswith("get:"):
            await query.answer("Sending… 📄")
            file_db_id = int(data.split(":", 1)[1])
            await deliver_file(context.bot, query.message.chat_id, user.id,
                               file_db_id)

        elif data.startswith("dym:"):
            _, sid, i_s = data.split(":")
            await query.answer()
            sess = get_search(sid)
            if not sess:
                return
            suggestions = sess.get("suggestions", [])
            i = int(i_s)
            if 0 <= i < len(suggestions):
                await run_search(context.bot, query.message.chat_id,
                                 suggestions[i], user.id)

        elif data.startswith("tq:"):
            await query.answer()
            q = data.split(":", 1)[1]
            await run_search(context.bot, query.message.chat_id, q, user.id)

        elif data.startswith(("w:", "d:")):
            # stream/download choice: show per-file url buttons
            _, sid, idx_s = data.split(":")
            await query.answer()
            sess = get_search(sid)
            if not sess:
                return
            movies = sess["movies"]
            idx = int(idx_s)
            if not 0 <= idx < len(movies):
                return
            from app.player import stream_links_for
            urls = []
            for f in movies[idx]["files"][:8]:
                watch_url, dl_url = stream_links_for(f["id"])
                urls.append((ui.file_button_label(f)
                             .replace("📄 ", ""), watch_url, dl_url))
            await query.edit_message_reply_markup(
                reply_markup=ui.stream_choice_keyboard(urls))

        elif data == "noop":
            await query.answer()
    except Exception as exc:  # noqa: BLE001
        log.warning("callback %r failed: %s", data, exc)
        try:
            await query.answer("⚠️ Something went wrong.", show_alert=True)
        except Exception:  # noqa: BLE001
            pass


def register(application: Application) -> None:
    application.add_handler(CallbackQueryHandler(
        on_callback,
        pattern=r"^(sl:|mv:|bk:|get:|dym:|tq:|w:|d:|noop$)",
    ))

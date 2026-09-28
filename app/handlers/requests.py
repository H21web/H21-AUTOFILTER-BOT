"""Movie request system.

The "📩 Request this movie" button stores a request row and notifies admins.
Admins manage the queue with /requests (see admin.py).
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from app.config import settings
from app.db import get_session_factory
from app.handlers.common import ensure_user
from app.models import MovieRequest
from app.state import get_search

log = logging.getLogger(__name__)


async def create_request(user_id: int, user_name: str | None,
                        title: str) -> int:
    """Store a request; returns its id."""
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        req = MovieRequest(user_id=user_id,
                           user_name=(user_name or "")[:200],
                           title=title[:300], status="pending")
        session.add(req)
        await session.commit()
        await session.refresh(req)
        return req.id


async def notify_admins(bot, req_id: int, title: str,
                       user_id: int, user_name: str | None) -> None:
    if not settings.admin_ids:
        return
    text = (f"📩 <b>New movie request</b> #{req_id}\n\n"
            f"🎬 <b>{title}</b>\n"
            f"👤 {user_name or '?'} (<code>{user_id}</code>)\n\n"
            f"<i>Manage with /requests</i>")
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as exc:  # noqa: BLE001
            log.debug("admin notify failed: %s", exc)


async def on_request_callback(update: Update,
                              context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    data = query.data or ""
    if not data.startswith("req:"):
        return
    await query.answer()
    sess = get_search(data.split(":", 1)[1])
    if not sess:
        await query.answer("That search expired — search again 🙂",
                           show_alert=True)
        return
    title = sess.get("query", "Unknown")
    await ensure_user(user)
    try:
        req_id = await create_request(user.id, user.full_name, title)
        await notify_admins(context.bot, req_id, title, user.id, user.full_name)
        await query.answer(f"✅ Requested! We'll notify you when “{title}” "
                           f"is added.", show_alert=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("request failed: %s", exc)
        await query.answer("⚠️ Couldn't save your request — try again.",
                           show_alert=True)


def register(application: Application) -> None:
    application.add_handler(CallbackQueryHandler(on_request_callback,
                                                pattern=r"^req:"))

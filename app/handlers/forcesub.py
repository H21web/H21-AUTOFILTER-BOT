"""Smart force-subscribe.

Before results/files are shown, membership in FORCE_SUB_CHANNELS is checked.
If the user hasn't joined, they get join buttons + "✅ I've Joined" — and
when they tap it, membership is re-verified and the *pending* action
(search results or file delivery) is executed automatically.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from app import ui
from app.handlers.common import get_missing_channels
from app.state import pop_pending

log = logging.getLogger(__name__)


async def on_joined(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    data = query.data or ""
    if not data.startswith("join:"):
        return
    await query.answer("Checking…")

    missing = await get_missing_channels(context.bot, user.id)
    if missing:
        await query.answer(
            "❌ You're not in all channels yet — join them first!", show_alert=True
        )
        return

    pending = pop_pending(user.id)
    if not pending:
        await query.edit_message_text(
            "✅ <b>All good!</b> Now send me any movie name to search. 🔍",
            parse_mode="HTML",
        )
        return

    # Membership verified — auto-deliver whatever was pending.
    try:
        await query.edit_message_text(
            "✅ <b>Verified!</b> Fetching your results… ⚡", parse_mode="HTML"
        )
    except Exception:  # noqa: BLE001
        pass

    # Deferred imports: search.py imports helpers from common.py, and this
    # module is imported by search-time code paths — keep the cycle broken.
    from app.handlers.search import deliver_file, run_search

    ptype = pending.get("type")
    try:
        if ptype == "search":
            await run_search(
                bot=context.bot,
                chat_id=pending["chat_id"],
                query=pending["query"],
                user_id=user.id,
                is_group=pending.get("is_group", False),
            )
        elif ptype == "file":
            await deliver_file(
                bot=context.bot,
                chat_id=pending["chat_id"],
                user_id=user.id,
                file_db_id=pending["file_db_id"],
            )
        else:
            log.warning("unknown pending type: %r", ptype)
    except Exception as exc:  # noqa: BLE001
        log.warning("pending delivery failed: %s", exc)
        await context.bot.send_message(
            chat_id=pending.get("chat_id", user.id),
            text="⚠️ Something went wrong delivering your results — please try again.",
        )


def register(application: Application) -> None:
    application.add_handler(CallbackQueryHandler(on_joined, pattern=r"^join:"))

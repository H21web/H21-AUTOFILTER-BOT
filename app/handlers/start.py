"""``/start`` and ``/help`` — the bot's front door.

Professional look: bold header, what-the-bot-does in two lines, buttons for
search help / trending / support. Also tracks the user row.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from app import ui
from app.handlers.common import ensure_user, is_banned
from app.services.search import get_trending
from app.state import get_search


async def _welcome_text(bot_username: str) -> str:
    return (
        f"{ui.HEADER} <b>Welcome to @{bot_username}!</b>\n\n"
        "Your personal movie library — just type any movie name here or in "
        "your group and I'll find it instantly. ⚡\n"
        "🎞 Posters & ratings &nbsp;•&nbsp; 📁 Quality options &nbsp;•&nbsp; "
        "▶️ Watch online"
        f"{ui.FOOTER_TIP}"
    )


def _welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 How to search", callback_data="howsearch")],
        [InlineKeyboardButton("🔥 Trending now", callback_data="trend")],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
    ])


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    await ensure_user(user)
    if await is_banned(user.id):
        await msg.reply_text("🚫 You are banned from using this bot.")
        return
    bot_username = (await context.bot.get_me()).username or "this bot"
    # /start with a deep-link payload (e.g. from the new-movie alert channel)
    await msg.reply_text(
        await _welcome_text(bot_username),
        reply_markup=_welcome_keyboard(),
        parse_mode="HTML",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    text = (
        f"{ui.header('How to use')}\n\n"
        "🔍 <b>Search:</b> just type a movie name — in this chat or any group "
        "I'm in.\n"
        "   <i>Example:</i> <code>avengers endgame 2019</code>\n\n"
        "🎞 Tap a movie to see posters, ratings and quality options.\n"
        "📄 Tap a file button to get the file instantly.\n"
        "✨ Typo? I'll suggest what you meant.\n"
        "📩 Missing a movie? Hit <b>Request this movie</b>.\n\n"
        "<b>Commands</b>\n"
        "/start — welcome screen\n"
        "/help — this message\n"
        "/trending — what everyone is searching"
    )
    await msg.reply_text(text, parse_mode="HTML")


async def trending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    trending = await get_trending()
    if not trending:
        await msg.reply_text("🔥 <i>No trending searches yet — be the first!</i>",
                             parse_mode="HTML")
        return
    lines = [ui.header("Trending now"), ""]
    lines += [f"{i}. <b>{q}</b> <i>({c} searches)</i>"
              for i, (q, c) in enumerate(trending, 1)]
    await msg.reply_text(
        "\n".join(lines),
        reply_markup=ui.trending_keyboard(trending),
        parse_mode="HTML",
    )


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    if data == "howsearch":
        await query.answer()
        await query.message.reply_text(
            "🔍 <b>Just type the movie name.</b>\n\n"
            "Add the <b>year</b> for exact matches "
            "(<code>inception 2010</code>), or a quality like "
            "<code>1080p</code> / language like <code>hindi</code> to filter.\n\n"
            "Try it now — send me any movie name! 👇",
            parse_mode="HTML",
        )
    elif data == "trend":
        await query.answer()
        trending = await get_trending()
        if not trending:
            await query.answer("No trending searches yet.", show_alert=True)
            return
        lines = [ui.header("Trending now"), ""]
        lines += [f"{i}. <b>{q}</b>" for i, (q, _) in enumerate(trending, 1)]
        await query.message.reply_text(
            "\n".join(lines),
            reply_markup=ui.trending_keyboard(trending),
            parse_mode="HTML",
        )
    elif data == "help":
        await query.answer()
        # reuse the /help text by faking a message call
        await help_command(update, context)
    elif data == "noop":
        await query.answer()


def register(application: Application) -> None:
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("trending", trending_command))
    application.add_handler(CallbackQueryHandler(
        _on_callback, pattern=r"^(howsearch|trend|help|noop)$"))

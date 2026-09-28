"""Builds the python-telegram-bot Application.

Bot API only — no Pyrogram/MTProto anywhere in this project. In webhook mode
the FastAPI app (``app/main.py``) feeds updates into
``application.update_queue``; PTB never opens its own HTTP listener.

Feature handlers live in ``app/handlers/`` and each exposes
``register(application)``.
"""
from __future__ import annotations

from telegram.ext import Application

from app.config import Settings
from app.handlers import admin, callbacks, forcesub, index, requests, search, start


def build_application(settings: Settings) -> Application:
    """Create and configure the PTB Application (no polling — webhook mode)."""
    application = Application.builder().token(settings.BOT_TOKEN).build()
    # Commands first (most specific), then channel indexing, then the
    # catch-all text search handler, then callback queries.
    start.register(application)
    admin.register(application)
    requests.register(application)
    index.register(application)
    forcesub.register(application)
    search.register(application)
    callbacks.register(application)
    return application

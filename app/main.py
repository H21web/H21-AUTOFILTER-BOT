"""FastAPI ASGI app: Telegram webhook receiver + health check.

Runs on any ASGI host (AlwaysData, Render, VPS, Docker) with zero code
changes — all config comes from environment variables (see ``app/config.py``).

Endpoints:
    GET  /health   -> {"status": "ok", "db": "ok"|"error: ..."}
    POST /webhook  -> verifies the Telegram secret header, then hands the
                      Update to the PTB application queue.
    GET  /watch/{token} -> HTML5 stream player page (signed expiring link)
    GET  /dl/{token}    -> file proxy with HTTP Range support
    GET  /admin/*       -> admin web dashboard (session login)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware
from telegram import Update

from app import player
from app.bot import build_application
from app.config import settings
from app.dashboard import router as dashboard_router
from app.db import dispose_engine, get_engine
from app.services import tmdb

logging.basicConfig(level=settings.LOG_LEVEL.upper())
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start/stop the PTB Application alongside the ASGI server."""
    ptb_app = build_application(settings)
    await ptb_app.initialize()
    await ptb_app.start()
    app.state.ptb_app = ptb_app
    log.info("PTB application started; webhook receiver ready.")
    yield
    await ptb_app.stop()
    await ptb_app.shutdown()
    await tmdb.close_client()
    await dispose_engine()
    log.info("Shutdown complete.")


app = FastAPI(title="Autofilter Bot", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.WEBHOOK_SECRET or settings.BOT_TOKEN,
    session_cookie="mxadm",
    max_age=24 * 3600,
)

app.include_router(player.router)
app.include_router(dashboard_router)
app.mount(
    "/admin/static",
    StaticFiles(directory=str(Path(__file__).parent / "dashboard" / "static")),
    name="admin_static",
)


@app.get("/health")
async def health() -> dict:
    """Liveness probe that also pings the database."""
    db_status = "ok"
    try:
        engine = get_engine(settings.DATABASE_URL)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - surfaced in the health payload
        db_status = f"error: {exc}"
    return {"status": "ok", "db": db_status}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    """Receive a Telegram update, verify the secret, queue it for PTB."""
    if settings.WEBHOOK_SECRET:
        if x_telegram_bot_api_secret_token != settings.WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid webhook secret")
    payload = await request.json()
    update = Update.de_json(payload, request.app.state.ptb_app.bot)
    await request.app.state.ptb_app.update_queue.put(update)
    return {"ok": True}

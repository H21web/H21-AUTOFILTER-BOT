"""Admin web dashboard (``/admin``).

Session login with ADMIN_USERNAME/ADMIN_PASSWORD. Pages: overview stat
cards, file browser, user management, broadcast composer, request queue and
bot settings. Dark, mobile-friendly UI (templates in ``templates/``).
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.config import settings
from app.db import get_session_factory
from app.handlers.common import (
    effective_force_sub_channels,
    effective_main_channel,
    force_sub_enabled,
    set_setting,
    stream_enabled,
)
from app.models import File, Group, MovieRequest, SearchLog, User
from app.state import settings_cache_invalidate

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")
TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

PAGE_SIZE = 20
LAST_BROADCAST: dict = {"status": "idle", "ok": 0, "fail": 0, "total": 0}


# ------------------------------------------------------------------ auth ---

def _dashboard_enabled() -> bool:
    return bool(settings.ADMIN_PASSWORD.strip())


def _check_login(username: str, password: str) -> bool:
    if not _dashboard_enabled():
        return False
    return (
        hmac.compare_digest(username, settings.ADMIN_USERNAME)
        and hmac.compare_digest(password, settings.ADMIN_PASSWORD)
    )


async def require_login(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/admin/login", status_code=303)
    return None


def _render(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request=request, name=name,
                                      context={"request": request, **ctx})


# ----------------------------------------------------------------- routes ---

@router.get("/login", name="admin_login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("admin"):
        return RedirectResponse(url="/admin/", status_code=303)
    return _render(request, "login.html",
                   enabled=_dashboard_enabled(), error=None)


@router.post("/login")
async def login_submit(request: Request, username: str = Form(""),
                       password: str = Form("")):
    if _check_login(username, password):
        request.session["admin"] = True
        return RedirectResponse(url="/admin/", status_code=303)
    return _render(
        request, "login.html", enabled=_dashboard_enabled(),
        error=("Dashboard login is disabled — set ADMIN_PASSWORD on the server."
               if not _dashboard_enabled() else "Invalid credentials."))


@router.get("/logout", name="admin_logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)


@router.get("/", name="admin_overview", response_class=HTMLResponse)
async def overview(request: Request, _=Depends(require_login)):
    if isinstance(_, RedirectResponse):
        return _
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        users = (await session.execute(select(func.count())
                                       .select_from(User))).scalar() or 0
        files = (await session.execute(select(func.count())
                                       .select_from(File))).scalar() or 0
        groups = (await session.execute(select(func.count())
                                        .select_from(Group))).scalar() or 0
        now = datetime.now(timezone.utc)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week = now - timedelta(days=7)
        searches_today = (await session.execute(
            select(func.count()).select_from(SearchLog)
            .where(SearchLog.created_at >= today))).scalar() or 0
        searches_week = (await session.execute(
            select(func.count()).select_from(SearchLog)
            .where(SearchLog.created_at >= week))).scalar() or 0
        top = (await session.execute(
            select(SearchLog.query, func.count().label("c"))
            .where(SearchLog.created_at >= week)
            .group_by(SearchLog.query).order_by(func.count().desc()).limit(10)
        )).all()
        pending = (await session.execute(
            select(func.count()).select_from(MovieRequest)
            .where(MovieRequest.status == "pending"))).scalar() or 0
        storage = (await session.execute(
            select(func.coalesce(func.sum(File.file_size), 0)))).scalar() or 0
    return _render(request, "overview.html", users=users, files=files,
                   groups=groups, searches_today=searches_today,
                   searches_week=searches_week, top=top, pending=pending,
                   storage=storage)


@router.get("/files", name="admin_files", response_class=HTMLResponse)
async def files_page(request: Request, _=Depends(require_login),
                     q: str = "", page: int = 1):
    if isinstance(_, RedirectResponse):
        return _
    page = max(1, page)
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        stmt = select(File)
        count_stmt = select(func.count()).select_from(File)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(File.file_name.ilike(like))
            count_stmt = count_stmt.where(File.file_name.ilike(like))
        total = (await session.execute(count_stmt)).scalar() or 0
        rows = (await session.execute(
            stmt.order_by(File.created_at.desc())
            .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))).scalars().all()
    pages = max(1, -(-total // PAGE_SIZE))
    return _render(request, "files.html", files=rows, q=q, page=page,
                   pages=pages, total=total)


@router.post("/files/delete")
async def files_delete(request: Request, _=Depends(require_login),
                       file_id: int = Form(...)):
    if isinstance(_, RedirectResponse):
        return _
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        row = await session.get(File, file_id)
        if row:
            await session.delete(row)
            await session.commit()
    return RedirectResponse(url="/admin/files", status_code=303)


@router.get("/users", name="admin_users", response_class=HTMLResponse)
async def users_page(request: Request, _=Depends(require_login)):
    if isinstance(_, RedirectResponse):
        return _
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        total = (await session.execute(select(func.count())
                                       .select_from(User))).scalar() or 0
        banned = (await session.execute(
            select(func.count()).select_from(User)
            .where(User.banned.is_(True)))).scalar() or 0
        rows = (await session.execute(
            select(User).order_by(User.created_at.desc()).limit(50)
        )).scalars().all()
    return _render(request, "users.html", users=rows, total=total,
                   banned=banned)


@router.post("/users/ban")
async def users_ban(request: Request, _=Depends(require_login),
                    user_id: int = Form(...), action: str = Form(...)):
    if isinstance(_, RedirectResponse):
        return _
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        row = await session.get(User, user_id)
        if row:
            row.banned = (action == "ban")
            await session.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


async def _broadcast_task(bot, text: str) -> None:
    LAST_BROADCAST.update(status="running", ok=0, fail=0, total=0)
    factory = get_session_factory(settings.DATABASE_URL)
    try:
        async with factory() as session:
            user_ids = (await session.execute(
                select(User.user_id).where(User.banned.is_(False))
            )).scalars().all()
        LAST_BROADCAST["total"] = len(user_ids)
        for uid in user_ids:
            try:
                await bot.send_message(uid, text, parse_mode="HTML")
                LAST_BROADCAST["ok"] += 1
            except Exception as exc:  # noqa: BLE001
                LAST_BROADCAST["fail"] += 1
                log.debug("broadcast to %s failed: %s", uid, exc)
            await asyncio.sleep(0.05)
        LAST_BROADCAST["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        log.warning("broadcast task failed: %s", exc)
        LAST_BROADCAST["status"] = f"error: {exc}"


@router.get("/broadcast", name="admin_broadcast", response_class=HTMLResponse)
async def broadcast_page(request: Request, _=Depends(require_login)):
    if isinstance(_, RedirectResponse):
        return _
    return _render(request, "broadcast.html", last=LAST_BROADCAST)


@router.post("/broadcast")
async def broadcast_submit(request: Request, _=Depends(require_login),
                           message: str = Form("")):
    if isinstance(_, RedirectResponse):
        return _
    message = message.strip()[:4000]
    if not message:
        return _render(request, "broadcast.html", last=LAST_BROADCAST,
                       error="Message is empty.")
    if LAST_BROADCAST.get("status") == "running":
        return _render(request, "broadcast.html", last=LAST_BROADCAST,
                       error="A broadcast is already running.")
    bot = request.app.state.ptb_app.bot
    asyncio.create_task(_broadcast_task(bot, message))
    return RedirectResponse(url="/admin/broadcast", status_code=303)


@router.get("/requests", name="admin_requests", response_class=HTMLResponse)
async def requests_page(request: Request, _=Depends(require_login)):
    if isinstance(_, RedirectResponse):
        return _
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        rows = (await session.execute(
            select(MovieRequest).order_by(MovieRequest.created_at.desc())
            .limit(50))).scalars().all()
    return _render(request, "requests.html", requests=rows)


@router.post("/requests/action")
async def requests_action(request: Request, _=Depends(require_login),
                          req_id: int = Form(...), action: str = Form(...)):
    if isinstance(_, RedirectResponse):
        return _
    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        req = await session.get(MovieRequest, req_id)
        if req:
            if action == "done":
                req.status = "done"
                try:
                    bot = request.app.state.ptb_app.bot
                    await bot.send_message(
                        req.user_id,
                        f"🎉 Good news! <b>{req.title}</b> is now available — "
                        "search for it! 🔍", parse_mode="HTML")
                except Exception:  # noqa: BLE001
                    pass
            else:
                await session.delete(req)
            await session.commit()
    return RedirectResponse(url="/admin/requests", status_code=303)


@router.get("/settings", name="admin_settings", response_class=HTMLResponse)
async def settings_page(request: Request, _=Depends(require_login)):
    if isinstance(_, RedirectResponse):
        return _
    return _render(request, "settings.html", **await _settings_ctx())


async def _settings_ctx() -> dict:
    return {
        "force_sub": await force_sub_enabled(),
        "stream": await stream_enabled(),
        "channels": ", ".join(await effective_force_sub_channels()),
        "main_channel": await effective_main_channel() or "",
    }


@router.post("/settings")
async def settings_submit(request: Request, _=Depends(require_login),
                          force_sub: str = Form("off"),
                          stream: str = Form("off"),
                          channels: str = Form(""),
                          main_channel: str = Form("")):
    if isinstance(_, RedirectResponse):
        return _
    await set_setting("force_sub_enabled", force_sub == "on")
    await set_setting("stream_enabled", stream == "on")
    await set_setting("force_sub_channels", channels.strip())
    await set_setting("main_channel_id", main_channel.strip())
    settings_cache_invalidate()
    return RedirectResponse(url="/admin/settings", status_code=303)

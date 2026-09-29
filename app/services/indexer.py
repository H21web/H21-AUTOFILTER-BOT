"""Backfill engine — fresh, fast, reliable.

Pyrogram (bot token, no user session) walks a channel's history and
bulk-forwards media (100 per API call) to the dump channel. The bot's
auto-index handler saves each file with a native Bot API file_id.

Designed for lakhs of files:
- Bulk forward (100/call) — ~50-100 files/sec, no per-file overhead
- FloodWait handled with backoff + retry
- Progress throttled (no Telegram rate-limit spam)
- Dedupe at DB level (ON CONFLICT DO NOTHING) — no per-file SELECT
- One run at a time; abort via cancel event
- Resume = just re-run (duplicates skipped by DB)
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_client = None
_client_lock = asyncio.Lock()

_current_run: dict | None = None
_run_lock = asyncio.Lock()


async def get_client():
    """Pyrogram bot client singleton (in-memory session)."""
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                from pyrogram import Client
                from app.config import settings
                if not (settings.BOT_TOKEN and settings.TG_API_ID
                        and settings.TG_API_HASH):
                    raise RuntimeError(
                        "indexer not configured: BOT_TOKEN + TG_API_ID + "
                        "TG_API_HASH required")
                _client = Client(
                    "moovidex_indexer",
                    api_id=int(settings.TG_API_ID),
                    api_hash=settings.TG_API_HASH,
                    bot_token=settings.BOT_TOKEN,
                    in_memory=True,
                )
                await _client.start()
                me = await _client.get_me()
                log.info("indexer logged in as @%s (%s)", me.username, me.id)
    return _client


def indexer_configured() -> bool:
    from app.config import settings
    return bool(settings.INDEXER_BOT_MODE and settings.BOT_TOKEN
                and settings.TG_API_ID and settings.TG_API_HASH
                and settings.index_channels)


def parse_channel_ref(raw: str) -> str:
    """Normalize: username, t.me link, t.me/c link, -100 id, invite link."""
    s = (raw or "").strip()
    if s.startswith("https://"):
        s = s.split("https://", 1)[1]
    for prefix in ("t.me/", "telegram.me/", "telegram.dog/"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    if s.startswith("+") or s.startswith("joinchat/"):
        return "https://t.me/" + s
    if s.startswith("c/"):
        parts = s[2:].split("/")
        if parts and parts[0].isdigit():
            return "-100" + parts[0]
    return s


async def resolve_channel(ref: str) -> dict:
    """Resolve ref -> {ref, chat_id, title, last_msg_id}. Raises on error."""
    client = await get_client()
    ref = parse_channel_ref(ref)
    try:
        chat = await client.get_chat(ref)
    except Exception:
        chat = await client.get_chat(int(ref))
    last = 0
    async for m in client.get_chat_history(chat.id, limit=1):
        last = m.id
        break
    return {
        "ref": ref,
        "chat_id": chat.id,
        "title": getattr(chat, "title", None) or ref,
        "last_msg_id": last,
    }


def _has_media(msg) -> bool:
    return bool(msg.document or msg.video or msg.audio or msg.photo
                or msg.animation or msg.voice or msg.video_note)


async def index_channel(ref: str, progress_cb=None,
                        cancel_event: asyncio.Event | None = None,
                        skip: int = 0, min_id: int = 0,
                        max_id: int = 0) -> dict:
    """Walk history newest->oldest, bulk-forward media to dump channel.

    progress_cb(stats) called per batch (throttled by caller if needed).
    stats: {scanned, queued, forwarded, errors, pct, done, title}
    """
    from pyrogram.errors import FloodWait
    from app.config import settings

    client = await get_client()
    info = await resolve_channel(ref)
    dump_id = settings.index_channels[0]
    total = info["last_msg_id"] or 1

    stats = {"scanned": 0, "queued": 0, "forwarded": 0, "errors": 0,
             "pct": 0.0, "done": False, "title": info["title"]}
    cancel = cancel_event or asyncio.Event()
    batch: list[int] = []
    skip_left = max(0, skip)
    offset_id = max_id or 0
    lowest_seen = total

    async def _report():
        if total:
            stats["pct"] = min(100.0,
                               (total - lowest_seen) / total * 100)
        if progress_cb:
            try:
                await progress_cb(dict(stats))
            except Exception:  # noqa: BLE001
                pass

    async def _flush():
        if not batch:
            return
        try:
            await client.forward_messages(
                chat_id=dump_id,
                from_chat_id=info["chat_id"],
                message_ids=batch)
            stats["forwarded"] += len(batch)
        except FloodWait as e:
            log.warning("backfill FloodWait %ss", e.value)
            await asyncio.sleep(e.value + 5)
            try:
                await client.forward_messages(
                    chat_id=dump_id,
                    from_chat_id=info["chat_id"],
                    message_ids=batch)
                stats["forwarded"] += len(batch)
            except Exception as e2:  # noqa: BLE001
                log.warning("backfill retry failed: %s", e2)
                stats["errors"] += len(batch)
        except Exception as e:  # noqa: BLE001
            log.warning("backfill batch failed: %s", e)
            stats["errors"] += len(batch)
        batch.clear()
        await _report()

    while not cancel.is_set():
        try:
            msgs = [m async for m in client.get_chat_history(
                info["chat_id"], limit=200,
                offset_id=offset_id or None)]
        except FloodWait as e:
            log.warning("backfill history FloodWait %ss", e.value)
            await asyncio.sleep(e.value + 5)
            continue
        except Exception as e:  # noqa: BLE001
            log.exception("backfill history error: %s", e)
            stats["errors"] += 1
            break
        if not msgs:
            break
        for m in msgs:
            stats["scanned"] += 1
            offset_id = m.id
            lowest_seen = min(lowest_seen, m.id)
            if min_id and m.id < min_id:
                await _flush()
                stats["done"] = True
                await _report()
                return stats
            if cancel.is_set():
                break
            if not _has_media(m):
                continue
            if skip_left > 0:
                skip_left -= 1
                continue
            batch.append(m.id)
            stats["queued"] += 1
            if len(batch) >= 100:
                await _flush()
        await _flush()
        # Gentle pause between history pages (kind to Telegram)
        await asyncio.sleep(0.3)

    await _flush()
    stats["done"] = True
    await _report()
    return stats


async def start_run(ref: str, progress_cb=None, skip: int = 0,
                    min_id: int = 0, max_id: int = 0) -> dict:
    """Start a backfill run. Returns {started: True} or {error: ...}."""
    global _current_run
    async with _run_lock:
        if _current_run and not _current_run["task"].done():
            return {"error": "already_running"}
        cancel = asyncio.Event()
        run = {"ref": ref, "cancel": cancel, "task": None,
               "title": ref, "stats": {}}

        async def _runner():
            try:
                async def _cb(s):
                    run["stats"] = s
                    run["title"] = s.get("title", ref)
                    if progress_cb:
                        await progress_cb(s)
                await index_channel(ref, progress_cb=_cb,
                                    cancel_event=cancel,
                                    skip=skip, min_id=min_id, max_id=max_id)
            except asyncio.CancelledError:
                run["stats"] = {**run.get("stats", {}), "done": True,
                                "aborted": True}
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("backfill crashed: %s", e)
                run["stats"] = {"error": str(e)[:200], "done": True}

        run["task"] = asyncio.create_task(_runner())
        _current_run = run
        return {"started": True, "ref": ref}


async def cancel_run() -> bool:
    """Abort the active run. True if one was running."""
    global _current_run
    async with _run_lock:
        if _current_run and not _current_run["task"].done():
            _current_run["cancel"].set()
            _current_run["task"].cancel()
            try:
                await _current_run["task"]
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            _current_run = None
            return True
        _current_run = None
        return False


def run_status() -> dict | None:
    if _current_run and not _current_run["task"].done():
        return {"ref": _current_run["ref"],
                "title": _current_run.get("title", ""),
                "stats": _current_run.get("stats", {})}
    return None


def current_run() -> dict | None:
    return _current_run

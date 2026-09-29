"""Fresh channel indexer — simple, fast, no bugs.

How it works (the proven path):
1. Pyrogram (bot token, no user session) walks the source channel's history.
2. Media messages are bulk-forwarded (100 per API call) to INDEX_CHANNEL.
3. The bot's `on_channel_post` auto-index handler saves each file with a
   native Bot API file_id — guaranteed deliverable via Bot API.

No job table, no Telethon, no manual file_id packing, no resume complexity.
If the run is interrupted, just run /index again (dedupe by file_id skips
already-indexed files).

Requires:
- INDEXER_BOT_MODE=true (bot logs in via MTProto itself)
- INDEX_CHANNELS set (dump channel where the bot is admin)
- Bot must be a member of the source channel
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_client = None
_client_lock = asyncio.Lock()

# One run at a time per process.
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
                        "indexer not configured: set BOT_TOKEN + TG_API_ID + "
                        "TG_API_HASH and INDEXER_BOT_MODE=true")
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
    """Normalize a channel ref (username, t.me link, -100 id, invite link)."""
    s = (raw or "").strip()
    if s.startswith("https://"):
        s = s.split("https://", 1)[1]
    for prefix in ("t.me/", "telegram.me/", "telegram.dog/"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    # Private invite links: t.me/+xxxx or t.me/joinchat/xxxx — keep as-is,
    # Pyrogram's get_chat() can resolve them if the bot can access.
    if s.startswith("+") or s.startswith("joinchat/"):
        return "https://t.me/" + s
    if s.startswith("c/"):
        # t.me/c/<internal_id>/<msg> -> -100<internal_id>
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
    """Walk channel history newest->oldest, bulk-forward media to dump.

    progress_cb(stats) is called after each batch. stats:
    {scanned, queued, forwarded, errors, done}
    """
    from pyrogram.errors import FloodWait
    from app.config import settings

    client = await get_client()
    info = await resolve_channel(ref)
    dump_id = settings.index_channels[0]

    stats = {"scanned": 0, "queued": 0, "forwarded": 0, "errors": 0,
             "done": False, "title": info["title"]}
    cancel = cancel_event or asyncio.Event()
    batch: list[int] = []
    skip_left = max(0, skip)
    offset_id = max_id or 0

    async def flush():
        if not batch:
            return
        try:
            await client.forward_messages(
                chat_id=dump_id,
                from_chat_id=info["chat_id"],
                message_ids=batch,
            )
            stats["forwarded"] += len(batch)
        except FloodWait as e:
            log.warning("index FloodWait %ss, sleeping", e.value)
            await asyncio.sleep(e.value + 5)
            # retry once
            try:
                await client.forward_messages(
                    chat_id=dump_id,
                    from_chat_id=info["chat_id"],
                    message_ids=batch,
                )
                stats["forwarded"] += len(batch)
            except Exception as e2:  # noqa: BLE001
                log.warning("index retry failed: %s", e2)
                stats["errors"] += len(batch)
        except Exception as e:  # noqa: BLE001
            log.warning("index forward batch failed: %s", e)
            stats["errors"] += len(batch)
        batch.clear()
        if progress_cb:
            try:
                await progress_cb(dict(stats))
            except Exception:  # noqa: BLE001
                pass

    while not cancel.is_set():
        try:
            msgs = [m async for m in client.get_chat_history(
                info["chat_id"], limit=200,
                offset_id=offset_id or None)]
        except FloodWait as e:
            log.warning("index history FloodWait %ss", e.value)
            await asyncio.sleep(e.value + 5)
            continue
        except Exception as e:  # noqa: BLE001
            log.exception("index history error: %s", e)
            stats["errors"] += 1
            break
        if not msgs:
            break
        for m in msgs:
            stats["scanned"] += 1
            offset_id = m.id
            if min_id and m.id < min_id:
                await flush()
                stats["done"] = True
                if progress_cb:
                    await progress_cb(dict(stats))
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
                await flush()
        await flush()
        await asyncio.sleep(0.2)

    await flush()
    stats["done"] = True
    if progress_cb:
        try:
            await progress_cb(dict(stats))
        except Exception:  # noqa: BLE001
            pass
    return stats


async def start_run(ref: str, progress_cb=None) -> dict:
    """Start an index run if none is active. Returns run dict or error."""
    global _current_run
    async with _run_lock:
        if _current_run and not _current_run["task"].done():
            return {"error": "already_running",
                    "title": _current_run.get("title", "")}
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
                                    cancel_event=cancel)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("index run crashed: %s", e)
                run["stats"] = {"error": str(e)[:200], "done": True}

        run["task"] = asyncio.create_task(_runner())
        _current_run = run
        return {"started": True, "ref": ref}


async def cancel_run() -> bool:
    """Cancel the active run. Returns True if one was running."""
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
    """Current run info or None."""
    if _current_run and not _current_run["task"].done():
        return {"ref": _current_run["ref"],
                "title": _current_run.get("title", ""),
                "stats": _current_run.get("stats", {})}
    return None

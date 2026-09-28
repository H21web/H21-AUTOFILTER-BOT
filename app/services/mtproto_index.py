"""Server-side MTProto channel indexer (optional).

The Bot API cannot read channel history, so the classic "/index a channel"
flow needs an MTProto client. This module runs a Telethon user client
*inside the bot process* using a session string from the TG_SESSION env var.

Security model:
  - No credentials in code: API_ID/API_HASH/TG_SESSION come from env vars.
  - The indexer account should be a DEDICATED spare account, not anyone's
    main account. It only reads channel history and forwards media to the
    bot itself; it never posts, deletes, or touches other chats.
  - Forwarded files arrive at the bot as PM messages from the indexer
    account, which must be listed in INDEXER_IDS to be trusted for indexing.
  - One indexing run at a time (asyncio lock), cancellable, FloodWait-aware
    with exact resume (offset_id), so it is fast but never hammers Telegram.

Dormant unless TG_SESSION is set: /index with a channel argument replies
with setup instructions instead of failing.

Backfill options (set from the /index panel, passed per run):
  - skip: ignore the first N media files (newest first), per channel.
  - min_id/max_id: only forward messages with min_id <= id <= max_id.
  - limit: stop after forwarding N files in total.
"""
from __future__ import annotations

import asyncio
import logging
import re

log = logging.getLogger(__name__)

_client = None
_client_lock = asyncio.Lock()
_run_lock = asyncio.Lock()

_LINK_RE = re.compile(
    r"(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$"
)


def indexer_configured() -> bool:
    from app.config import settings
    return bool(settings.TG_SESSION and settings.TG_API_ID and settings.TG_API_HASH)


def parse_channel_ref(text: str) -> tuple[str, int]:
    """Accept @username, t.me links (incl. t.me/c/<id>/<msg>), or -100 ids.

    Private channels have no username — pass the -100 id
    (e.g. -1001234567890) or copy any message link (t.me/c/<id>/<msg>).

    Returns (chat_ref, last_msg_id_hint). last_msg_id_hint is 0 when unknown.
    """
    text = text.strip()
    m = _LINK_RE.match(text)
    if m:
        chat_id, msg_id = m.group(4), int(m.group(5))
        if chat_id.isnumeric():
            chat_id = "-100" + chat_id
        return chat_id, msg_id
    if text.startswith("@") or text.startswith("-100") or text.lstrip("-").isdigit():
        return text, 0
    return "@" + text, 0


def _entity_ref(ref: str):
    """Normalize a channel ref for Telethon's get_entity.

    "-100…" strings (the only way to address a private channel) are turned
    into PeerChannel — get_entity doesn't reliably parse them as strings.
    Usernames/links pass through unchanged.
    """
    from telethon.tl.types import PeerChannel
    s = ref.strip()
    digits = s[4:] if s.startswith("-100") else s
    if digits and digits.lstrip("-").isdigit():
        return PeerChannel(int(digits))
    return ref


async def get_client():
    """Lazily build the Telethon client. Raises RuntimeError if unconfigured."""
    global _client
    if not indexer_configured():
        raise RuntimeError("indexer not configured (set TG_SESSION/TG_API_ID/TG_API_HASH)")
    if _client is None:
        async with _client_lock:
            if _client is None:
                from telethon.sessions import StringSession
                from telethon import TelegramClient
                from app.config import settings
                _client = TelegramClient(
                    StringSession(settings.TG_SESSION),
                    int(settings.TG_API_ID),
                    settings.TG_API_HASH,
                )
    return _client


async def resolve_channel(ref: str) -> dict:
    """Resolve a channel ref -> title/chat_id/last message id. Raises on error."""
    client = await get_client()
    if not client.is_connected():
        await client.connect()
    entity = await client.get_entity(_entity_ref(ref))
    last = 0
    async for m in client.iter_messages(entity, limit=1):
        last = m.id
        break
    return {
        "ref": ref,
        "chat_id": getattr(entity, "id", None),
        "title": getattr(entity, "title", None) or ref,
        "last_msg_id": last,
    }


async def walk_and_forward(
    ref: str,
    last_msg_id: int,
    bot_username: str,
    cancel_event: asyncio.Event,
    progress_cb=None,
    delay: float = 1.2,
    skip: int = 0,
    min_id: int = 0,
    max_id: int = 0,
    limit: int = 0,
) -> dict:
    """Walk channel history (newest->oldest) forwarding media to the bot.

    skip: ignore the first `skip` media files (newest first).
    min_id/max_id: only forward messages with min_id <= id <= max_id
        (0 = no bound). The walk stops once it passes below min_id.
    limit: stop after forwarding `limit` files (0 = no limit).
    Returns stats dict. Resumes exactly after FloodWaits via offset_id.
    Only one run at a time (module lock).
    """
    from telethon.errors import FloodWaitError

    stats = {"scanned": 0, "forwarded": 0, "skipped": 0,
             "errors": 0, "cancelled": False, "limit_hit": False}
    if _run_lock.locked():
        raise RuntimeError("another indexing run is already in progress")
    async with _run_lock:
        client = await get_client()
        if not client.is_connected():
            await client.connect()
        channel = await client.get_entity(_entity_ref(ref))
        offset_id = last_msg_id or 0
        skip_left = max(0, skip)
        since_progress = 0
        while True:
            if cancel_event.is_set():
                stats["cancelled"] = True
                break
            floor_hit = False
            limit_hit_now = False
            try:
                got = 0
                async for m in client.iter_messages(channel, limit=200,
                                                   offset_id=offset_id):
                    if cancel_event.is_set():
                        stats["cancelled"] = True
                        break
                    got += 1
                    stats["scanned"] += 1
                    offset_id = m.id
                    if min_id and m.id < min_id:
                        floor_hit = True
                        break
                    media = m.document or m.video or m.audio
                    if max_id and m.id > max_id:
                        stats["skipped"] += 1
                        continue
                    if not m.media or media is None:
                        stats["skipped"] += 1
                        continue
                    if skip_left > 0:
                        skip_left -= 1
                        stats["skipped"] += 1
                        continue
                    try:
                        await client.forward_messages(bot_username, m)
                        stats["forwarded"] += 1
                        since_progress += 1
                        await asyncio.sleep(delay)
                    except FloodWaitError as e:
                        wait = e.seconds + 5
                        log.warning("index FloodWait %ss, sleeping %ss (resume below %s)",
                                    e.seconds, wait, offset_id)
                        await asyncio.sleep(wait)
                        break  # re-enter while -> resume at offset_id
                    except Exception as e:  # noqa: BLE001
                        stats["errors"] += 1
                        log.warning("index forward failed for msg %s: %s", m.id, e)
                    if limit and stats["forwarded"] >= limit:
                        stats["limit_hit"] = True
                        limit_hit_now = True
                        break
                    if progress_cb and since_progress >= 25:
                        since_progress = 0
                        await progress_cb(dict(stats))
                if stats["cancelled"] or floor_hit or limit_hit_now:
                    break
                if got == 0:
                    break  # reached the beginning
            except FloodWaitError as e:
                wait = e.seconds + 5
                log.warning("index FloodWait %ss, sleeping %ss", e.seconds, wait)
                await asyncio.sleep(wait)
                continue
            except Exception as e:  # noqa: BLE001
                log.exception("index run error: %s", e)
                stats["errors"] += 1
                break
        if progress_cb:
            await progress_cb(dict(stats))
    return stats

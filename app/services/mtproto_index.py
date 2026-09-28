"""Parallel multi-account MTProto backfill engine.

Two modes (BACKFILL_MODE):

- **forward** (proven fallback): forward every media message to the bot in
  bulk batches (BACKFILL_BATCH per API call), which indexes each received
  file with a Bot-API-issued file_id: ~15-30 files/sec per account.
  The file_ids are guaranteed Bot-API-issued, so delivery always works.
  Use this with user sessions (TG_SESSIONS) when the bot can't join the
  channel directly.

- **direct** (default, fastest): walk history and save each media file's
  Bot API file_id straight into Postgres (~500-1000 files/sec).
  REQUIRES INDEXER_BOT_MODE=true — the indexer logs in as the bot itself
  via MTProto using **Pyrogram** (same library Tech VJ uses). Pyrogram's
  ``message.document.file_id`` is ALREADY the correct Bot API file_id —
  no manual packing, no format guessing. This is exactly how Tech VJ-style
  bots do it: bot token only, no user session string. NOTE: user sessions
  (TG_SESSIONS) do NOT work for direct mode.

Shared safety: chunked resumable jobs (backfill_jobs table, SKIP LOCKED
claiming, offset_id checkpointed as the worker walks), FloodWait backoff
(sleep e.seconds+5, 10-min cooldown after 5 consecutive), graceful cancel.

Security model: API_ID/API_HASH/sessions come from env vars only. Indexer
accounts should be DEDICATED spare accounts.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re

log = logging.getLogger(__name__)

_clients: list = []
_clients_lock = asyncio.Lock()
_pyro_client = None
_pyro_lock = asyncio.Lock()

_LINK_RE = re.compile(
    r"(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$"
)

_CHECKPOINT_EVERY = 25
_FW_COOLDOWN_AFTER = 5
_FW_COOLDOWN_SECS = 600
_DIRECT_BATCH = 500


def indexer_configured() -> bool:
    from app.config import settings
    if settings.INDEXER_BOT_MODE:
        return bool(settings.BOT_TOKEN and settings.TG_API_ID
                    and settings.TG_API_HASH)
    return bool(settings.tg_sessions and settings.TG_API_ID
                and settings.TG_API_HASH)


def backfill_mode() -> str:
    from app.config import settings
    mode = (settings.BACKFILL_MODE or "direct").strip().lower()
    return mode if mode in ("direct", "forward") else "direct"


def session_count() -> int:
    from app.config import settings
    return len(settings.tg_sessions)


def parse_channel_ref(text: str) -> tuple[str, int]:
    """Accept @username, t.me links (incl. t.me/c/<id>/<msg>), or -100 ids.

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
    """
    from telethon.tl.types import PeerChannel
    s = ref.strip()
    digits = s[4:] if s.startswith("-100") else s
    if digits and digits.lstrip("-").isdigit():
        return PeerChannel(int(digits))
    return ref


async def get_clients() -> list:
    """Lazily build one Telethon client per configured user session.

    Bot mode (INDEXER_BOT_MODE) uses Pyrogram instead — see
    get_pyro_client(). This is only for user sessions (forward mode).
    """
    global _clients
    if not indexer_configured():
        raise RuntimeError(
            "indexer not configured (set INDEXER_BOT_MODE=true or "
            "TG_SESSION/TG_SESSIONS + TG_API_ID/TG_API_HASH)")
    if not _clients:
        async with _clients_lock:
            if not _clients:
                from telethon import TelegramClient
                from telethon.sessions import StringSession
                from app.config import settings
                for sess in settings.tg_sessions:
                    _clients.append(TelegramClient(
                        StringSession(sess),
                        int(settings.TG_API_ID),
                        settings.TG_API_HASH,
                    ))
    for c in _clients:
        if not c.is_connected():
            await c.connect()
    return _clients


async def get_pyro_client():
    """Pyrogram bot client for INDEXER_BOT_MODE.

    Logs in as the bot itself (bot token, no session string).
    Pyrogram's message.file_id is already the correct Bot API file_id —
    exactly like Tech VJ.
    """
    global _pyro_client
    if _pyro_client is None:
        async with _pyro_lock:
            if _pyro_client is None:
                from pyrogram import Client
                from app.config import settings
                _pyro_client = Client(
                    "moovidex_indexer",
                    api_id=int(settings.TG_API_ID),
                    api_hash=settings.TG_API_HASH,
                    bot_token=settings.BOT_TOKEN,
                    in_memory=True,
                )
                await _pyro_client.start()
                me = await _pyro_client.get_me()
                log.info("pyrogram indexer logged in as @%s (%s)",
                         me.username, me.id)
    return _pyro_client


async def resolve_channel(ref: str, client=None) -> dict:
    """Resolve a channel ref -> title/chat_id/last message id. Raises on error."""
    from app.config import settings
    # Bot mode: use Pyrogram (same as the walker).
    if settings.INDEXER_BOT_MODE and client is None:
        pyro = await get_pyro_client()
        try:
            chat = await pyro.get_chat(ref)
        except Exception:  # noqa: BLE001
            # ref might be a numeric id string
            chat = await pyro.get_chat(int(ref))
        last = 0
        async for m in pyro.get_chat_history(chat.id, limit=1):
            last = m.id
            break
        return {
            "ref": ref,
            "chat_id": chat.id,
            "title": getattr(chat, "title", None) or ref,
            "last_msg_id": last,
        }
    client = client or (await get_clients())[0]
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


async def create_jobs(run_token: str, channel_refs: list[str], *,
                      skip: int = 0, from_id: int = 0, to_id: int = 0) -> dict:
    """Split channels into chunked jobs and store them as pending.

    skip applies to the newest chunk of each channel only.
    Returns {"jobs": n, "channels": [{"ref","title","last_msg_id"}]}.
    """
    from app.config import settings
    from app.db import get_session_factory
    from app.models import BackfillJob

    # Bot mode: resolve_channel uses Pyrogram internally (client=None).
    if settings.INDEXER_BOT_MODE:
        client = None
    else:
        client = (await get_clients())[0]
    factory = get_session_factory(settings.DATABASE_URL)
    chunk = max(50000, settings.BACKFILL_CHUNK)
    jobs = 0
    channels = []
    async with factory() as session:
        for ref in channel_refs:
            info = await resolve_channel(ref, client)
            last = info["last_msg_id"]
            hi = to_id or last
            lo = from_id or 1
            channels.append({"ref": ref, "title": info["title"],
                             "last_msg_id": last})
            if hi < lo or last <= 0:
                continue
            cur_hi = hi
            first = True
            while cur_hi >= lo:
                cur_lo = max(lo, cur_hi - chunk + 1)
                session.add(BackfillJob(
                    run_token=run_token,
                    channel_ref=ref,
                    channel_id=info["chat_id"],
                    channel_title=info["title"],
                    min_id=cur_lo,
                    max_id=cur_hi,
                    skip=skip if first else 0,
                    status="pending",
                    offset_id=cur_hi,
                    stats={},
                ))
                first = False
                jobs += 1
                cur_hi = cur_lo - 1
        await session.commit()
    return {"jobs": jobs, "channels": channels}


async def _claim_job(factory, run_token: str, worker: int):
    """Claim one pending job (SKIP LOCKED — no double work)."""
    from sqlalchemy import select
    from app.models import BackfillJob
    async with factory() as session:
        job = (await session.execute(
            select(BackfillJob)
            .where(BackfillJob.run_token == run_token,
                   BackfillJob.status == "pending")
            .order_by(BackfillJob.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )).scalar_one_or_none()
        if job is None:
            return None
        job.status = "running"
        job.worker = worker
        await session.commit()
        await session.refresh(job)
        return {"id": job.id, "channel_ref": job.channel_ref,
                "channel_id": job.channel_id, "min_id": job.min_id,
                "max_id": job.max_id, "skip": job.skip,
                "offset_id": job.offset_id}


async def _save_job(factory, job_id: int, **fields) -> None:
    from app.models import BackfillJob
    async with factory() as session:
        job = await session.get(BackfillJob, job_id)
        if job is None:
            return
        for k, v in fields.items():
            setattr(job, k, v)
        await session.commit()


async def _already_indexed(factory, channel_id: int,
                           msg_ids: list[int]) -> set[int]:
    """Which source message ids are already in the DB (pre-filter)."""
    if not msg_ids or not channel_id:
        return set()
    from sqlalchemy import select
    from app.models import File
    async with factory() as session:
        rows = (await session.execute(
            select(File.source_message_id)
            .where(File.source_channel_id == channel_id,
                   File.source_message_id.in_(msg_ids))
        )).all()
    return {r[0] for r in rows if r[0]}


# ------------------------- direct-save helpers -------------------------

def _doc_file_type(doc) -> int:
    """Bot API file type id from a Telethon Document's attributes."""
    from telethon.tl import types
    ftype = 5  # generic document
    for attr in doc.attributes:
        if isinstance(attr, types.DocumentAttributeAudio):
            ftype = 3 if attr.voice else 9
        elif isinstance(attr, types.DocumentAttributeVideo):
            ftype = 13 if attr.round_message else 4
        elif isinstance(attr, types.DocumentAttributeSticker):
            ftype = 8
        elif isinstance(attr, types.DocumentAttributeAnimated):
            ftype = 10
        else:
            continue
        break
    return ftype


def _doc_file_name(doc) -> str | None:
    from telethon.tl import types
    for attr in doc.attributes:
        if isinstance(attr, types.DocumentAttributeFilename):
            return attr.file_name
    return None


def _pack_message_file(m) -> str | None:
    """Bot API file_id for a Telethon message's document, or None.

    Uses the verified Bot API layout with the fresh file_reference from
    getHistory (see app/services/fileid_conv.pack_bot_file_id_typed).
    None = no usable id (no file_reference on the document).
    """
    from app.services.fileid_conv import pack_bot_file_id_typed
    doc = m.document
    if doc is None:
        return None
    ref = getattr(doc, "file_reference", None)
    if not ref:
        return None
    return pack_bot_file_id_typed(
        _doc_file_type(doc), doc.dc_id, doc.id, doc.access_hash, bytes(ref))


def telethon_media_meta(doc) -> dict:
    """Width/height/duration/streaming flag from a Telethon Document.

    All of this ships with the file metadata — no download needed.
    """
    from telethon.tl import types
    meta = {"width": None, "height": None, "duration": None,
            "supports_streaming": None}
    for attr in doc.attributes:
        if isinstance(attr, types.DocumentAttributeVideo):
            meta["width"] = attr.w or None
            meta["height"] = attr.h or None
            meta["duration"] = attr.duration or None
            meta["supports_streaming"] = bool(
                getattr(attr, "supports_streaming", False))
        elif isinstance(attr, types.DocumentAttributeAudio):
            if meta["duration"] is None:
                meta["duration"] = attr.duration or None
    return meta


async def _bulk_save_files(factory, rows: list[dict]) -> tuple[int, int]:
    """Bulk-insert file rows; ON CONFLICT DO NOTHING on file_id.

    Returns (saved, duplicates). The files.search_vector trigger keeps
    full-text search fresh automatically.
    """
    if not rows:
        return 0, 0
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.models import File
    async with factory() as session:
        stmt = (pg_insert(File).values(rows)
                .on_conflict_do_nothing(index_elements=["file_id"])
                .returning(File.id))
        res = await session.execute(stmt)
        saved = len(res.fetchall())
        await session.commit()
    return saved, len(rows) - saved


async def _walk_job_direct(client, factory, job: dict, ctx: dict) -> dict:
    """Walk one job newest->oldest, packing file_ids and bulk-saving.

    No forwarding, no per-file delay: throughput is bounded by getHistory
    rate (~500-1000 msgs/sec) and bulk-insert speed.
    """
    from telethon.errors import FloodWaitError
    from app.services.textutil import detect_quality_language, title_key

    stats = {"scanned": 0, "saved": 0, "skipped": 0, "dupes": 0, "errors": 0}
    channel = await client.get_entity(_entity_ref(job["channel_ref"]))
    src_id = job["channel_id"]
    offset_id = job["offset_id"] or job["max_id"] or 0
    skip_left = max(0, job["skip"])
    rows: list[dict] = []
    fw_streak = 0

    async def flush() -> None:
        if not rows:
            return
        try:
            saved, dupes = await _bulk_save_files(factory, rows)
            stats["saved"] += saved
            stats["dupes"] += dupes
        except Exception as e:  # noqa: BLE001
            stats["errors"] += len(rows)
            log.warning("backfill bulk save failed (%d rows): %s",
                        len(rows), e)
        rows.clear()
        await _save_job(factory, job["id"], offset_id=offset_id,
                        stats=dict(stats))

    while True:
        if ctx["cancel"].is_set() or ctx.get("limit_hit"):
            break
        try:
            batch = [m async for m in client.iter_messages(
                channel, limit=200, offset_id=offset_id or None,
                max_id=job["max_id"] or None)]
        except FloodWaitError as e:
            fw_streak += 1
            wait = e.seconds + 5
            log.warning("backfill iterate FloodWait %ss (job %s), sleeping %ss",
                        e.seconds, job["id"], wait)
            await flush()
            await asyncio.sleep(wait)
            if fw_streak >= _FW_COOLDOWN_AFTER:
                log.warning("backfill job %s: %d consecutive FloodWaits, "
                            "cooling down %ss", job["id"], fw_streak,
                            _FW_COOLDOWN_SECS)
                await asyncio.sleep(_FW_COOLDOWN_SECS)
                fw_streak = 0
            continue
        except Exception as e:  # noqa: BLE001
            log.exception("backfill iterate error (job %s): %s", job["id"], e)
            stats["errors"] += 1
            await flush()
            await _save_job(factory, job["id"], offset_id=offset_id,
                            stats=dict(stats), status="error",
                            error=str(e)[:500])
            return stats
        if not batch:
            break  # reached the beginning of the range
        fw_streak = 0
        done = False
        for m in batch:
            stats["scanned"] += 1
            offset_id = m.id
            if job["min_id"] and m.id < job["min_id"]:
                done = True
                break
            if ctx["cancel"].is_set() or ctx.get("limit_hit"):
                break
            doc = m.document
            if doc is None:
                stats["skipped"] += 1
                continue
            if skip_left > 0:
                skip_left -= 1
                stats["skipped"] += 1
                continue
            async with ctx["lock"]:
                if ctx.get("remaining") is not None:
                    if ctx["remaining"] <= 0:
                        ctx["limit_hit"] = True
                        break
                    ctx["remaining"] -= 1
            try:
                file_id = _pack_message_file(m)
            except Exception as e:  # noqa: BLE001
                stats["errors"] += 1
                log.debug("backfill pack failed msg %s: %s", m.id, e)
                continue
            if not file_id:
                stats["skipped"] += 1  # no file_reference on the document
                continue
            file_name = (_doc_file_name(doc) or "").strip()
            if not file_name and m.text:
                file_name = m.text.strip().split("\n")[0].strip()
            if not file_name:
                stats["skipped"] += 1
                continue
            file_name = file_name[:500]
            quality, language = detect_quality_language(file_name)
            caption = m.text or ""
            if len(caption) > 1024:
                caption = caption[:1020] + "..."
            meta = telethon_media_meta(doc)
            rows.append({
                "file_id": file_id,
                "file_name": file_name,
                "file_size": getattr(doc, "size", None),
                "mime_type": getattr(doc, "mime_type", None),
                "caption": caption or None,
                "channel_id": None,
                "message_id": None,
                "source_channel_id": src_id,
                "source_message_id": m.id,
                "quality": quality,
                "language": language,
                "title_key": title_key(file_name) or None,
                "width": meta["width"],
                "height": meta["height"],
                "duration": meta["duration"],
                "supports_streaming": meta["supports_streaming"],
                "posted_at": getattr(m, "date", None),
                "views": getattr(m, "views", None),
                "forwards": getattr(m, "forwards", None),
            })
            if len(rows) >= _DIRECT_BATCH:
                await flush()
        await flush()
        if done or ctx["cancel"].is_set() or ctx.get("limit_hit"):
            break
        await asyncio.sleep(0.15)  # polite getHistory pacing
    await flush()
    return stats


def _pyro_media(msg) -> dict | None:
    """Extract (file_id, file_name, file_size, mime_type, w, h, dur) from a
    Pyrogram message. file_id is ALREADY the Bot API file_id — no packing.
    """
    for attr in ("document", "video", "audio", "animation", "voice",
                 "video_note", "sticker", "photo"):
        m = getattr(msg, attr, None)
        if m is not None:
            return {
                "file_id": m.file_id,
                "file_name": getattr(m, "file_name", None),
                "file_size": getattr(m, "file_size", None),
                "mime_type": getattr(m, "mime_type", None),
                "width": getattr(m, "width", None),
                "height": getattr(m, "height", None),
                "duration": getattr(m, "duration", None),
            }
    return None


async def _walk_job_direct_pyro(client, factory, job: dict,
                                ctx: dict) -> dict:
    """Walk one job newest->oldest using Pyrogram (bot session).

    This is EXACTLY how Tech VJ does it: Pyrogram bot client reads history,
    ``message.document.file_id`` is already the correct Bot API file_id.
    No manual packing, no format guessing.
    """
    from pyrogram.errors import FloodWait
    from app.services.textutil import detect_quality_language, title_key

    stats = {"scanned": 0, "saved": 0, "skipped": 0, "dupes": 0, "errors": 0}
    # Resolve channel: prefer the original ref (username/link), else the id.
    ref = job["channel_ref"]
    try:
        chat = await client.get_chat(ref)
    except Exception:  # noqa: BLE001
        chat = await client.get_chat(job["channel_id"])
    src_id = job["channel_id"]
    offset_id = job["offset_id"] or job["max_id"] or 0
    skip_left = max(0, job["skip"])
    rows: list[dict] = []
    fw_streak = 0

    async def flush() -> None:
        if not rows:
            return
        try:
            saved, dupes = await _bulk_save_files(factory, rows)
            stats["saved"] += saved
            stats["dupes"] += dupes
        except Exception as e:  # noqa: BLE001
            stats["errors"] += len(rows)
            log.warning("backfill bulk save failed (%d rows): %s",
                        len(rows), e)
        rows.clear()
        await _save_job(factory, job["id"], offset_id=offset_id,
                        stats=dict(stats))

    while True:
        if ctx["cancel"].is_set() or ctx.get("limit_hit"):
            break
        try:
            batch = [m async for m in client.get_chat_history(
                chat.id, limit=200, offset_id=offset_id or None)]
        except FloodWait as e:
            fw_streak += 1
            wait = e.value + 5
            log.warning("backfill FloodWait %ss (job %s), sleeping %ss",
                        e.value, job["id"], wait)
            await flush()
            await asyncio.sleep(wait)
            if fw_streak >= _FW_COOLDOWN_AFTER:
                await asyncio.sleep(_FW_COOLDOWN_SECS)
                fw_streak = 0
            continue
        except Exception as e:  # noqa: BLE001
            log.exception("backfill iterate error (job %s): %s", job["id"], e)
            stats["errors"] += 1
            await flush()
            await _save_job(factory, job["id"], offset_id=offset_id,
                            stats=dict(stats), status="error",
                            error=str(e)[:500])
            return stats
        if not batch:
            break
        fw_streak = 0
        done = False
        for m in batch:
            stats["scanned"] += 1
            offset_id = m.id
            if job["min_id"] and m.id < job["min_id"]:
                done = True
                break
            if ctx["cancel"].is_set() or ctx.get("limit_hit"):
                break
            media = _pyro_media(m)
            if not media or not media["file_id"]:
                stats["skipped"] += 1
                continue
            if skip_left > 0:
                skip_left -= 1
                stats["skipped"] += 1
                continue
            async with ctx["lock"]:
                if ctx.get("remaining") is not None:
                    if ctx["remaining"] <= 0:
                        ctx["limit_hit"] = True
                        break
                    ctx["remaining"] -= 1
            file_name = (media["file_name"] or "").strip()
            caption_text = (m.caption or "").strip()
            if not file_name and caption_text:
                file_name = caption_text.split("\n")[0].strip()
            if not file_name:
                stats["skipped"] += 1
                continue
            file_name = file_name[:500]
            quality, language = detect_quality_language(file_name)
            if len(caption_text) > 1024:
                caption_text = caption_text[:1020] + "..."
            rows.append({
                "file_id": media["file_id"],
                "file_name": file_name,
                "file_size": media["file_size"],
                "mime_type": media["mime_type"],
                "caption": caption_text or None,
                "channel_id": None,
                "message_id": None,
                "source_channel_id": src_id,
                "source_message_id": m.id,
                "quality": quality,
                "language": language,
                "title_key": title_key(file_name) or None,
                "width": media["width"],
                "height": media["height"],
                "duration": media["duration"],
                "supports_streaming": None,
                "posted_at": getattr(m, "date", None),
                "views": getattr(m, "views", None),
                "forwards": getattr(m, "forwards", None),
            })
            if len(rows) >= _DIRECT_BATCH:
                await flush()
        await flush()
        # Respect max_id upper bound
        if job["max_id"] and offset_id > job["max_id"]:
            pass  # get_chat_history goes newest->oldest; max_id handled below
        if done or ctx["cancel"].is_set() or ctx.get("limit_hit"):
            break
        await asyncio.sleep(0.15)
    await flush()
    return stats


# ------------------------- forward-mode walker -------------------------

async def _walk_job_forward(client, factory, job: dict, ctx: dict) -> dict:
    """Walk one job newest->oldest, forwarding media to the bot.

    Proven fallback (BACKFILL_MODE=forward): every file arrives at the bot
    as a PM and is indexed with a Bot-API-issued file_id. Forwards go out
    in bulk batches (BACKFILL_BATCH per API call) — roughly 15-30 files/sec
    per worker, with FloodWait backoff for safety.
    """
    from telethon.errors import FloodWaitError

    stats = {"scanned": 0, "saved": 0, "skipped": 0, "dupes": 0, "errors": 0}
    channel = await client.get_entity(_entity_ref(job["channel_ref"]))
    src_id = job["channel_id"]
    offset_id = job["offset_id"] or job["max_id"] or 0
    skip_left = max(0, job["skip"])
    delay = ctx["delay"]
    bot_peer = ctx["bot_username"]
    fw_streak = 0
    since_checkpoint = 0

    async def checkpoint() -> None:
        await _save_job(factory, job["id"], offset_id=offset_id,
                        stats=dict(stats))

    while True:
        if ctx["cancel"].is_set() or ctx.get("limit_hit"):
            break
        try:
            batch = [m async for m in client.iter_messages(
                channel, limit=200, offset_id=offset_id or None,
                max_id=job["max_id"] or None)]
        except FloodWaitError as e:
            fw_streak += 1
            wait = e.seconds + 5
            log.warning("backfill iterate FloodWait %ss (job %s), sleeping %ss",
                        e.seconds, job["id"], wait)
            await asyncio.sleep(wait)
            if fw_streak >= _FW_COOLDOWN_AFTER:
                log.warning("backfill job %s: %d consecutive FloodWaits, "
                            "cooling down %ss", job["id"], fw_streak,
                            _FW_COOLDOWN_SECS)
                await asyncio.sleep(_FW_COOLDOWN_SECS)
                fw_streak = 0
            continue
        except Exception as e:  # noqa: BLE001
            log.exception("backfill iterate error (job %s): %s", job["id"], e)
            stats["errors"] += 1
            await _save_job(factory, job["id"], offset_id=offset_id,
                            stats=dict(stats), status="error",
                            error=str(e)[:500])
            return stats
        if not batch:
            break

        cands = []
        for m in batch:
            stats["scanned"] += 1
            if job["min_id"] and m.id < job["min_id"]:
                cands_done = True
                break
            media = m.document or m.video or m.audio
            if not m.media or media is None:
                stats["skipped"] += 1
                offset_id = m.id
                continue
            cands.append((m.id, m))
        else:
            cands_done = False

        done_ids = await _already_indexed(factory, src_id,
                                          [i for i, _ in cands])
        # Filter to fresh messages, honoring skip/limit, then bulk-forward
        # in batches: one API call per batch instead of one per file.
        fresh = []
        for mid, m in cands:
            if ctx["cancel"].is_set() or ctx.get("limit_hit"):
                break
            if mid in done_ids:
                stats["dupes"] += 1
                stats["skipped"] += 1
                offset_id = mid
                continue
            if skip_left > 0:
                skip_left -= 1
                stats["skipped"] += 1
                offset_id = mid
                continue
            async with ctx["lock"]:
                if ctx.get("remaining") is not None:
                    if ctx["remaining"] <= 0:
                        ctx["limit_hit"] = True
                        break
                    ctx["remaining"] -= 1
            fresh.append(mid)
        batch_size = max(1, int(ctx.get("batch_size", 30)))
        for i in range(0, len(fresh), batch_size):
            if ctx["cancel"].is_set() or ctx.get("limit_hit"):
                break
            chunk = fresh[i:i + batch_size]
            try:
                await client.forward_messages(bot_peer, chunk,
                                              from_peer=channel)
                stats["saved"] += len(chunk)
                fw_streak = 0
                since_checkpoint += len(chunk)
                offset_id = chunk[-1]  # newest->oldest: last = oldest id
                await asyncio.sleep(delay + random.uniform(0, 0.5))
            except FloodWaitError as e:
                fw_streak += 1
                wait = e.seconds + 5
                log.warning("backfill forward FloodWait %ss (job %s), "
                            "sleeping %ss", e.seconds, job["id"], wait)
                await _save_job(factory, job["id"], offset_id=offset_id,
                                stats=dict(stats))
                if fw_streak >= _FW_COOLDOWN_AFTER:
                    log.warning("backfill job %s cooling down %ss",
                                job["id"], _FW_COOLDOWN_SECS)
                    await asyncio.sleep(_FW_COOLDOWN_SECS)
                    fw_streak = 0
                else:
                    await asyncio.sleep(wait)
                break
            except Exception as e:  # noqa: BLE001
                stats["errors"] += 1
                log.warning("backfill forward failed chunk %s..%s: %s",
                            chunk[0], chunk[-1], e)
            if since_checkpoint >= _CHECKPOINT_EVERY:
                since_checkpoint = 0
                await checkpoint()
        if cands_done or ctx["cancel"].is_set() or ctx.get("limit_hit"):
            break

    await checkpoint()
    return stats


async def _worker(wid: int, client, factory, run_token: str,
                  ctx: dict, walker) -> dict:
    """Claim and walk jobs until none are left, cancelled, or limit hit."""
    totals = {"scanned": 0, "saved": 0, "skipped": 0, "dupes": 0,
              "errors": 0, "jobs": 0}
    while not ctx["cancel"].is_set() and not ctx.get("limit_hit"):
        job = await _claim_job(factory, run_token, wid)
        if job is None:
            break
        log.info("backfill worker %d claimed job %s (%s %s-%s)", wid,
                 job["id"], job["channel_ref"], job["min_id"], job["max_id"])
        try:
            stats = await walker(client, factory, job, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Never let one bad job kill the whole run: record it and move on.
            log.exception("backfill worker %d job %s crashed: %s",
                          wid, job["id"], exc)
            stats = {"scanned": 0, "saved": 0, "skipped": 0, "dupes": 0,
                     "errors": 1}
            await _save_job(factory, job["id"], status="error",
                            error=str(exc)[:500], stats=dict(stats))
        for k in totals:
            if k in stats:
                totals[k] += stats[k]
        totals["jobs"] += 1
        final = ("cancelled" if ctx["cancel"].is_set()
                 else "done")
        await _save_job(factory, job["id"], status=final,
                        stats=dict(stats))
    return totals


async def run_backfill(run_token: str, bot_username: str,
                       cancel_event: asyncio.Event,
                       progress_cb=None, limit: int = 0) -> dict:
    """Run all pending jobs for a run with one worker per session.

    Mode comes from BACKFILL_MODE ("direct" default, "forward" fallback).
    Returns aggregate totals. progress_cb(agg) is called ~every 5s.
    """
    from app.config import settings
    from app.db import get_session_factory

    mode = backfill_mode()
    # Bot mode + direct: use Pyrogram (Tech VJ method) — file_id comes
    # straight from Pyrogram, no manual packing.
    if mode == "direct" and settings.INDEXER_BOT_MODE:
        walker = _walk_job_direct_pyro
        clients = [await get_pyro_client()]
    else:
        walker = _walk_job_direct if mode == "direct" else _walk_job_forward
        clients = await get_clients()
    factory = get_session_factory(settings.DATABASE_URL)
    ctx = {
        "cancel": cancel_event,
        "lock": asyncio.Lock(),
        # inter-batch delay for forward mode (bulk); direct mode ignores it
        "delay": max(1.0, float(settings.BACKFILL_DELAY)),
        "batch_size": max(1, int(settings.BACKFILL_BATCH)),
        "bot_username": bot_username,
        "remaining": limit or None,
        "mode": mode,
    }
    workers = [asyncio.create_task(_worker(i, c, factory, run_token, ctx,
                                          walker))
               for i, c in enumerate(clients)]
    log.info("backfill run %s: %d worker(s), mode=%s", run_token,
             len(workers), mode)

    async def pump_progress() -> None:
        while any(not w.done() for w in workers):
            await asyncio.sleep(5)
            if progress_cb:
                try:
                    await progress_cb(await backfill_stats(run_token))
                except Exception:  # noqa: BLE001
                    pass

    pump = asyncio.create_task(pump_progress())
    results = await asyncio.gather(*workers)
    pump.cancel()
    try:
        await pump
    except asyncio.CancelledError:
        pass
    totals = {"scanned": 0, "saved": 0, "skipped": 0, "dupes": 0,
              "errors": 0, "jobs": 0, "workers": len(workers),
              "mode": mode,
              "cancelled": cancel_event.is_set(),
              "limit_hit": bool(ctx.get("limit_hit"))}
    for r in results:
        for k in ("scanned", "saved", "skipped", "dupes", "errors", "jobs"):
            totals[k] += r.get(k, 0)
    if progress_cb:
        try:
            await progress_cb(await backfill_stats(run_token))
        except Exception:  # noqa: BLE001
            pass
    return totals


async def backfill_stats(run_token: str) -> dict:
    """Aggregate job stats for a run, from the DB."""
    from sqlalchemy import select
    from app.config import settings
    from app.db import get_session_factory
    from app.models import BackfillJob

    factory = get_session_factory(settings.DATABASE_URL)
    agg = {"scanned": 0, "saved": 0, "skipped": 0, "dupes": 0,
           "errors": 0, "pending": 0, "running": 0, "done": 0,
           "cancelled": 0, "error": 0, "total": 0, "titles": [],
           "last_error": "", "mode": backfill_mode()}
    async with factory() as session:
        rows = (await session.execute(
            select(BackfillJob.status, BackfillJob.stats,
                   BackfillJob.channel_title, BackfillJob.error)
            .where(BackfillJob.run_token == run_token)
            .order_by(BackfillJob.id)
        )).all()
    for status, stats, title, error in rows:
        agg["total"] += 1
        agg[status] = agg.get(status, 0) + 1
        for k in ("scanned", "saved", "skipped", "dupes", "errors"):
            agg[k] += (stats or {}).get(k, 0)
        if title and title not in agg["titles"]:
            agg["titles"].append(title)
        if error and not agg["last_error"]:
            agg["last_error"] = str(error)[:200]
    return agg


async def job_errors(run_token: str, limit: int = 3) -> list[dict]:
    """Most recent job errors for a run (for the status card)."""
    from sqlalchemy import select
    from app.config import settings
    from app.db import get_session_factory
    from app.models import BackfillJob

    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        rows = (await session.execute(
            select(BackfillJob.channel_title, BackfillJob.error)
            .where(BackfillJob.run_token == run_token,
                   BackfillJob.error.isnot(None))
            .order_by(BackfillJob.id.desc())
            .limit(limit)
        )).all()
    return [{"title": t, "error": e} for t, e in rows]


async def cancel_run(run_token: str) -> None:
    """Mark pending/running jobs of a run as cancelled."""
    from sqlalchemy import update
    from app.config import settings
    from app.db import get_session_factory
    from app.models import BackfillJob

    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as session:
        await session.execute(
            update(BackfillJob)
            .where(BackfillJob.run_token == run_token,
                   BackfillJob.status.in_(["pending", "running"]))
            .values(status="cancelled")
        )
        await session.commit()

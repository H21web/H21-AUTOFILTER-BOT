"""Channel history backfill engine — fast, resumable, built for 1M+ files.

How it works:
1. A Pyrogram USER session (TG_SESSION) reads channel history.
   Bots cannot read history (BOT_METHOD_INVALID) — user session is required.
2. Media messages are bulk-forwarded (100/batch) to the dump channel.
3. The bot's own auto-index (on_channel_post) saves them with native
   Bot API file_ids — the only reliable file_id source.

Design for large data:
- Streams history (never loads all IDs into memory).
- DB-level dedupe in auto-index (ON CONFLICT DO NOTHING) — safe to run
  while auto-index or another backfill is active.
- Job state in PostgreSQL — survives bot restarts, resumable.
- Progress saved every 1,000 files (not every file — avoids DB spam).
- FloodWait: exponential backoff, never crashes the job.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("backfill")

# Tuning — safe for 1M+ file jobs
PAGE_SIZE = 200       # messages per get_chat_history call (Telegram max)
FORWARD_BATCH = 100   # messages per forward_messages call (Telegram max)
DB_SAVE_EVERY = 1000  # files between DB progress saves
PROGRESS_EVERY_SEC = 5  # min seconds between Telegram progress edits
FORWARD_DELAY = 0.5   # seconds between forward calls (flood safety)

_client = None
_client_lock = asyncio.Lock()
# job_id -> asyncio.Event (set = cancel requested)
_cancel_events: dict[int, asyncio.Event] = {}
# job_id -> running task
_tasks: dict[int, asyncio.Task] = {}


@dataclass
class JobProgress:
    job_id: int
    channel_ref: str
    scanned: int = 0
    forwarded: int = 0
    skipped: int = 0
    errors: int = 0
    started_at: float = field(default_factory=time.time)
    last_edit: float = 0.0

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    @property
    def rate(self) -> float:
        return self.forwarded / self.elapsed if self.elapsed > 0 else 0.0


async def get_client():
    """Pyrogram user-client singleton (lazy, thread-safe)."""
    global _client
    async with _client_lock:
        if _client is not None:
            return _client
        from pyrogram import Client
        from app.config import settings

        api_id = int(settings.TG_API_ID or 0)
        api_hash = (settings.TG_API_HASH or "").strip()
        session = (settings.TG_SESSION or "").strip()
        if not (api_id and api_hash and session):
            raise RuntimeError(
                "Backfill needs TG_API_ID, TG_API_HASH and TG_SESSION env vars. "
                "Generate the session with make-session.py on your PC."
            )
        _client = Client(
            name="backfill",
            api_id=api_id,
            api_hash=api_hash,
            session_string=session,
            in_memory=True,
        )
        await _client.start()
        me = await _client.get_me()
        log.info("Backfill client started as @%s (%s)",
                 getattr(me, "username", "?"), me.id)
        return _client


def _resolve_chat(ref: str):
    """Turn @username / t.me link / -100id into something Pyrogram accepts."""
    ref = ref.strip()
    if ref.startswith("https://t.me/"):
        ref = ref.split("https://t.me/")[1].split("/")[0].split("?")[0]
        if not ref.startswith("@") and not ref.startswith("+"):
            ref = "@" + ref
    if ref.startswith("-100"):
        return int(ref)
    try:
        return int(ref)
    except ValueError:
        return ref  # @username


def _is_media(msg) -> bool:
    return bool(getattr(msg, "document", None)
               or getattr(msg, "video", None)
               or getattr(msg, "audio", None))


def request_cancel(job_id: int) -> bool:
    ev = _cancel_events.get(job_id)
    if ev and not ev.is_set():
        ev.set()
        return True
    return False


def is_running(job_id: int) -> bool:
    t = _tasks.get(job_id)
    return bool(t and not t.done())


async def get_active_job_id() -> int | None:
    from app.db import get_session_factory
    from app.config import settings
    from app.models import BackfillJob
    from sqlalchemy import select

    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        row = (await s.execute(
            select(BackfillJob.id)
            .where(BackfillJob.status == "running")
            .order_by(BackfillJob.id.desc())
            .limit(1)
        )).scalar_one_or_none()
        return row


async def _save_progress(job_id: int, prog: JobProgress,
                         offset_id: int | None,
                         status: str | None = None,
                         error_msg: str | None = None) -> None:
    from app.db import get_session_factory
    from app.config import settings
    from app.models import BackfillJob

    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        job = await s.get(BackfillJob, job_id)
        if not job:
            return
        job.stats = {"scanned": prog.scanned, "forwarded": prog.forwarded,
                     "skipped": prog.skipped, "errors": prog.errors}
        if offset_id is not None:
            job.offset_id = offset_id
        if status:
            job.status = status
        if error_msg is not None:
            job.error = error_msg[:2000]
        await s.commit()


async def create_job(channel_ref: str, skip: int = 0,
                     min_id: int = 0, max_id: int = 0) -> int:
    """Insert a pending job row, return its id."""
    import uuid
    from app.db import get_session_factory
    from app.config import settings
    from app.models import BackfillJob

    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        job = BackfillJob(
            run_token=f"bf-{uuid.uuid4().hex[:12]}",
            channel_ref=channel_ref, skip=skip,
            min_id=min_id, max_id=max_id, status="pending",
        )
        s.add(job)
        await s.commit()
        await s.refresh(job)
        return job.id


async def _run_job(job_id: int, progress_cb=None) -> None:
    """Background worker — walks history, forwards media, saves state."""
    from app.db import get_session_factory
    from app.config import settings
    from app.models import BackfillJob
    from pyrogram.errors import FloodWait

    factory = get_session_factory(settings.DATABASE_URL)
    async with factory() as s:
        job = await s.get(BackfillJob, job_id)
        if not job:
            return
        channel_ref, skip = job.channel_ref, job.skip
        min_id, max_id = job.min_id, job.max_id
        resume_from = job.offset_id or None
        job.status = "running"
        await s.commit()

    prog = JobProgress(job_id=job_id, channel_ref=channel_ref)
    cancel_ev = asyncio.Event()
    _cancel_events[job_id] = cancel_ev

    try:
        client = await get_client()
        chat = await client.get_chat(_resolve_chat(channel_ref))

        # Dump target: first INDEX_CHANNEL (bot must see it as a channel post)
        dump_targets = settings.index_channels
        if not dump_targets:
            raise RuntimeError(
                "Set INDEX_CHANNELS env (the dump channel id) for backfill.")
        dump_id = dump_targets[0]

        # Persist resolved channel info
        async with factory() as s:
            job = await s.get(BackfillJob, job_id)
            if job:
                job.channel_id = chat.id
                job.channel_title = getattr(chat, "title", None)
                await s.commit()

        # Walk newest -> oldest. offset_id = resume point (exclusive).
        offset_id = resume_from or max_id or 0
        to_skip = skip
        batch: list[int] = []
        since_db_save = 0
        backoff = 1.0

        log.info("Backfill #%d started: %s skip=%d range=%d..%d resume=%s",
                 job_id, channel_ref, skip, min_id, max_id, offset_id)

        while True:
            if cancel_ev.is_set():
                await _save_progress(job_id, prog, offset_id or None,
                                     status="cancelled")
                log.info("Backfill #%d aborted at msg %s", job_id, offset_id)
                return

            try:
                page = [m async for m in client.get_chat_history(
                    chat.id, limit=PAGE_SIZE, offset_id=offset_id)]
                backoff = 1.0
            except FloodWait as e:
                wait = min(e.value + 2, 300)
                log.warning("Backfill #%d FloodWait %ds", job_id, wait)
                await asyncio.sleep(wait)
                continue
            except Exception as exc:  # noqa: BLE001
                log.warning("Backfill #%d history error: %s — retry in %.0fs",
                            job_id, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                prog.errors += 1
                continue

            if not page:
                break  # reached the beginning

            for m in page:
                mid = m.id
                offset_id = mid  # next page continues below this
                if min_id and mid < min_id:
                    page = []  # signal: range complete
                    break
                if max_id and mid > max_id:
                    continue
                prog.scanned += 1
                if not _is_media(m):
                    continue  # avoid non-media messages
                if to_skip > 0:
                    to_skip -= 1
                    prog.skipped += 1
                    continue
                batch.append(mid)

                if len(batch) >= FORWARD_BATCH:
                    ok = await _forward_batch(
                        client, chat.id, dump_id, batch, prog, cancel_ev)
                    batch.clear()
                    since_db_save += FORWARD_BATCH
                    if not ok:  # cancelled during forward
                        await _save_progress(job_id, prog, offset_id,
                                             status="cancelled")
                        return
                    if since_db_save >= DB_SAVE_EVERY:
                        await _save_progress(job_id, prog, offset_id)
                        since_db_save = 0
                    if progress_cb and time.time() - prog.last_edit >= PROGRESS_EVERY_SEC:
                        prog.last_edit = time.time()
                        await _safe_cb(progress_cb, prog)

            if not page:
                break  # min_id range hit

        # Flush remainder
        if batch and not cancel_ev.is_set():
            await _forward_batch(client, chat.id, dump_id, batch, prog,
                                 cancel_ev)

        final = "cancelled" if cancel_ev.is_set() else "done"
        await _save_progress(job_id, prog, offset_id or None, status=final)
        if progress_cb:
            await _safe_cb(progress_cb, prog, final=True)
        log.info("Backfill #%d %s: forwarded=%d scanned=%d errors=%d",
                 job_id, final, prog.forwarded, prog.scanned, prog.errors)

    except Exception as exc:  # noqa: BLE001 — never leave a job stuck
        log.exception("Backfill #%d crashed", job_id)
        await _save_progress(job_id, prog, None, status="error",
                             error_msg=str(exc))
        if progress_cb:
            await _safe_cb(progress_cb, prog, final=True, error=str(exc))
    finally:
        _cancel_events.pop(job_id, None)
        _tasks.pop(job_id, None)


async def _forward_batch(client, from_chat: int, dump_id: int,
                         batch: list[int], prog: JobProgress,
                         cancel_ev: asyncio.Event) -> bool:
    """Forward one batch. Returns False if cancelled."""
    from pyrogram.errors import FloodWait

    backoff = 1.0
    while True:
        if cancel_ev.is_set():
            return False
        try:
            await client.forward_messages(
                chat_id=dump_id, from_chat_id=from_chat,
                message_ids=batch)
            prog.forwarded += len(batch)
            await asyncio.sleep(FORWARD_DELAY)
            return True
        except FloodWait as e:
            wait = min(e.value + 2, 300)
            log.warning("Forward FloodWait %ds (batch %d)", wait, len(batch))
            await asyncio.sleep(wait)
        except Exception as exc:  # noqa: BLE001
            # Split-and-retry: one bad message must not kill the batch
            if len(batch) > 1:
                mid = len(batch) // 2
                log.warning("Batch failed (%s) — splitting %d",
                            exc, len(batch))
                ok1 = await _forward_batch(client, from_chat, dump_id,
                                           batch[:mid], prog, cancel_ev)
                ok2 = await _forward_batch(client, from_chat, dump_id,
                                           batch[mid:], prog, cancel_ev)
                return ok1 and ok2
            log.warning("Skipping bad message %d: %s", batch[0], exc)
            prog.errors += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            return True


async def _safe_cb(cb, prog: JobProgress, final: bool = False,
                   error: str | None = None) -> None:
    try:
        await cb(prog, final=final, error=error)
    except Exception:  # noqa: BLE001
        log.debug("Progress callback failed", exc_info=True)


def launch(job_id: int, progress_cb=None) -> asyncio.Task:
    """Start the background worker for a job."""
    task = asyncio.get_event_loop().create_task(_run_job(job_id, progress_cb))
    _tasks[job_id] = task
    return task


def format_progress(prog: JobProgress, final: bool = False,
                    error: str | None = None) -> str:
    bar_w = 12
    pct = (prog.forwarded / max(prog.scanned, 1)) * 100
    filled = int(bar_w * min(pct, 100) / 100)
    bar = "█" * filled + "░" * (bar_w - filled)
    mins, secs = divmod(int(prog.elapsed), 60)
    head = "✅ Done" if final and not error else "❌ Failed" if error else "📥 Backfilling"
    txt = (
        f"{head} — <code>{prog.channel_ref}</code>\n"
        f"{bar} {pct:.1f}%\n"
        f"Forwarded: <b>{prog.forwarded:,}</b> | "
        f"Scanned: {prog.scanned:,} | Errors: {prog.errors}\n"
        f"⏱ {mins}m {secs}s • {prog.rate:.0f} files/sec"
    )
    if error:
        txt += f"\n⚠️ {error[:300]}"
    return txt

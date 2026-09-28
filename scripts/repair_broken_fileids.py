"""
Repair broken file_ids in the database.

During 'direct' backfill with INDEXER_BOT_MODE=True, a bug in _pyro_media caused
file_ids to be saved without media_id and access_hash (only 47 decoded bytes instead
of the required 70). These broken file_ids produce:
  "Wrong remote file identifier specified: can't unserialize it"

This script:
1. Scans all files in the DB that have a broken/truncated file_id (< 80 chars is a strong signal)
2. For each broken record that has a source_channel_id + source_message_id, re-fetches
   the message from Telegram via Pyrogram and rebuilds the correct file_id.
3. Updates the DB record in-place (same row id, same file_name, just new file_id).

Usage:
    python scripts/repair_broken_fileids.py
    python scripts/repair_broken_fileids.py --dry-run   # just count, don't update
    python scripts/repair_broken_fileids.py --min-id 1 --max-id 100000  # range
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

# Make sure app/ is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("repair_fileids")

# Minimum length of a valid Pyrogram file_id string (type+dc+fileref+id+ah+tail)
# Empirically: document with 32-byte file_ref = ~86 chars. Use 75 as safe threshold.
_MIN_VALID_FID_LEN = 75


def is_broken_fid(fid: str) -> bool:
    """True if the file_id is too short to contain media_id+access_hash."""
    return len(fid) < _MIN_VALID_FID_LEN


async def main(dry_run: bool, min_id: int, max_id: int) -> None:
    from app.config import settings
    from app.db import get_session_factory
    from app.models import File
    from sqlalchemy import select, update

    factory = get_session_factory(settings.DATABASE_URL)

    # ---- count broken records ----
    log.info("Scanning DB for broken file_ids (len < %d)...", _MIN_VALID_FID_LEN)
    async with factory() as session:
        stmt = select(File.id, File.file_id, File.file_name,
                      File.source_channel_id, File.source_message_id)
        if min_id:
            stmt = stmt.where(File.id >= min_id)
        if max_id:
            stmt = stmt.where(File.id <= max_id)
        rows = (await session.execute(stmt)).fetchall()

    broken = [r for r in rows if is_broken_fid(r.file_id or "")]
    log.info("Total rows scanned: %d | Broken file_ids: %d", len(rows), len(broken))

    if not broken:
        log.info("Nothing to fix!")
        return

    if dry_run:
        for r in broken[:20]:
            log.info("  [DRY-RUN] id=%d name=%r fid_len=%d src_ch=%s src_msg=%s",
                     r.id, r.file_name[:50], len(r.file_id or ""),
                     r.source_channel_id, r.source_message_id)
        if len(broken) > 20:
            log.info("  ... and %d more", len(broken) - 20)
        return

    # ---- connect Pyrogram ----
    from pyrogram import Client
    from pyrogram.errors import FloodWait
    from pyrogram.file_id import FileId, FileType
    from pyrogram import raw as pyro_raw

    if settings.INDEXER_BOT_MODE:
        log.info("Connecting Pyrogram as bot...")
        pyro = Client(
            "repair_indexer",
            api_id=int(settings.TG_API_ID),
            api_hash=settings.TG_API_HASH,
            bot_token=settings.BOT_TOKEN,
            in_memory=True,
        )
    else:
        if not settings.tg_sessions:
            log.error("No TG_SESSIONS configured and INDEXER_BOT_MODE is off. Cannot connect.")
            return
        log.info("Connecting Pyrogram as user session...")
        pyro = Client(
            "repair_indexer",
            api_id=int(settings.TG_API_ID),
            api_hash=settings.TG_API_HASH,
            session_string=settings.tg_sessions[0],
            in_memory=True,
        )

    await pyro.start()
    me = await pyro.get_me()
    log.info("Connected as @%s (%s)", me.username, me.id)

    fixed = 0
    skipped = 0
    errors = 0

    # Group by source_channel_id to minimise get_messages calls
    by_channel: dict[int, list] = {}
    unfetchable = []
    for r in broken:
        if r.source_channel_id and r.source_message_id:
            by_channel.setdefault(r.source_channel_id, []).append(r)
        else:
            unfetchable.append(r)

    log.info("%d broken records have a source channel (can re-fetch), "
             "%d do not (will be skipped).", len(broken) - len(unfetchable), len(unfetchable))
    skipped += len(unfetchable)

    for ch_id, ch_rows in by_channel.items():
        log.info("Processing channel %s (%d broken records)...", ch_id, len(ch_rows))
        msg_ids = [r.source_message_id for r in ch_rows]
        id_to_row = {r.source_message_id: r for r in ch_rows}

        # fetch in batches of 200
        for batch_start in range(0, len(msg_ids), 200):
            batch = msg_ids[batch_start:batch_start + 200]
            try:
                messages = await pyro.get_messages(ch_id, message_ids=batch)
            except FloodWait as e:
                log.warning("FloodWait %ss — sleeping...", e.value)
                await asyncio.sleep(e.value + 2)
                try:
                    messages = await pyro.get_messages(ch_id, message_ids=batch)
                except Exception as exc:
                    log.error("Failed to fetch messages from %s: %s", ch_id, exc)
                    errors += len(batch)
                    continue
            except Exception as exc:
                log.error("Failed to fetch messages from %s: %s", ch_id, exc)
                errors += len(batch)
                continue

            for msg in messages:
                if msg is None or not msg.id:
                    continue
                orig_row = id_to_row.get(msg.id)
                if not orig_row:
                    continue

                # Build file_id from raw MTProto document
                raw_msg = getattr(msg, "_raw", None)
                raw_media = getattr(raw_msg, "media", None) if raw_msg else None
                raw_doc = getattr(raw_media, "document", None) if raw_media else None

                new_fid = None
                if raw_doc and isinstance(raw_doc, pyro_raw.types.Document):
                    if raw_doc.file_reference:
                        try:
                            fid_obj = FileId(
                                file_type=FileType.DOCUMENT,
                                dc_id=raw_doc.dc_id,
                                media_id=raw_doc.id,
                                access_hash=raw_doc.access_hash,
                                file_reference=bytes(raw_doc.file_reference),
                            )
                            new_fid = fid_obj.encode()
                        except Exception as e:
                            log.warning("Could not build file_id for row %d: %s",
                                        orig_row.id, e)

                if not new_fid or is_broken_fid(new_fid):
                    log.warning("Row id=%d: could not get valid file_id (msg may be deleted)",
                                orig_row.id)
                    errors += 1
                    continue

                # Update DB
                try:
                    async with factory() as session:
                        await session.execute(
                            update(File)
                            .where(File.id == orig_row.id)
                            .values(file_id=new_fid)
                        )
                        await session.commit()
                    fixed += 1
                    log.info("Fixed row id=%d (%r): %d -> %d chars",
                             orig_row.id, orig_row.file_name[:40],
                             len(orig_row.file_id), len(new_fid))
                except Exception as exc:
                    log.error("DB update failed for row %d: %s", orig_row.id, exc)
                    errors += 1

            await asyncio.sleep(0.5)  # polite pacing

    await pyro.stop()
    log.info("Done! Fixed: %d | Skipped (no source): %d | Errors: %d",
             fixed, skipped, errors)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Repair broken file_ids in DB")
    parser.add_argument("--dry-run", action="store_true",
                        help="Just count broken records, don't update anything")
    parser.add_argument("--min-id", type=int, default=0,
                        help="Only process File rows with id >= this value")
    parser.add_argument("--max-id", type=int, default=0,
                        help="Only process File rows with id <= this value (0 = no limit)")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run, min_id=args.min_id, max_id=args.max_id))

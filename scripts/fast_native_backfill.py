"""
Fast Native Backfill Script

This script uses Pyrogram (either Bot Token or User Session) to iterate through a source channel's
history and rapidly FORWARD media messages in chunks of 100 to the bot's INDEX_CHANNEL.

Since the messages are forwarded directly to the INDEX_CHANNEL, the bot's native "auto file index" 
(which triggers on channel posts) will automatically catch them and index them into the database 
using 100% native Bot API file_ids. 

This avoids any "can't unserialize it" errors when sending files, and is extremely fast 
because MTProto can forward up to 100 messages in a single API call!
"""
import asyncio
import logging
from pyrogram import Client
from pyrogram.errors import FloodWait

from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("fast_backfill")

async def main():
    if not settings.INDEX_CHANNELS:
        log.error("No INDEX_CHANNELS configured in .env! We need a dump channel to forward to.")
        return

    dump_channel = settings.INDEX_CHANNELS[0]
    
    # Connect Pyrogram
    if settings.INDEXER_BOT_MODE:
        log.info("Connecting as Bot...")
        app = Client(
            "fast_indexer",
            api_id=settings.TG_API_ID,
            api_hash=settings.TG_API_HASH,
            bot_token=settings.BOT_TOKEN,
            in_memory=True
        )
    else:
        log.info("Connecting as User Session...")
        if not settings.tg_sessions:
            log.error("No TG_SESSIONS found in .env!")
            return
        app = Client(
            "fast_indexer",
            api_id=settings.TG_API_ID,
            api_hash=settings.TG_API_HASH,
            session_string=settings.tg_sessions[0],
            in_memory=True
        )

    await app.start()
    
    source_channel = input("Enter the source channel username (e.g. @channel) or ID to backfill from: ").strip()
    if not source_channel:
        return
        
    try:
        # Resolve if it's a numeric ID string
        if source_channel.lstrip("-").isdigit():
            source_channel = int(source_channel)
        source_chat = await app.get_chat(source_channel)
    except Exception as e:
        log.error(f"Could not resolve source channel: {e}")
        await app.stop()
        return

    try:
        dump_chat = await app.get_chat(dump_channel)
    except Exception as e:
        log.error(f"Could not resolve dump channel: {e}")
        await app.stop()
        return

    log.info(f"Source: {source_chat.title} ({source_chat.id})")
    log.info(f"Dump Channel: {dump_chat.title} ({dump_chat.id})")
    
    input("Press Enter to start rapidly forwarding media to the dump channel (or Ctrl+C to abort)...")

    batch_size = 100
    msg_ids = []
    total_forwarded = 0

    log.info("Walking history (from newest to oldest)...")
    async for message in app.get_chat_history(source_chat.id):
        if message.document or message.video or message.audio:
            msg_ids.append(message.id)
            
            if len(msg_ids) >= batch_size:
                try:
                    await app.forward_messages(
                        chat_id=dump_chat.id,
                        from_chat_id=source_chat.id,
                        message_ids=msg_ids
                    )
                    total_forwarded += len(msg_ids)
                    log.info(f"Forwarded {total_forwarded} files...")
                    msg_ids.clear()
                    await asyncio.sleep(2)  # Avoid FloodWait limits
                except FloodWait as e:
                    log.warning(f"FloodWait! Sleeping for {e.value} seconds...")
                    await asyncio.sleep(e.value + 2)
                except Exception as e:
                    log.error(f"Failed to forward batch: {e}")
                    msg_ids.clear()
                    
    # flush remaining
    if msg_ids:
        try:
            await app.forward_messages(
                chat_id=dump_chat.id,
                from_chat_id=source_chat.id,
                message_ids=msg_ids
            )
            total_forwarded += len(msg_ids)
            log.info(f"Forwarded {total_forwarded} files...")
        except Exception as e:
            log.error(f"Failed to forward final batch: {e}")

    log.info(f"Done! Forwarded {total_forwarded} files to {dump_channel}.")
    log.info("The bot's 'auto file index' function should now be catching them natively and saving perfectly compatible Bot API file_ids!")
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())

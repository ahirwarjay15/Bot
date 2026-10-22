"""One-time cleanup of the retired bot database; never touches unrelated databases."""
import logging
import os
from datetime import datetime, timezone

from aiogram.exceptions import TelegramAPIError

log = logging.getLogger(__name__)

BOT_COLLECTIONS = (
    "deeplink_codes", "files", "moderators", "tasks", "broadcast_jobs",
    "batch_deliveries", "delivery_requests", "broadcast_drafts",
    "system_migrations", "settings", "broadcast_recipients", "service_locks",
    "batch_sessions", "users", "pending_join_requests", "batches",
    "force_join_links",
)


async def cleanup_legacy_database(bot, client, active_database_name: str) -> bool:
    """Revoke saved old bot-owned invites, then clear only known legacy app collections once."""
    legacy_name = os.getenv("LEGACY_MONGODB_DB_NAME", "").strip()
    if not legacy_name or legacy_name == active_database_name:
        return False
    legacy = client[legacy_name]
    marker = "titania_legacy_bot_database_reset_v1"
    if await legacy.system_migrations.find_one({"_id": marker}):
        return False

    # Revoke only links previously saved by this bot. Other admins' links are untouched.
    async for cfg in legacy.settings.find({"key": "force_join_channel", "button_url": {"$type": "string"}}):
        chat_id = cfg.get("channel")
        invite_link = cfg.get("button_url")
        if not chat_id or not invite_link:
            continue
        try:
            await bot.revoke_chat_invite_link(chat_id=int(chat_id), invite_link=invite_link)
        except (TelegramAPIError, ValueError):
            log.warning("Could not revoke a legacy Force Join link for chat %s", chat_id, exc_info=True)

    # Clear only the retired bot's known collections. Keep collection definitions/indexes.
    for collection_name in BOT_COLLECTIONS:
        if collection_name == "system_migrations":
            continue
        try:
            await legacy[collection_name].delete_many({})
        except Exception:
            log.exception("Could not clear legacy bot collection %s", collection_name)
            raise

    # Preserve a durable marker so two worker restarts never repeat the reset.
    await legacy.system_migrations.delete_many({})
    await legacy.system_migrations.insert_one({
        "_id": marker, "completed": True, "completed_at": datetime.now(timezone.utc),
        "active_database": active_database_name,
    })
    log.info("Legacy bot database %s was reset; unrelated databases were not touched", legacy_name)
    return True

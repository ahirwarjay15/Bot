"""MongoDB-backed single-owner lease for Telegram long polling."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

LEASE_ID = "telegram-get-updates"
LEASE_SECONDS = 90
RENEW_SECONDS = 20


async def _claim(db, owner: str) -> bool:
    now = datetime.now(timezone.utc)
    locks = db.service_locks
    current = await locks.find_one_and_update(
        {"_id": LEASE_ID, "lease_until": {"$lte": now}},
        {"$set": {"owner": owner, "lease_until": now + timedelta(seconds=LEASE_SECONDS)}},
        return_document=ReturnDocument.AFTER,
    )
    if current and current.get("owner") == owner:
        return True
    try:
        await locks.insert_one({
            "_id": LEASE_ID,
            "owner": owner,
            "lease_until": now + timedelta(seconds=LEASE_SECONDS),
        })
        return True
    except DuplicateKeyError:
        return False


async def _renew(db, owner: str) -> None:
    while True:
        await asyncio.sleep(RENEW_SECONDS)
        now = datetime.now(timezone.utc)
        result = await db.service_locks.update_one(
            {"_id": LEASE_ID, "owner": owner, "lease_until": {"$gt": now}},
            {"$set": {"lease_until": now + timedelta(seconds=LEASE_SECONDS)}},
        )
        if result.modified_count != 1:
            raise RuntimeError("Telegram polling lease lost; stopping this poller")


async def run_polling_with_lease(dp, bot, db) -> None:
    """Wait for sole polling ownership and relinquish it safely on shutdown."""
    owner = uuid4().hex
    while not await _claim(db, owner):
        await asyncio.sleep(10)

    polling_task = renewal_task = None
    try:
        # Never discard pending updates; only the lease owner touches polling state.
        await bot.delete_webhook(drop_pending_updates=False)
        polling_task = asyncio.create_task(
            dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()),
            name="telegram-polling",
        )
        renewal_task = asyncio.create_task(_renew(db, owner), name="telegram-polling-lease")
        done, _ = await asyncio.wait(
            {polling_task, renewal_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if renewal_task in done:
            error = renewal_task.exception()
            if error:
                raise error
            raise RuntimeError("Telegram polling lease renewal stopped unexpectedly")
        await polling_task
    finally:
        active = [task for task in (polling_task, renewal_task) if task is not None]
        for task in active:
            if not task.done():
                task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        # Expire rather than delete the lease: the fixed _id row avoids insert races.
        try:
            await db.service_locks.update_one(
                {"_id": LEASE_ID, "owner": owner},
                {"$set": {"lease_until": datetime.now(timezone.utc)}},
            )
        except Exception:
            # If MongoDB is unavailable, the lease will expire automatically.
            pass

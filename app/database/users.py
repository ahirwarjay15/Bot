"""Compact active/banned Telegram user storage with bounded BSON documents."""
import asyncio
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

MAX_USERS_PER_DOCUMENT = 100_000
BANNED_DOCUMENT_ID = "banned-users"
MIGRATION_ID = "users_chunked_storage_v1"

_BANNED_USER_IDS: set[int] = set()
_USER_LOCKS = tuple(asyncio.Lock() for _ in range(256))


def is_banned_cached(user_id: int) -> bool:
    return int(user_id) in _BANNED_USER_IDS


def _user_lock(user_id: int) -> asyncio.Lock:
    return _USER_LOCKS[int(user_id) % len(_USER_LOCKS)]


async def load_banned_user_cache(db) -> int:
    """Load the small banned-ID set once when each bot process starts."""
    record = await db.banned_users.find_one({"_id": BANNED_DOCUMENT_ID}, {"ids": 1})
    _BANNED_USER_IDS.clear()
    _BANNED_USER_IDS.update(int(value) for value in (record or {}).get("ids", []))
    return len(_BANNED_USER_IDS)


async def _add_active_user(db, user_id: int, *, session=None) -> bool:
    """Atomically append to a non-full bucket, or create the next bucket."""
    user_id = int(user_id)
    result = await db.users.update_one(
        {
            "ids": {"$ne": user_id},
            "$expr": {"$lt": [{"$size": "$ids"}, MAX_USERS_PER_DOCUMENT]},
        },
        {"$addToSet": {"ids": user_id}},
        session=session,
    )
    if result.modified_count:
        return True

    if await db.users.find_one({"ids": user_id}, {"_id": 1}, session=session):
        return False

    try:
        await db.users.insert_one({"_id": ObjectId(), "ids": [user_id]}, session=session)
        return True
    except DuplicateKeyError:
        # A concurrent registration may have inserted this ID in another bucket.
        if await db.users.find_one({"ids": user_id}, {"_id": 1}, session=session):
            return False
        raise


async def register_active_user(db, user_id: int) -> bool:
    """Save an active user with one MongoDB write; banned checks use the local cache."""
    user_id = int(user_id)
    async with _user_lock(user_id):
        if is_banned_cached(user_id):
            return False
        await _add_active_user(db, user_id)
        return True


async def set_user_banned(db, user_id: int, banned: bool) -> bool:
    """Move an ID between active and banned storage atomically."""
    user_id = int(user_id)
    async with _user_lock(user_id):
        async with await db.client.start_session() as session:
            async with session.start_transaction():
                if banned:
                    if is_banned_cached(user_id):
                        return False
                    await db.users.update_many(
                        {"ids": user_id},
                        {"$pull": {"ids": user_id}},
                        session=session,
                    )
                    await db.users.delete_many({"ids": []}, session=session)
                    await db.banned_users.update_one(
                        {"_id": BANNED_DOCUMENT_ID},
                        {"$addToSet": {"ids": user_id}},
                        upsert=True,
                        session=session,
                    )
                    await db.broadcast_recipients.update_many(
                        {"user_id": user_id, "status": "pending"},
                        {"$set": {
                            "status": "skipped_banned",
                            "finished_at": datetime.now(timezone.utc),
                        }},
                        session=session,
                    )
                else:
                    result = await db.banned_users.update_one(
                        {"_id": BANNED_DOCUMENT_ID, "ids": user_id},
                        {"$pull": {"ids": user_id}},
                        session=session,
                    )
                    if not result.matched_count:
                        return False
                    await _add_active_user(db, user_id, session=session)

        if banned:
            _BANNED_USER_IDS.add(user_id)
        else:
            _BANNED_USER_IDS.discard(user_id)
        return True


async def count_active_users(db) -> int:
    """Count IDs across buckets, not the number of bucket documents."""
    rows = await db.users.aggregate([
        {"$group": {"_id": None, "total": {"$sum": {"$size": "$ids"}}}},
    ]).to_list(length=1)
    return int(rows[0]["total"]) if rows else 0


async def count_user_totals(db) -> tuple[int, int]:
    """Return active and banned totals with one aggregate command."""
    rows = await db.banned_users.aggregate([
        {"$match": {"_id": BANNED_DOCUMENT_ID}},
        {"$lookup": {
            "from": "users",
            "pipeline": [
                {"$group": {"_id": None, "total": {"$sum": {"$size": "$ids"}}}},
            ],
            "as": "active",
        }},
        {"$project": {
            "_id": 0,
            "total": {"$ifNull": [{"$arrayElemAt": ["$active.total", 0]}, 0]},
            "banned": {"$size": {"$ifNull": ["$ids", []]}},
        }},
    ]).to_list(length=1)
    if not rows:
        return 0, 0
    return int(rows[0]["total"]), int(rows[0]["banned"])


async def migrate_user_storage(db) -> int:
    """Safely migrate legacy per-user records into 100k-ID buckets and one ban document."""
    while True:
        marker = await db.system_migrations.find_one({"_id": MIGRATION_ID})
        legacy_count = await db.users.count_documents({"user_id": {"$exists": True}})
        if marker and marker.get("completed") and not legacy_count:
            break

        now = datetime.now(timezone.utc)
        stale_before = now - timedelta(minutes=2)
        try:
            claim = await db.system_migrations.find_one_and_update(
                {
                    "_id": MIGRATION_ID,
                    "$or": [
                        {"status": {"$ne": "running"}},
                        {"started_at": {"$lte": stale_before}},
                    ],
                },
                {"$set": {"status": "running", "started_at": now, "completed": False}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            owns_lock = bool(claim and claim.get("started_at") == now)
        except DuplicateKeyError:
            owns_lock = False

        if not owns_lock:
            await asyncio.sleep(1)
            continue

        try:
            indexes = await db.users.index_information()
            if "user_id_1" in indexes:
                await db.users.drop_index("user_id_1")

            async with await db.client.start_session() as session:
                async with session.start_transaction():
                    legacy_docs = await db.users.find(
                        {"user_id": {"$exists": True}}, session=session
                    ).to_list(length=None)
                    existing_chunks = await db.users.find(
                        {"ids": {"$exists": True}}, {"ids": 1}, session=session
                    ).to_list(length=None)
                    existing_bans = await db.banned_users.find_one(
                        {"_id": BANNED_DOCUMENT_ID}, {"ids": 1}, session=session
                    )

                    active_ids = {
                        int(user_id)
                        for chunk in existing_chunks
                        for user_id in chunk.get("ids", [])
                    }
                    banned_ids = {
                        int(user_id)
                        for user_id in (existing_bans or {}).get("ids", [])
                    }
                    for record in legacy_docs:
                        user_id = int(record["user_id"])
                        if record.get("banned") is True:
                            banned_ids.add(user_id)
                            active_ids.discard(user_id)
                        elif user_id not in banned_ids:
                            active_ids.add(user_id)
                    active_ids.difference_update(banned_ids)

                    await db.users.delete_many({}, session=session)
                    active_list = sorted(active_ids)
                    for offset in range(0, len(active_list), MAX_USERS_PER_DOCUMENT):
                        await db.users.insert_one(
                            {
                                "_id": ObjectId(),
                                "ids": active_list[offset:offset + MAX_USERS_PER_DOCUMENT],
                            },
                            session=session,
                        )
                    await db.banned_users.update_one(
                        {"_id": BANNED_DOCUMENT_ID},
                        {"$set": {"ids": sorted(banned_ids)}},
                        upsert=True,
                        session=session,
                    )
                    await db.system_migrations.update_one(
                        {"_id": MIGRATION_ID},
                        {"$set": {
                            "status": "completed",
                            "completed": True,
                            "completed_at": datetime.now(timezone.utc),
                            "active_users": len(active_ids),
                            "banned_users": len(banned_ids),
                        }},
                        session=session,
                    )
            break
        except Exception:
            await db.system_migrations.update_one(
                {"_id": MIGRATION_ID},
                {"$set": {"status": "failed", "failed_at": datetime.now(timezone.utc)}},
            )
            raise

    await db.banned_users.create_index(
        [("ids", ASCENDING)], unique=True, sparse=True, name="banned_user_ids_unique"
    )
    await db.banned_users.update_one(
        {"_id": BANNED_DOCUMENT_ID},
        {"$setOnInsert": {"ids": []}},
        upsert=True,
    )
    await load_banned_user_cache(db)
    return legacy_count

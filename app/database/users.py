"""Compact comma-separated active/banned Telegram user storage."""
import asyncio
import re
import uuid
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

MAX_USERS_PER_DOCUMENT = 100_000
BANNED_DOCUMENT_ID = ObjectId("000000000000000000000001")
MIGRATION_ID = "users_comma_string_storage_v2"

_BANNED_USER_IDS: set[int] = set()
_USER_LOCKS = tuple(asyncio.Lock() for _ in range(256))
_NEW_BUCKET_LOCK = asyncio.Lock()


def _split_ids(value) -> list[str]:
    """Parse the canonical comma-separated ID field, ignoring empty tokens."""
    if isinstance(value, str):
        return [part for part in value.split(",") if part]
    if isinstance(value, (list, tuple, set)):
        return [str(part) for part in value if str(part)]
    return []


def _id_pattern(user_id: int) -> str:
    return rf"(?:^|,){re.escape(str(int(user_id)))}(?:,|$)"


def _join_without_id_expression(user_id: int) -> dict:
    """Aggregation expression that removes exactly one comma-delimited ID."""
    return {
        "$reduce": {
            "input": {
                "$filter": {
                    "input": {"$split": [{"$ifNull": ["$id", ""]}, ","]},
                    "as": "uid",
                    "cond": {"$and": [{"$ne": ["$$uid", ""]}, {"$ne": ["$$uid", str(int(user_id))]}]},
                }
            },
            "initialValue": "",
            "in": {
                "$cond": [
                    {"$eq": ["$$value", ""]},
                    "$$this",
                    {"$concat": ["$$value", ",", "$$this"]},
                ]
            },
        }
    }


def _count_ids_expression() -> dict:
    return {
        "$cond": [
            {"$eq": [{"$ifNull": ["$id", ""]}, ""]},
            0,
            {"$size": {"$split": ["$id", ","]}},
        ]
    }


def is_banned_cached(user_id: int) -> bool:
    return int(user_id) in _BANNED_USER_IDS


def _user_lock(user_id: int) -> asyncio.Lock:
    return _USER_LOCKS[int(user_id) % len(_USER_LOCKS)]


async def load_banned_user_cache(db) -> int:
    """Load the single comma-separated banned-ID string into process memory."""
    record = await db.banned_users.find_one({"_id": BANNED_DOCUMENT_ID}, {"id": 1})
    _BANNED_USER_IDS.clear()
    _BANNED_USER_IDS.update(int(value) for value in _split_ids((record or {}).get("id")))
    return len(_BANNED_USER_IDS)


async def _add_active_user(db, user_id: int, *, session=None) -> bool:
    """Append one ID to a non-full string bucket, or create the next bucket."""
    user_id = int(user_id)
    token = str(user_id)
    if await db.users.find_one({"id": {"$regex": _id_pattern(user_id)}}, {"_id": 1}, session=session):
        return False

    capacity_filter = {
        "$expr": {"$lt": [_count_ids_expression(), MAX_USERS_PER_DOCUMENT]}
    }
    async with _NEW_BUCKET_LOCK:
        # The database rechecks capacity when applying the atomic update.
        result = await db.users.update_one(
            capacity_filter,
            [{"$set": {
                "id": {
                    "$cond": [
                        {"$eq": [{"$ifNull": ["$id", ""]}, ""]},
                        token,
                        {"$concat": ["$id", ",", token]},
                    ]
                }
            }}],
            session=session,
        )
        if result.matched_count:
            return True
        await db.users.insert_one({"_id": ObjectId(), "id": token}, session=session)
        return True


async def register_active_user(db, user_id: int) -> bool:
    """Save an active user; banned checks use the process-local cache."""
    user_id = int(user_id)
    async with _user_lock(user_id):
        if is_banned_cached(user_id):
            return False
        await _add_active_user(db, user_id)
        return True


async def set_user_banned(db, user_id: int, banned: bool) -> bool:
    """Move an ID between active and banned comma-separated strings atomically."""
    user_id = int(user_id)
    token = str(user_id)
    pattern = _id_pattern(user_id)
    async with _user_lock(user_id):
        async with db.client.start_session() as session:
            async with await session.start_transaction():
                if banned:
                    if is_banned_cached(user_id):
                        return False
                    await db.users.update_many(
                        {"id": {"$regex": pattern}},
                        [{"$set": {"id": _join_without_id_expression(user_id)}}],
                        session=session,
                    )
                    await db.users.delete_many({"id": ""}, session=session)
                    await db.banned_users.update_one(
                        {"_id": BANNED_DOCUMENT_ID},
                        [{"$set": {
                            "id": {
                                "$cond": [
                                    {"$or": [
                                        {"$eq": [{"$ifNull": ["$id", ""]}, ""]},
                                        {"$not": [{"$in": [token, {"$split": ["$id", ","]}]}]},
                                    ]},
                                    {"$cond": [
                                        {"$eq": [{"$ifNull": ["$id", ""]}, ""]},
                                        token,
                                        {"$concat": ["$id", ",", token]},
                                    ]},
                                    "$id",
                                ]
                            }
                        }}],
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
                        {"_id": BANNED_DOCUMENT_ID, "id": {"$regex": pattern}},
                        [{"$set": {"id": _join_without_id_expression(user_id)}}],
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
    """Count comma-delimited IDs across active buckets."""
    rows = await db.users.aggregate([
        {"$group": {"_id": None, "total": {"$sum": _count_ids_expression()}}},
    ]).to_list(length=1)
    return int(rows[0]["total"]) if rows else 0


async def count_user_totals(db) -> tuple[int, int]:
    """Return total registered and banned counts with one aggregate command."""
    rows = await db.banned_users.aggregate([
        {"$match": {"_id": BANNED_DOCUMENT_ID}},
        {"$lookup": {
            "from": "users",
            "pipeline": [
                {"$group": {"_id": None, "total": {"$sum": _count_ids_expression()}}},
            ],
            "as": "active",
        }},
        {"$project": {
            "_id": 0,
            "total": {"$add": [
                {"$ifNull": [{"$arrayElemAt": ["$active.total", 0]}, 0]},
                {"$cond": [
                    {"$eq": [{"$ifNull": ["$id", ""]}, ""]},
                    0,
                    {"$size": {"$split": ["$id", ","]}},
                ]},
            ]},
            "banned": {"$cond": [
                {"$eq": [{"$ifNull": ["$id", ""]}, ""]},
                0,
                {"$size": {"$split": ["$id", ","]}},
            ]},
        }},
    ]).to_list(length=1)
    if not rows:
        return 0, 0
    return int(rows[0]["total"]), int(rows[0]["banned"])


async def migrate_user_storage(db) -> int:
    """Convert every legacy/array representation to comma strings, preserving IDs."""
    while True:
        marker = await db.system_migrations.find_one({"_id": MIGRATION_ID})
        old_users = await db.users.count_documents({
            "$or": [
                {"ids": {"$exists": True}},
                {"user_id": {"$exists": True}},
            ]
        })
        old_bans = await db.banned_users.count_documents({"ids": {"$exists": True}})
        if marker and marker.get("completed") and not old_users and not old_bans:
            break

        now = datetime.now(timezone.utc)
        lock_token = uuid.uuid4().hex
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
                {"$set": {
                    "status": "running",
                    "started_at": now,
                    "lock_token": lock_token,
                    "completed": False,
                }},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            owns_lock = bool(claim and claim.get("lock_token") == lock_token)
        except DuplicateKeyError:
            owns_lock = False

        if not owns_lock:
            await asyncio.sleep(1)
            continue

        try:
            for collection, obsolete_indexes in (
                (db.users, ("active_user_ids_unique", "ids_1")),
                (db.banned_users, ("banned_user_ids_unique", "ids_1")),
            ):
                index_info = await collection.index_information()
                for index_name in obsolete_indexes:
                    if index_name in index_info:
                        await collection.drop_index(index_name)

            async with db.client.start_session() as session:
                async with await session.start_transaction():
                    user_docs = await db.users.find({}, session=session).to_list(length=None)
                    ban_docs = await db.banned_users.find({}, session=session).to_list(length=None)
                    active_ids: set[str] = set()
                    banned_ids: set[str] = set()

                    for record in user_docs:
                        if "user_id" in record:
                            values = [str(record["user_id"])]
                            if record.get("banned") is True:
                                banned_ids.update(values)
                            else:
                                active_ids.update(values)
                        else:
                            active_ids.update(_split_ids(record.get("id")))
                            active_ids.update(_split_ids(record.get("ids")))

                    for record in ban_docs:
                        banned_ids.update(_split_ids(record.get("id")))
                        banned_ids.update(_split_ids(record.get("ids")))

                    active_ids.difference_update(banned_ids)
                    await db.users.delete_many({}, session=session)
                    ordered_active = sorted(active_ids, key=int)
                    for offset in range(0, len(ordered_active), MAX_USERS_PER_DOCUMENT):
                        await db.users.insert_one({
                            "_id": ObjectId(),
                            "id": ",".join(ordered_active[offset:offset + MAX_USERS_PER_DOCUMENT]),
                        }, session=session)

                    await db.banned_users.delete_many({}, session=session)
                    await db.banned_users.insert_one({
                        "_id": BANNED_DOCUMENT_ID,
                        "id": ",".join(sorted(banned_ids, key=int)),
                    }, session=session)

                    # All historical one-time migration markers are retired.
                    await db.system_migrations.delete_many(
                        {"_id": {"$ne": MIGRATION_ID}, "completed": True},
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
                        }, "$unset": {"lock_token": ""}},
                        session=session,
                    )
            break
        except Exception:
            await db.system_migrations.update_one(
                {"_id": MIGRATION_ID},
                {"$set": {"status": "failed", "failed_at": datetime.now(timezone.utc)},
                 "$unset": {"lock_token": ""}},
            )
            raise

    await db.banned_users.update_one(
        {"_id": BANNED_DOCUMENT_ID},
        {"$setOnInsert": {"id": ""}},
        upsert=True,
    )
    await load_banned_user_cache(db)
    return old_users + old_bans

import logging
from datetime import datetime, timedelta, timezone
from pymongo import ASCENDING, AsyncMongoClient
from pymongo.errors import DuplicateKeyError
from app.config import Settings

logger = logging.getLogger(__name__)


async def unify_file_batch_records(db) -> int:
    """Move legacy batch records into files and remove the obsolete kind discriminator."""
    moved = 0
    async for batch in db.batches.find({}):
        code = batch.get("batch_id")
        items = batch.get("items")
        if not code or not isinstance(items, list):
            continue
        existing = await db.files.find_one({"key": code}, {"_id": 1, "items": 1})
        if existing is None:
            existing = await db.files.find_one({"file_key": code}, {"_id": 1, "items": 1})
        if existing is None:
            await db.files.insert_one({"key": code, "items": items})
            moved += 1
            await db.batches.delete_one({"_id": batch["_id"]})
        elif isinstance(existing.get("items"), list):
            await db.batches.delete_one({"_id": batch["_id"]})
    await db.files.update_many({}, {"$unset": {"kind": "", "file_name": "", "created_at": "", "created_by": ""}})
    return moved


async def migrate_delivery_storage(db) -> bool:
    """Run destructive legacy-collection cleanup once across multiple worker services."""
    migration_id = "minimal_delivery_storage_v1"
    now = datetime.now(timezone.utc)
    existing = await db.system_migrations.find_one({"_id": migration_id})
    if existing and existing.get("completed"):
        return False
    if existing:
        started_at = existing.get("started_at")
        if started_at and started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if existing.get("status") == "running" and started_at and now - started_at < timedelta(minutes=10):
            return False
        claimed = await db.system_migrations.update_one(
            {"_id": migration_id, "status": existing.get("status"), "started_at": existing.get("started_at")},
            {"$set": {"status": "running", "started_at": now, "completed": False}},
        )
        if not claimed.modified_count:
            return False
    else:
        try:
            await db.system_migrations.insert_one({
                "_id": migration_id, "status": "running", "started_at": now, "completed": False,
            })
        except DuplicateKeyError:
            return False

    try:
        moved = await unify_file_batch_records(db)
        logger.info("Unified file/batch records; legacy batches moved=%s", moved)
        await db.batches.drop()
        await db.batch_sessions.drop()
        await db.batch_deliveries.drop()
        await db.deeplink_codes.drop()
        await db.tasks.update_many(
            {},
            {"$unset": {
                "kind": "", "created_at": "", "notice": "", "notice_entities": "",
                "button1_text": "", "button1_url": "", "button1_enabled": "",
                "button2_text": "", "button2_url": "", "button2_enabled": "",
            }},
        )
        await db.system_migrations.update_one(
            {"_id": migration_id},
            {"$set": {"status": "completed", "completed": True, "completed_at": datetime.now(timezone.utc)}},
        )
        return True
    except Exception:
        await db.system_migrations.update_one(
            {"_id": migration_id},
            {"$set": {"status": "failed", "failed_at": datetime.now(timezone.utc)}},
        )
        raise


async def cleanup_completed_transients(db) -> dict:
    """Remove completed temporary records left by older versions; keep active work intact."""
    result = {}
    result["done_tasks"] = (await db.tasks.delete_many({"status": "done"})).deleted_count
    result["done_batch_deliveries"] = 0  # The obsolete collection is dropped during schema initialization.
    result["finished_delivery_requests"] = (
        await db.delivery_requests.delete_many({"status": {"$in": ["done", "failed"]}})
    ).deleted_count
    completed_jobs = [
        job["_id"] async for job in db.broadcast_jobs.find({"status": "completed"}, {"_id": 1})
    ]
    if completed_jobs:
        result["broadcast_recipients"] = (
            await db.broadcast_recipients.delete_many({"job_id": {"$in": completed_jobs}})
        ).deleted_count
        result["broadcast_jobs"] = (
            await db.broadcast_jobs.delete_many({"_id": {"$in": completed_jobs}})
        ).deleted_count
    else:
        result["broadcast_recipients"] = 0
        result["broadcast_jobs"] = 0
    return result


async def remove_legacy_external_force_join_links(db) -> bool:
    """Remove the retired external Force Join setting from legacy deployments."""
    migration_id = "remove_external_force_join_links_v1"
    if await db.system_migrations.find_one({"_id": migration_id}):
        return False
    await db.settings.update_one(
        {"key": "force_join"},
        {"$unset": {"other_links": ""}},
    )
    await db.system_migrations.update_one(
        {"_id": migration_id},
        {"$setOnInsert": {"completed": True}},
        upsert=True,
    )
    return True


class Database:
    def __init__(
        self,
        settings: Settings,
        *,
        initialize_schema: bool = True,
        recover_interrupted_jobs: bool = True,
    ):
        self.settings = settings
        self.initialize_schema = initialize_schema
        self.recover_interrupted_jobs = recover_interrupted_jobs
        self.client = None
        self.db = None

    async def connect(self):
        self.client = AsyncMongoClient(
            self.settings.mongodb_uri.get_secret_value(),
            appname="File-Bot",
            serverSelectionTimeoutMS=8000,
            connectTimeoutMS=8000,
            retryWrites=True,
        )
        try:
            await self.client.admin.command("ping")
            self.db = self.client[self.settings.mongodb_db_name]
            # Polling-only replicas use the existing schema. Only the worker role
            # performs index changes and one-time migrations, avoiding startup races.
            if not self.initialize_schema:
                logger.info("MongoDB ready; schema initialization is owned by the worker service")
                return

            await self.db.users.create_index([("user_id", ASCENDING)], unique=True)
            file_indexes = await self.db.files.index_information()
            if "file_key_1" in file_indexes:
                await self.db.files.drop_index("file_key_1")
            await self.db.files.create_index([("key", ASCENDING)], unique=True, sparse=True, name="key_unique")
            settings_indexes = await self.db.settings.index_information()
            await self.db.settings.create_index(
                [("key", ASCENDING), ("channel", ASCENDING)],
                unique=True,
                name="key_channel_unique",
            )
            for index_name, index_info in settings_indexes.items():
                if (
                    index_name != "_id_"
                    and index_info.get("key") == [("key", ASCENDING)]
                    and index_info.get("unique")
                ):
                    await self.db.settings.drop_index(index_name)
            await self.db.moderators.create_index([("user_id", ASCENDING)], unique=True)
            await self.db.tasks.create_index([("status", ASCENDING), ("deadline", ASCENDING)])
            await self.db.tasks.create_index([("request_key", ASCENDING)], unique=True, sparse=True)
            await self.db.delivery_requests.create_index([("request_key", ASCENDING)], unique=True)
            await self.db.delivery_requests.create_index([("status", ASCENDING), ("updated_at", ASCENDING)])
            await migrate_delivery_storage(self.db)
            # Stable Force Sub URLs are looked up by channel; no expiry polling/index is needed.
            force_join_indexes = await self.db.force_join_links.index_information()
            for obsolete_index in (
                "user_chat_unique",
                "user_chat_lookup",
                "temporary_link_ttl",
            ):
                if obsolete_index in force_join_indexes:
                    await self.db.force_join_links.drop_index(obsolete_index)
            await self.db.force_join_links.create_index(
                [("channel", ASCENDING)], name="channel_lookup"
            )
            await self.db.force_join_links.create_index(
                [("invite_link", ASCENDING)], unique=True, name="invite_link_unique"
            )
            await remove_legacy_external_force_join_links(self.db)
            await self.db.broadcast_drafts.create_index([("admin_id", ASCENDING)], unique=True)
            await self.db.broadcast_jobs.create_index([("draft_id", ASCENDING)], unique=True)
            await self.db.broadcast_jobs.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
            await self.db.broadcast_recipients.create_index([("job_id", ASCENDING), ("user_id", ASCENDING)], unique=True)
            await self.db.broadcast_recipients.create_index([("job_id", ASCENDING), ("status", ASCENDING)])
            await self.db.broadcast_recipients.create_index(
                [("job_id", ASCENDING), ("status", ASCENDING), ("retry_at", ASCENDING)],
                name="job_status_retry_at",
            )
            # Remove the retired delivery-status flag and profile metadata. Admin
            # bans remain represented by users.banned; Telegram delivery failures
            # are tracked per broadcast recipient, not as a second user status.
            user_indexes = await self.db.users.index_information()
            if "blocked_1" in user_indexes:
                await self.db.users.drop_index("blocked_1")
            # This full-collection cleanup is a one-time migration, not a startup task.
            user_compaction_marker = "compact_user_profiles_v1"
            if not await self.db.system_migrations.find_one({"_id": user_compaction_marker, "completed": True}):
                user_cleanup = await self.db.users.update_many(
                    {},
                    {"$unset": {
                        "first_name": "", "last_name": "", "username": "",
                        "created_at": "", "last_seen": "",
                        "last_delivery_error": "", "last_delivery_error_at": "",
                    }},
                )
                await self.db.system_migrations.update_one(
                    {"_id": user_compaction_marker},
                    {"$setOnInsert": {"completed": True, "completed_at": datetime.now(timezone.utc)}},
                    upsert=True,
                )
                logger.info("One-time user record compaction finished; records updated=%s", user_cleanup.modified_count)
            if self.recover_interrupted_jobs:
                # Never reset jobs just because a second worker started. Only reclaim
                # work whose claim/heartbeat has been stale long enough to indicate a crash.
                stale_before = datetime.now(timezone.utc) - timedelta(minutes=10)
                stale_claim = {"$or": [
                    {"claimed_at": {"$lte": stale_before}},
                    {"worker_claimed_at": {"$lte": stale_before}},
                    {"updated_at": {"$lte": stale_before}},
                    {"$and": [
                        {"claimed_at": {"$exists": False}},
                        {"worker_claimed_at": {"$exists": False}},
                        {"updated_at": {"$exists": False}},
                        {"created_at": {"$lte": stale_before}},
                    ]},
                ]}
                task_result = await self.db.tasks.update_many(
                    {"status": "processing", **stale_claim},
                    {"$set": {"status": "pending", "recovered_at": datetime.now(timezone.utc)}},
                )
                logger.info(
                    "Stale job recovery complete: %s expiry tasks",
                    task_result.modified_count,
                )
            else:
                logger.info("Worker-owned job recovery skipped for this process role")
        except Exception:
            await self.close()
            logger.exception("MongoDB startup check failed")
            raise

    async def close(self):
        if self.client is not None:
            await self.client.close()
            self.client = None
            self.db = None

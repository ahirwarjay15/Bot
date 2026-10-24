import logging
from datetime import datetime, timedelta, timezone
from pymongo import ASCENDING, AsyncMongoClient
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
        existing = await db.files.find_one({"file_key": code}, {"_id": 1, "items": 1})
        if existing is None:
            await db.files.insert_one({"file_key": code, "items": items})
            moved += 1
            await db.batches.delete_one({"_id": batch["_id"]})
        elif isinstance(existing.get("items"), list):
            await db.batches.delete_one({"_id": batch["_id"]})
    await db.files.update_many({}, {"$unset": {"kind": "", "file_name": "", "created_at": "", "created_by": ""}})
    await db.deeplink_codes.update_many({}, {"$unset": {"kind": ""}})
    return moved


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
            await self.db.files.create_index([("file_key", ASCENDING)], unique=True)
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
            moved_batches = await unify_file_batch_records(self.db)
            logger.info("Unified file/batch records; legacy batches moved=%s", moved_batches)
            await self.db.batches.drop()
            await self.db.batch_sessions.drop()
            await self.db.batch_deliveries.drop()
            await self.db.deeplink_codes.drop()
            await self.db.tasks.update_many(
                {"chat_id": {"$exists": False}, "user_id": {"$exists": True}},
                {"$rename": {"user_id": "chat_id"}, "$unset": {
                    "kind": "", "created_at": "", "notice": "", "notice_entities": "",
                }},
            )
            await self.db.tasks.update_many(
                {},
                {"$unset": {
                    "notice": "", "notice_entities": "", "button1_text": "",
                    "button1_url": "", "button1_enabled": "", "button2_text": "",
                    "button2_url": "", "button2_enabled": "",
                }},
            )
            logger.info("Unified file/batch records; legacy batches moved=%s", moved_batches)
            # Keep multiple short-lived links per user/chat so issuing a fresh
            # link never has to revoke an older one in the interactive request path.
            force_join_indexes = await self.db.force_join_links.index_information()
            if "user_chat_unique" in force_join_indexes:
                await self.db.force_join_links.drop_index("user_chat_unique")
            if "user_chat_lookup" not in force_join_indexes:
                await self.db.force_join_links.create_index(
                    [("user_id", ASCENDING), ("chat_id", ASCENDING)], name="user_chat_lookup"
                )
            force_join_indexes = await self.db.force_join_links.index_information()
            if "temporary_link_ttl" in force_join_indexes:
                await self.db.force_join_links.drop_index("temporary_link_ttl")
            await self.db.force_join_links.create_index([("cleanup_at", ASCENDING)], expireAfterSeconds=0, name="temporary_link_cleanup_ttl")
            await self.db.force_join_links.create_index([("invite_link", ASCENDING)], unique=True, name="invite_link_unique")
            await remove_legacy_external_force_join_links(self.db)
            cleanup_counts = await cleanup_completed_transients(self.db)
            logger.info("Removed completed legacy records: %s", cleanup_counts)
            await self.db.broadcast_drafts.create_index([("admin_id", ASCENDING)], unique=True)
            await self.db.broadcast_jobs.create_index([("draft_id", ASCENDING)], unique=True)
            await self.db.broadcast_jobs.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
            await self.db.broadcast_recipients.create_index([("job_id", ASCENDING), ("user_id", ASCENDING)], unique=True)
            await self.db.broadcast_recipients.create_index([("job_id", ASCENDING), ("status", ASCENDING)])
            await self.db.users.create_index([("blocked", ASCENDING)])
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

import logging
from datetime import datetime, timedelta, timezone
from pymongo import ASCENDING, AsyncMongoClient
from app.config import Settings
from app.database.users import migrate_user_storage

logger = logging.getLogger(__name__)


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
            # Both services wait for the same idempotent user-storage migration.
            # The polling process also loads the small banned-ID cache at startup.
            await migrate_user_storage(self.db)
            if not self.initialize_schema:
                logger.info("MongoDB ready; remaining schema initialization is owned by the worker service")
                return

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
            await self.db.broadcast_drafts.create_index([("admin_id", ASCENDING)], unique=True)
            await self.db.broadcast_jobs.create_index([("draft_id", ASCENDING)], unique=True)
            await self.db.broadcast_jobs.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
            await self.db.broadcast_recipients.create_index([("job_id", ASCENDING), ("user_id", ASCENDING)], unique=True)
            await self.db.broadcast_recipients.create_index([("job_id", ASCENDING), ("status", ASCENDING)])
            await self.db.broadcast_recipients.create_index(
                [("job_id", ASCENDING), ("status", ASCENDING), ("retry_at", ASCENDING)],
                name="job_status_retry_at",
            )
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

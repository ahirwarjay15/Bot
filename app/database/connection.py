import logging
from datetime import datetime, timedelta, timezone
from pymongo import ASCENDING, AsyncMongoClient, UpdateOne
from app.config import Settings

logger = logging.getLogger(__name__)


async def _backfill_code_collection(source, target, field: str, kind: str, batch_size: int = 500):
    operations = []
    async for record in source.find({field: {"$type": "string"}}, {field: 1}):
        code = record[field]
        operations.append(UpdateOne(
            {"code": code},
            {"$setOnInsert": {"code": code, "kind": kind}},
            upsert=True,
        ))
        if len(operations) >= batch_size:
            await target.bulk_write(operations, ordered=False)
            operations = []
    if operations:
        await target.bulk_write(operations, ordered=False)


async def backfill_deeplink_codes(db, batch_size: int = 500) -> bool:
    """Run the legacy-code backfill once, in bulk, instead of N round trips on every boot."""
    migration_id = "deeplink_code_backfill_v1"
    if await db.system_migrations.find_one({"_id": migration_id}):
        return False
    await _backfill_code_collection(db.files, db.deeplink_codes, "file_key", "legacy-file", batch_size)
    await _backfill_code_collection(db.batches, db.deeplink_codes, "batch_id", "legacy-batch", batch_size)
    await db.system_migrations.update_one(
        {"_id": migration_id},
        {"$setOnInsert": {"completed": True}},
        upsert=True,
    )
    return True


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
            await self.db.batches.create_index([("batch_id", ASCENDING)], unique=True, sparse=True)
            await self.db.deeplink_codes.create_index([("code", ASCENDING)], unique=True)
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
            await backfill_deeplink_codes(self.db)
            await self.db.broadcast_drafts.create_index([("admin_id", ASCENDING)], unique=True)
            await self.db.broadcast_jobs.create_index([("draft_id", ASCENDING)], unique=True)
            await self.db.broadcast_jobs.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
            await self.db.broadcast_recipients.create_index([("job_id", ASCENDING), ("user_id", ASCENDING)], unique=True)
            await self.db.broadcast_recipients.create_index([("job_id", ASCENDING), ("status", ASCENDING)])
            await self.db.batch_sessions.create_index([("admin_id", ASCENDING)], unique=True)
            await self.db.batch_deliveries.create_index([("request_key", ASCENDING)], unique=True)
            await self.db.batch_deliveries.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
            await self.db.batch_sessions.create_index([("status", ASCENDING), ("last_activity_ts", ASCENDING)])
            await self.db.users.create_index([("blocked", ASCENDING)])
            if self.recover_interrupted_jobs:
                # Never reset jobs just because a second worker started. Only reclaim
                # work whose claim/heartbeat has been stale long enough to indicate a crash.
                stale_before = datetime.now(timezone.utc) - timedelta(minutes=10)
                stale_claim = {"$or": [
                    {"claimed_at": {"$lte": stale_before}},
                    {"worker_claimed_at": {"$lte": stale_before}},
                    {"updated_at": {"$lte": stale_before}},
                    {"created_at": {"$lte": stale_before}},
                ]}
                task_result = await self.db.tasks.update_many(
                    {"status": "processing", **stale_claim},
                    {"$set": {"status": "pending", "recovered_at": datetime.now(timezone.utc)}},
                )
                batch_result = await self.db.batch_deliveries.update_many(
                    {"status": "processing", **stale_claim},
                    {"$set": {"status": "pending", "recovered_at": datetime.now(timezone.utc)}},
                )
                logger.info(
                    "Stale job recovery complete: %s expiry tasks, %s batch deliveries",
                    task_result.modified_count, batch_result.modified_count,
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

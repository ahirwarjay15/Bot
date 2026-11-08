"""Application entry point and service lifecycle."""
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import get_settings
from app.database.connection import Database, cleanup_completed_transients
from app.database.legacy_cleanup import cleanup_legacy_database
from app.handlers.start import router as start_router, cleanup_expired_pending_join_requests, migrate_force_join_links
from app.handlers.file import router as file_router
from app.handlers.batch import router as batch_router
from app.handlers.admin import router as admin_router
from app.handlers.settings import router as settings_router
from app.handlers.broadcast import router as broadcast_router, run_broadcast_worker
from app.handlers.shortener import router as shortener_router
from app.handlers.misc import router as misc_router
from app.services.delivery import run_task_worker
from app.services.polling_lease import run_polling_with_lease

SERVICE_READY = False
log = logging.getLogger(__name__)
VALID_SERVICE_ROLES = {"all", "polling", "worker"}


async def run_daily_database_cleanup_worker(db):
    """Run all retention cleanup once daily at 04:00 Asia/Kolkata."""
    tz = ZoneInfo("Asia/Kolkata")
    while True:
        now_local = datetime.now(tz)
        next_run = now_local.replace(hour=4, minute=0, second=0, microsecond=0)
        if next_run <= now_local:
            next_run += timedelta(days=1)
        await asyncio.sleep(max(1, (next_run - now_local).total_seconds()))
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        try:
            transient_counts = await cleanup_completed_transients(db)
            deleted_drafts = await db.broadcast_drafts.delete_many(
                {"status": "draft", "created_at": {"$lte": cutoff}}
            )
            expired_requests = await cleanup_expired_pending_join_requests(db)
            log.info(
                "04:00 daily cleanup finished: transients=%s, old_drafts=%s, expired_join_requests=%s",
                transient_counts, deleted_drafts.deleted_count, expired_requests,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Daily database retention cleanup failed; will retry next day at 04:00")


def get_service_role() -> str:
    """Select a process role; the default preserves the original single-service behavior."""
    role = os.getenv("SERVICE_ROLE", "all").strip().lower()
    if role not in VALID_SERVICE_ROLES:
        raise ValueError(
            f"Invalid SERVICE_ROLE={role!r}; expected one of: "
            + ", ".join(sorted(VALID_SERVICE_ROLES))
        )
    return role


async def health(_request):
    """Report readiness, not merely that the HTTP listener is alive."""
    if not SERVICE_READY:
        return web.json_response({"status": "starting", "service": "Telegram File Bot"}, status=503)
    return web.json_response({"status": "ok", "service": "Telegram File Bot"})


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000")))
    await site.start()
    log.info("Health endpoint started on port %s", os.getenv("PORT", "10000"))
    return runner


async def connect_database_with_retry(database, *, initial_delay: float = 5.0, max_delay: float = 60.0):
    """Retry transient database outages with bounded exponential backoff."""
    delay = initial_delay
    while True:
        try:
            await database.connect()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("MongoDB unavailable; retrying in %.1f seconds", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)


async def supervise_worker(worker, *args, name: str, restart_delay: float = 5.0):
    """Restart a background worker after an unexpected exception."""
    while True:
        try:
            await worker(*args)
            log.error("Background worker %s exited unexpectedly; restarting", name)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Background worker %s crashed; restarting in %.1f seconds", name, restart_delay)
        await asyncio.sleep(restart_delay)


async def main():
    global SERVICE_READY
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    health_runner = await start_health_server()
    bot = None
    database = None
    tasks = []
    try:
        role = get_service_role()
        settings = get_settings()
        logging.getLogger().setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
        log.info("Starting bot with SERVICE_ROLE=%s", role)
        bot = Bot(
            token=settings.bot_token.get_secret_value(),
            default=DefaultBotProperties(link_preview_is_disabled=True),
        )
        # A polling-only process must not reset jobs being processed by the worker service.
        database = Database(settings, initialize_schema=(role != "polling"), recover_interrupted_jobs=(role != "polling"))
        await connect_database_with_retry(database)
        if role in {"all", "worker"}:
            await migrate_force_join_links(bot, database.db)
        if role == "worker":
            await cleanup_legacy_database(bot, database.client, settings.mongodb_db_name)

        if role in {"all", "polling"}:
            dp = Dispatcher(storage=MemoryStorage())
            dp["settings"] = settings
            dp["database"] = database
            # Keep the catch-all invalid-post handler last so other handlers run first.
            for r in (start_router, file_router, batch_router, admin_router, settings_router, broadcast_router, shortener_router, misc_router):dp.include_router(r)
        else:
            dp = None

        if role in {"all", "worker"}:
            workers = (
                (run_task_worker, (bot, database.db), "delivery-task-worker"),
                (run_broadcast_worker, (bot, database.db), "broadcast-worker"),
                (run_daily_database_cleanup_worker, (database.db,), "daily-database-cleanup-worker"),
            )
            for worker, args, name in workers:
                tasks.append(asyncio.create_task(
                    supervise_worker(worker, *args, name=name),
                    name=name,
                ))

        SERVICE_READY = True
        log.info("Bot is ready; service role=%s", role)
        if dp is not None:
            await run_polling_with_lease(dp, bot, database.db)
        else:
            await asyncio.gather(*tasks)
    finally:
        SERVICE_READY = False
        for task in tasks:
            task.cancel()
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                    log.error("Background worker exited with an error during shutdown: %r", result)
        if database is not None:
            await database.close()
        if bot is not None:
            await bot.session.close()
        await health_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

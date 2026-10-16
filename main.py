"""Application entry point."""
import asyncio,logging,sys
from aiogram import Bot,Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from app.config import get_settings
from app.database.connection import Database
from app.handlers.start import router as start_router, run_pending_join_request_cleanup_worker
from app.handlers.file import router as file_router
from app.handlers.batch import router as batch_router, run_batch_session_worker
from app.handlers.admin import router as admin_router
from app.handlers.settings import router as settings_router
from app.handlers.broadcast import router as broadcast_router, run_broadcast_worker
from app.handlers.shortener import router as shortener_router
from app.handlers.misc import router as misc_router
from app.services.delivery import run_task_worker, run_batch_delivery_worker

async def supervise_worker(worker, *args, name: str, restart_delay: float = 5.0):
    """Restart a background worker after an unexpected exception."""
    log = logging.getLogger(__name__)
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
    settings=get_settings()
    logging.basicConfig(level=getattr(logging,settings.log_level.upper(),logging.INFO),format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",stream=sys.stdout)
    # Do not set a global parse mode: custom messages may contain arbitrary user text.
    bot=Bot(token=settings.bot_token.get_secret_value(),default=DefaultBotProperties(link_preview_is_disabled=True))
    database=Database(settings);worker=None;batch_worker=None;broadcast_worker=None;delivery_worker=None;join_request_cleanup_worker=None
    try:
        await database.connect()
        dp=Dispatcher(storage=MemoryStorage());dp["settings"]=settings;dp["database"]=database
        # Keep the catch-all invalid-post handler last so file, batch, and settings handlers can process their own messages first.
        for r in (start_router,file_router,batch_router,admin_router,settings_router,broadcast_router,shortener_router,misc_router):dp.include_router(r)
        await bot.delete_webhook(drop_pending_updates=False)
        worker=asyncio.create_task(supervise_worker(run_task_worker, bot, database.db, name="delivery-task-worker"),name="delivery-task-worker")
        batch_worker=asyncio.create_task(supervise_worker(run_batch_session_worker, bot, database.db, name="batch-session-worker"),name="batch-session-worker")
        delivery_worker=asyncio.create_task(supervise_worker(run_batch_delivery_worker, bot, database.db, name="batch-delivery-worker"),name="batch-delivery-worker")
        broadcast_worker=asyncio.create_task(supervise_worker(run_broadcast_worker, bot, database.db, name="broadcast-worker"),name="broadcast-worker")
        join_request_cleanup_worker=asyncio.create_task(supervise_worker(run_pending_join_request_cleanup_worker, database.db, name="pending-join-request-cleanup-worker"),name="pending-join-request-cleanup-worker")
        logging.getLogger(__name__).info("Polling started")
        await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in (worker, batch_worker, broadcast_worker, delivery_worker, join_request_cleanup_worker):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logging.getLogger(__name__).exception("Background worker exited with an error during shutdown")
        await database.close();await bot.session.close()

if __name__=="__main__":
    try:asyncio.run(main())
    except (KeyboardInterrupt,SystemExit):pass

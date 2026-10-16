"""MongoDB-backed batch collection with one permanent deeplink per batch."""
import logging
import time
from secrets import token_urlsafe
from datetime import datetime, timezone
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, CopyTextButton
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.dispatcher.event.bases import SkipHandler
from app.config import Settings
from app.database.files import FileRepository
from app.services.permissions import is_owner_or_moderator

router = Router(name="batch")
log = logging.getLogger(__name__)
SESSION_TTL = 200
AUTO_GENERATE_SETTLE_SECONDS = 4
PANEL_SETTLE_SECONDS = 1

class BatchState(StatesGroup):
    collecting = State()

def menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ɢᴇɴᴇʀᴀᴛᴇ ʟɪɴᴋ", callback_data="batch:generate")],
        [InlineKeyboardButton(text="ᴄᴀɴᴄᴇʟ", callback_data="batch:cancel")],
    ])

def _now():
    return datetime.now(timezone.utc)


def is_batch_file(message: Message) -> bool:
    """Only count actual file/media messages; each album item arrives as its own update."""
    return any((
        message.document, message.photo, message.video, message.audio,
        message.animation, message.voice, message.video_note, message.sticker,
    ))


def ordered_batch_items(items: list[dict]) -> list[dict]:
    """Keep the exact order in which messages appear in the Database channel."""
    return sorted(items, key=lambda item: (
        int(item.get("source_message_id", 0)),
        int(item.get("original_message_id", 0)),
    ))


def _database_channel_origin(message: Message, source_channel_id: int | str):
    """Return the original storage-channel message only for a forwarded channel post."""
    origin = getattr(message, "forward_origin", None)
    origin_chat = getattr(origin, "chat", None)
    origin_message_id = getattr(origin, "message_id", None)
    if not origin_chat or not origin_message_id:
        return None
    try:
        expected_chat_id = int(source_channel_id)
        actual_chat_id = int(origin_chat.id)
        original_id = int(origin_message_id)
    except (TypeError, ValueError, AttributeError):
        return None
    if actual_chat_id != expected_chat_id:
        return None
    return actual_chat_id, original_id


async def _session(db, user_id: int):
    return await db.batch_sessions.find_one({"admin_id": user_id, "status": {"$in": ["collecting", "generating"]}})

async def _expired(session) -> bool:
    if not session:
        return True
    if session.get("status") == "generating":
        return False
    return time.time() - float(session.get("last_activity_ts", 0)) > SESSION_TTL

async def _expire_session(db, user_id: int):
    await db.batch_sessions.update_one(
        {"admin_id": user_id, "status": "collecting"},
        {"$set": {"status": "expired", "finished_at": _now()}},
    )

async def _render_session_panel(bot, db, session):
    """Render one fresh panel after a burst of file messages has settled."""
    count = len(session.get("items", []))
    if count < 2 or count >= 100 or session.get("panel_count") == count:
        return
    text = (
        f"BATCH STORAGE\n\nFiles collected: {count}/100\n\n"
        "Send 2–100 files forwarded directly from the configured Database channel.\n"
        "Files are delivered in Database-channel order. You can add more before generating."
    )
    chat_id = session.get("status_chat_id", session["admin_id"])
    old_id = session.get("status_message_id")
    sent = await bot.send_message(chat_id, text, reply_markup=menu())
    updated = await db.batch_sessions.update_one(
        {"_id": session["_id"], "status": "collecting",
         "last_activity_ts": session.get("last_activity_ts")},
        {"$set": {
            "status_message_id": sent.message_id,
            "status_chat_id": chat_id,
            "panel_count": count,
        }},
    )
    if not updated.modified_count:
        try:
            await bot.delete_message(chat_id, sent.message_id)
        except TelegramAPIError:
            pass
        return
    if old_id and old_id != sent.message_id:
        try:
            await bot.delete_message(chat_id, old_id)
        except TelegramAPIError:
            log.debug("Could not delete replaced batch panel", exc_info=True)


@router.message(Command("batch"))
async def batch_start(message: Message, state: FSMContext, settings: Settings, database):
    if not message.from_user or not await is_owner_or_moderator(database.db, message.from_user.id, settings):
        return
    if not settings.source_channel_id:
        await message.answer("Configure SOURCE_CHANNEL_ID first.")
        return
    uid = message.from_user.id
    existing = await _session(database.db, uid)
    if existing and not await _expired(existing):
        if existing.get("batch_id"):
            saved_batch = await database.db.batches.find_one({"batch_id": existing["batch_id"], "kind": "batch"})
            if saved_batch:
                me = await message.bot.get_me()
                link = f"https://t.me/{me.username}?start=batch_{saved_batch['batch_id']}"
                await database.db.batch_sessions.update_one({"_id": existing["_id"]}, {"$set": {"status": "completed", "completed_at": _now()}})
                await state.clear()
                await message.answer(link, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="ᴄᴏᴘʏ", copy_text=CopyTextButton(text=link))
                ]]))
                return
        await database.db.batch_sessions.update_one(
            {"_id": existing["_id"]},
            {"$set": {"status": "collecting", "last_activity_ts": time.time(), "last_activity_at": _now()}},
        )
        if not existing.get("batch_id") or len(str(existing.get("batch_id", ""))) != 15:
            batch_id = await FileRepository(database.db).reserve_code(15, "batch")
            await database.db.batch_sessions.update_one(
                {"_id": existing["_id"]},
                {"$set": {"batch_id": batch_id}},
            )
        await state.set_state(BatchState.collecting)
        await state.set_data({"admin_id": uid})
        count = len(existing.get("items", []))
        if not count:
            await message.answer("Send a minimum of 2 and a maximum of 100 files, directly forwarded from the configured Database channel. Use /cancel to stop.")
        return
    if existing:
        await _expire_session(database.db, uid)
    batch_id = await FileRepository(database.db).reserve_code(15, "batch")
    await database.db.batch_sessions.update_one(
        {"admin_id": uid},
        {"$set": {"admin_id": uid, "items": [], "status": "collecting",
                  "batch_id": batch_id,
                  "started_at": _now(), "last_activity_at": _now(),
                  "last_activity_ts": time.time()}},
        upsert=True,
    )
    await state.set_state(BatchState.collecting)
    await state.set_data({"admin_id": uid})
    await message.answer("Send a minimum of 2 and a maximum of 100 files, directly forwarded from the configured Database channel. Use /cancel to stop.")

@router.message(BatchState.collecting, Command("cancel"))
async def cancel_command(message: Message, state: FSMContext, settings: Settings, database):
    if not message.from_user or not await is_owner_or_moderator(database.db, message.from_user.id, settings):
        return
    session = await _session(database.db, message.from_user.id)
    if session:
        await database.db.batch_sessions.update_one(
            {"_id": session["_id"], "status": "collecting"},
            {"$set": {"status": "cancelled", "finished_at": _now()}},
        )
    await state.clear()
    await message.answer("Cancelled. Batch collection ended, your files were not saved.")

async def _collect_message(message: Message, settings: Settings, database, uid: int):
    session = await _session(database.db, uid)
    if not session:
        return False
    if await _expired(session):
        await _expire_session(database.db, uid)
        status_id = session.get("status_message_id")
        if status_id:
            try:
                await message.bot.delete_message(session.get("status_chat_id", message.chat.id), status_id)
            except TelegramAPIError:
                pass
        await message.answer("Time's up. Try again.\n/batch")
        return True
    if message.text and message.text.startswith("/"):
        return False
    if not is_batch_file(message):
        await message.answer("Please send a file or media item. A minimum of 2 files is required.")
        return True
    items = session.get("items", [])
    origin = _database_channel_origin(message, settings.source_channel_id)
    if origin is None:
        await message.answer("Batch only accepts files forwarded directly from the configured Database channel. This file was not added or copied.")
        return True
    source_chat_id, source_message_id = origin
    if any(int(item.get("source_message_id", -1)) == source_message_id for item in items):
        await message.answer("This Database-channel message is already included in the batch.")
        return True
    if len(items) >= 100:
        await database.db.batch_sessions.update_one(
            {"_id": session["_id"], "status": "collecting"},
            {"$set": {"status": "failed", "finished_at": _now(), "failure_reason": "over_100_files"}},
        )
        status_id = session.get("status_message_id")
        if status_id:
            try:
                await message.bot.delete_message(session.get("status_chat_id", message.chat.id), status_id)
            except TelegramAPIError:
                pass
        await message.answer("Batch failed: maximum is 100 files. Start a new batch with /batch.")
        return "failed"
    item = {
        # Reuse the original Database-channel post. /batch never copies external files.
        "source_chat_id": source_chat_id,
        "source_message_id": source_message_id,
        "file_name": message.document.file_name if message.document else None,
        "source_message_date": message.date.isoformat() if message.date else None,
        "original_message_id": message.message_id,
        "reply_markup": (
            message.reply_markup.model_dump(mode="json", exclude_none=True)
            if message.reply_markup else None
        ),
        **({"original_has_buttons": True} if message.reply_markup and message.reply_markup.inline_keyboard else {}),
    }
    # Conditional update prevents adding to a session cancelled or expired concurrently.
    result = await database.db.batch_sessions.update_one(
        {"_id": session["_id"], "status": "collecting",
         "$expr": {"$lt": [{"$size": "$items"}, 100]}},
        {"$push": {"items": item},
         "$max": {"last_activity_ts": time.time()},
         "$currentDate": {"last_activity_at": True}},
    )
    if not result.modified_count:
        await message.answer("Batch session changed. Use /batch to resume or start again.")
        return True
    # The background worker debounces a burst of updates into one panel.
    return True

@router.message(BatchState.collecting)
async def collect_in_state(message: Message, state: FSMContext, settings: Settings, database):
    if not message.from_user:
        return
    if message.text and message.text.startswith("/"):
        raise SkipHandler
    handled = await _collect_message(message, settings, database, message.from_user.id)
    if handled == "failed":
        await state.clear()
        return
    if not handled:
        await state.clear()
        raise SkipHandler

@router.callback_query(F.data.startswith("batch:"))
async def actions(query: CallbackQuery, state: FSMContext, settings: Settings, database):
    await query.answer()
    if not query.from_user or not await is_owner_or_moderator(database.db, query.from_user.id, settings):
        return
    uid = query.from_user.id
    action = query.data.split(":", 1)[1]
    session = await _session(database.db, uid)
    if action == "cancel":
        if session:
            await database.db.batch_sessions.update_one(
                {"_id": session["_id"], "status": "collecting"},
                {"$set": {"status": "cancelled", "finished_at": _now()}},
            )
        await state.clear()
        if query.message:
            await query.message.edit_text("Cancelled. Batch collection ended, your files were not saved.")
        return
    if not session or await _expired(session):
        if session:
            await _expire_session(database.db, uid)
        await state.clear()
        if query.message:
            try:
                await query.message.delete()
            except TelegramAPIError:
                pass
            await query.message.answer("Time's up. Try again.\n/batch")
        return
    items = ordered_batch_items(session.get("items", []))
    if len(items) < 2:
        if query.message:
            await query.message.answer(f"Add at least 2 files first. Current count: {len(items)}.")
        return
    # Mark as generating before saving to prevent double-click duplicate batches.
    claim = await database.db.batch_sessions.update_one(
        {"_id": session["_id"], "status": "collecting"},
        {"$set": {"status": "generating", "finished_at": _now()}},
    )
    if not claim.modified_count:
        if query.message:
            await query.message.answer("This batch is already being generated.")
        return
    try:
        batch = await FileRepository(database.db).save_batch(items, uid, batch_id=session.get('batch_id'))
        me = await query.bot.get_me()
        link = f"https://t.me/{me.username}?start=batch_{batch['batch_id']}"
        await database.db.batch_sessions.update_one(
            {"_id": session["_id"]},
            {"$set": {"status": "completed", "batch_id": batch["batch_id"], "completed_at": _now()}},
        )
        await state.clear()
        if query.message:
            await query.message.answer(
                link,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="ᴄᴏᴘʏ", copy_text=CopyTextButton(text=link))
                ]]),
            )
    except Exception:
        log.exception("Batch generation failed")
        await database.db.batch_sessions.update_one(
            {"_id": session["_id"], "status": "generating"},
            {"$set": {"status": "collecting", "last_error_at": _now()}},
        )
        if query.message:
            await query.message.answer("Batch link generation failed. Your collected messages are still saved; tap Generate link again.")


async def run_batch_session_worker(bot, db, poll_seconds: int = 2):
    """Auto-generate exactly-100 batches after a quiet window and expire idle sessions."""
    import asyncio
    while True:
        panel_cutoff = time.time() - PANEL_SETTLE_SECONDS
        async for panel_session in db.batch_sessions.find({
            "status": "collecting", "last_activity_ts": {"$lte": panel_cutoff},
            "$expr": {"$and": [
                {"$gte": [{"$size": "$items"}, 2]},
                {"$lt": [{"$size": "$items"}, 100]},
            ]},
        }).limit(50):
            try:
                await _render_session_panel(bot, db, panel_session)
            except TelegramAPIError:
                log.exception("Could not render batch panel for session %s", panel_session.get("_id"))
        auto_cutoff = time.time() - AUTO_GENERATE_SETTLE_SECONDS
        async for full_session in db.batch_sessions.find({
            "status": "collecting", "last_activity_ts": {"$lte": auto_cutoff},
            "$expr": {"$eq": [{"$size": "$items"}, 100]},
        }).limit(20):
            claimed = await db.batch_sessions.update_one(
                {"_id": full_session["_id"], "status": "collecting",
                 "last_activity_ts": {"$lte": auto_cutoff},
                 "$expr": {"$eq": [{"$size": "$items"}, 100]}},
                {"$set": {"status": "generating", "finished_at": _now()}},
            )
            if not claimed.modified_count:
                continue
            try:
                items = ordered_batch_items(full_session.get("items", []))
                batch = await FileRepository(db).save_batch(items, full_session["admin_id"], batch_id=full_session.get("batch_id"))
                me = await bot.get_me()
                link = f"https://t.me/{me.username}?start=batch_{batch['batch_id']}"
                await db.batch_sessions.update_one(
                    {"_id": full_session["_id"], "status": "generating"},
                    {"$set": {"status": "completed", "batch_id": batch["batch_id"], "completed_at": _now()}},
                )
                status_id = full_session.get("status_message_id")
                if status_id:
                    try:
                        await bot.delete_message(full_session.get("status_chat_id", full_session["admin_id"]), status_id)
                    except TelegramAPIError:
                        pass
                await bot.send_message(full_session["admin_id"], link, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="ᴄᴏᴘʏ", copy_text=CopyTextButton(text=link))
                ]]))
            except Exception:
                log.exception("Automatic 100-file batch generation failed")
                await db.batch_sessions.update_one(
                    {"_id": full_session["_id"], "status": "generating"},
                    {"$set": {"status": "collecting", "last_error_at": _now()}},
                )
        cutoff = time.time() - SESSION_TTL
        async for session in db.batch_sessions.find({"status": "collecting", "last_activity_ts": {"$lte": cutoff}}).limit(100):
            result = await db.batch_sessions.update_one(
                {"_id": session["_id"], "status": "collecting", "last_activity_ts": {"$lte": cutoff}},
                {"$set": {"status": "expired", "finished_at": _now()}},
            )
            if result.modified_count:
                try:
                    status_id = session.get("status_message_id")
                    status_chat = session.get("status_chat_id", session["admin_id"])
                    if status_id:
                        try:
                            await bot.delete_message(status_chat, status_id)
                        except TelegramAPIError:
                            pass
                    await bot.send_message(session["admin_id"], "Time's up. Try again.\n/batch")
                except TelegramAPIError:
                    log.info("Could not notify moderator %s that batch session expired", session["admin_id"])
        await asyncio.sleep(poll_seconds)

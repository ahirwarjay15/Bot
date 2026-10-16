"""Durable, confirmed broadcast queue with restart-safe per-recipient progress."""
import asyncio
import logging
from secrets import token_urlsafe
from datetime import datetime, timezone
from pymongo.errors import BulkWriteError
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter as RetryAfter, TelegramForbiddenError
from app.config import Settings
from app.services.permissions import is_owner_or_moderator

router = Router(name="broadcast")
log = logging.getLogger(__name__)

def now():
    return datetime.now(timezone.utc)


def format_broadcast_report(counts: dict[str, int]) -> str:
    """Report every recipient outcome, including users banned while a job runs."""
    total = sum(counts.get(status, 0) for status in ("sent", "failed", "blocked", "unknown", "skipped_banned"))
    return (
        f"BROADCAST REPORT\nRecipients: {total}\nSent successfully: {counts.get('sent', 0)}\n"
        f"Failed: {counts.get('failed', 0) + counts.get('unknown', 0)}\n"
        f"Blocked/unreachable: {counts.get('blocked', 0)}\n"
        f"Skipped (banned): {counts.get('skipped_banned', 0)}"
    )


def confirmation_keyboard(draft_key: str) -> InlineKeyboardMarkup:
    """Only show the explicit confirm/cancel choices on broadcast confirmation."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="ᴄᴏɴꜰɪʀᴍ", callback_data=f"broadcast:yes:{draft_key}"),
        InlineKeyboardButton(text="ᴄᴀɴᴄᴇʟ", callback_data=f"broadcast:no:{draft_key}"),
    ]])

@router.message(Command("broadcast"))
async def broadcast(message: Message, settings: Settings, database):
    if not message.from_user or not await is_owner_or_moderator(database.db, message.from_user.id, settings):
        return
    if not message.reply_to_message:
        await message.answer("Reply to the message you want to broadcast, then send /broadcast.")
        return
    admin_id = message.from_user.id
    active = await database.db.broadcast_drafts.find_one({"admin_id": admin_id, "status": {"$in": ["draft", "queued", "running"]}})
    if active:
        await message.answer("You already have a pending broadcast draft or active broadcast. Finish it before starting another.")
        return
    source = message.reply_to_message
    recipients = await database.db.users.count_documents({"banned": {"$ne": True}})
    await database.db.broadcast_drafts.update_one(
        {"admin_id": admin_id},
        {"$set": {"admin_id": admin_id, "chat_id": source.chat.id, "message_id": source.message_id,
                  "reply_markup": (source.reply_markup.model_dump(mode="json", exclude_none=True)
                                   if source.reply_markup else None),
                  "created_at": now(), "draft_key": token_urlsafe(8), "status": "draft"}},
        upsert=True,
    )
    draft = await database.db.broadcast_drafts.find_one({"admin_id": admin_id, "status": "draft"})
    if not draft:
        await message.answer("Could not create the broadcast draft. Please try again.")
        return
    draft_key = draft["draft_key"]
    keyboard = confirmation_keyboard(draft_key)
    await message.answer(f"BROADCAST CONFIRMATION\nReview the message before sending it to users.\nRecipients: {recipients}", reply_markup=keyboard)

@router.callback_query(F.data.startswith("broadcast:"))
async def confirm(query: CallbackQuery, settings: Settings, database):
    if not query.from_user or not await is_owner_or_moderator(database.db, query.from_user.id, settings):
        await query.answer("Not authorized.", show_alert=True)
        return
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    draft_key = parts[2] if len(parts) > 2 else ""
    admin_id = query.from_user.id
    if action in ("no", "back"):
        deleted = await database.db.broadcast_drafts.delete_one({"admin_id": admin_id, "status": "draft", "draft_key": draft_key})
        if not deleted.deleted_count:
            await query.answer("This broadcast draft is no longer active.", show_alert=True)
            return
        await query.answer("Broadcast cancelled.")
        if query.message:
            await query.message.edit_text("Broadcast cancelled." if action == "no" else "Back. Draft cancelled.")
        return
    draft = await database.db.broadcast_drafts.find_one_and_update(
        {"admin_id": admin_id, "status": "draft", "draft_key": draft_key},
        {"$set": {"status": "queued", "queued_at": now(),
                  "report_chat_id": query.message.chat.id if query.message else admin_id,
                  "report_message_id": query.message.message_id if query.message else None}},
        return_document=True,
    )
    if not draft:
        await query.answer("No pending broadcast draft, or it has already been queued.", show_alert=True)
        return
    # Create the durable job and acknowledge immediately. The worker loads recipients
    # asynchronously, so a large user database cannot make Telegram's callback time out.
    await database.db.broadcast_jobs.update_one(
        {"draft_id": draft["_id"]},
        {"$setOnInsert": {
            "draft_id": draft["_id"], "admin_id": admin_id, "source_chat_id": draft["chat_id"],
            "source_message_id": draft["message_id"], "reply_markup": draft.get("reply_markup"),
            "report_chat_id": draft.get("report_chat_id", admin_id),
            "report_message_id": draft.get("report_message_id"), "status": "preparing",
            "created_at": now(), "recipients_loaded": False,
        }},
        upsert=True,
    )
    await query.answer("Broadcast queued.")
    if query.message:
        await query.message.edit_text("Broadcast queued. You will receive the delivery report when it finishes.")

async def run_broadcast_worker(bot, db, poll_seconds: int = 2):
    """Resume queued broadcasts after restarts; a 'sending' recipient is not resent blindly."""
    # If the process stopped mid-request, outcome is ambiguous; count it as failed instead of risking a duplicate.
    await db.broadcast_recipients.update_many({"status": "sending"}, {"$set": {"status": "unknown", "error": "process restarted during send"}})
    while True:
        # Recover the narrow crash window after a draft is marked queued but before its job is inserted.
        async for draft in db.broadcast_drafts.find({"status": "queued"}).limit(20):
            await db.broadcast_jobs.update_one(
                {"draft_id": draft["_id"]},
                {"$setOnInsert": {
                    "draft_id": draft["_id"], "admin_id": draft["admin_id"],
                    "source_chat_id": draft["chat_id"], "source_message_id": draft["message_id"],
                    "reply_markup": draft.get("reply_markup"),
                    "report_chat_id": draft.get("report_chat_id", draft["admin_id"]),
                    "report_message_id": draft.get("report_message_id"),
                    "status": "preparing", "created_at": draft.get("queued_at", draft.get("created_at", now())),
                    "recipients_loaded": False,
                }},
                upsert=True,
            )
        job = await db.broadcast_jobs.find_one_and_update(
            {"status": {"$in": ["preparing", "pending", "processing"]}},
            {"$set": {"status": "processing", "worker_seen_at": now()}},
            sort=[("created_at", 1)],
        )
        if not job:
            await asyncio.sleep(poll_seconds)
            continue
        job_id = job["_id"]
        if not job.get("recipients_loaded", False):
            ids = []
            async for user in db.users.find({"banned": {"$ne": True}}, {"user_id": 1}):
                ids.append({"job_id": job_id, "user_id": user["user_id"], "status": "pending"})
            if ids:
                try:
                    await db.broadcast_recipients.insert_many(ids, ordered=False)
                except BulkWriteError:
                    pass
            await db.broadcast_jobs.update_one(
                {"_id": job_id}, {"$set": {"recipients_loaded": True, "recipient_count": len(ids), "status": "processing"}}
            )
        recipient = await db.broadcast_recipients.find_one_and_update(
            {"job_id": job_id, "status": "pending"},
            {"$set": {"status": "sending", "attempted_at": now()}},
            sort=[("user_id", 1)],
        )
        if not recipient:
            counts = {}
            for status in ("sent", "failed", "blocked", "unknown", "skipped_banned"):
                counts[status] = await db.broadcast_recipients.count_documents({"job_id": job_id, "status": status})
            report = format_broadcast_report(counts)
            try:
                if job.get("report_message_id"):
                    await bot.edit_message_text(report, chat_id=job["report_chat_id"], message_id=job["report_message_id"])
                else:
                    await bot.send_message(job["report_chat_id"], report)
            except TelegramAPIError:
                log.exception("Could not send broadcast report for job %s", job_id)
            await db.broadcast_jobs.update_one({"_id": job_id}, {"$set": {"status": "completed", "completed_at": now(), "counts": counts}})
            await db.broadcast_drafts.delete_one({"_id": job["draft_id"]})
            continue
        uid = recipient["user_id"]
        latest_user = await db.users.find_one({"user_id": uid}, {"banned": 1})
        if not latest_user or latest_user.get("banned", False):
            await db.broadcast_recipients.update_one(
                {"_id": recipient["_id"]},
                {"$set": {"status": "skipped_banned", "finished_at": now()}},
            )
            continue
        try:
            markup_data = job.get("reply_markup")
            markup = InlineKeyboardMarkup.model_validate(markup_data) if markup_data else None
            await bot.copy_message(
                chat_id=uid, from_chat_id=job["source_chat_id"],
                message_id=job["source_message_id"], reply_markup=markup,
            )
            await db.broadcast_recipients.update_one({"_id": recipient["_id"]}, {"$set": {"status": "sent", "finished_at": now()}})
        except RetryAfter as exc:
            await db.broadcast_recipients.update_one({"_id": recipient["_id"]}, {"$set": {"status": "pending", "retry_after": float(exc.retry_after)}})
            await asyncio.sleep(float(exc.retry_after) + 0.5)
        except TelegramForbiddenError:
            await db.broadcast_recipients.update_one({"_id": recipient["_id"]}, {"$set": {"status": "blocked", "finished_at": now()}})
            await db.users.update_one({"user_id": uid}, {"$set": {"blocked": True, "last_delivery_error": "forbidden"}})
        except TelegramAPIError as exc:
            await db.broadcast_recipients.update_one({"_id": recipient["_id"]}, {"$set": {"status": "failed", "error": type(exc).__name__, "finished_at": now()}})
            await db.users.update_one({"user_id": uid}, {"$set": {"last_delivery_error": type(exc).__name__, "last_delivery_error_at": now()}})
        except Exception as exc:
            log.exception("Unexpected broadcast failure for user %s", uid)
            await db.broadcast_recipients.update_one({"_id": recipient["_id"]}, {"$set": {"status": "failed", "error": type(exc).__name__, "finished_at": now()}})
        await asyncio.sleep(0.05)

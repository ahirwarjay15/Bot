"""Durable, confirmed broadcast queue with restart-safe per-recipient progress."""
import asyncio
import logging
from secrets import token_urlsafe
from datetime import datetime, timedelta, timezone
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


def _eligible_broadcast_recipient_filter(job_id, current_time):
    """Claim only pending recipients whose Telegram retry delay has elapsed."""
    return {
        "job_id": job_id,
        "status": "pending",
        "$or": [
            {"retry_at": {"$exists": False}},
            {"retry_at": {"$lte": current_time}},
        ],
    }


def _retry_wait_seconds(retry_at, current_time):
    """Convert MongoDB's possibly-naive UTC datetime into a safe sleep duration."""
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    else:
        retry_at = retry_at.astimezone(timezone.utc)
    return max(0.1, (retry_at - current_time).total_seconds())


def format_broadcast_report(counts: dict[str, int]) -> str:
    """Show mutually exclusive outcomes for this broadcast and reconcile totals."""
    statuses = ("sent", "failed", "blocked", "unknown", "skipped_banned")
    normalized = {status: max(0, int(counts.get(status, 0))) for status in statuses}
    total = sum(normalized.values())
    return (
        f"BROADCAST REPORT\nRecipients processed: {total}\n"
        f"Sent successfully: {normalized['sent']}\n"
        f"Failed: {normalized['failed']}\n"
        f"Blocked/unreachable in this broadcast: {normalized['blocked']}\n"
        f"Delivery unknown: {normalized['unknown']}\n"
        f"Skipped (banned during broadcast): {normalized['skipped_banned']}"
    )


def confirmation_keyboard(draft_key: str) -> InlineKeyboardMarkup:
    """Only show the explicit confirm/cancel choices on broadcast confirmation."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="ᴄᴏɴꜰɪʀᴍ", callback_data=f"broadcast:yes:{draft_key}"),
        InlineKeyboardButton(text="ᴄᴀɴᴄᴇʟ", callback_data=f"broadcast:no:{draft_key}"),
    ]])

@router.message(F.pinned_message)
async def remove_bot_pin_service_message(message: Message):
    pinned = message.pinned_message
    if not pinned:
        return
    try:
        bot_user = await message.bot.get_me()
        if pinned.from_user and pinned.from_user.id == bot_user.id:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except TelegramAPIError:
        log.debug("Could not remove pin service message", exc_info=True)


@router.message(Command("broadcast", "pbroadcast"))
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
    command_name = (message.text or "").split()[0].split("@")[0].lstrip("/").lower()
    pin_after_send = command_name == "pbroadcast"
    recipients = await database.db.users.count_documents({"banned": {"$ne": True}})
    await database.db.broadcast_drafts.update_one(
        {"admin_id": admin_id},
        {"$set": {"admin_id": admin_id, "chat_id": source.chat.id, "message_id": source.message_id,
                  "reply_markup": (source.reply_markup.model_dump(mode="json", exclude_none=True)
                                   if source.reply_markup else None),
                  "pin_after_send": pin_after_send,
                  "created_at": now(), "draft_key": token_urlsafe(8), "status": "draft"}},
        upsert=True,
    )
    draft = await database.db.broadcast_drafts.find_one({"admin_id": admin_id, "status": "draft"})
    if not draft:
        await message.answer("Could not create the broadcast draft. Please try again.")
        return
    draft_key = draft["draft_key"]
    keyboard = confirmation_keyboard(draft_key)
    action = "broadcast and pin" if pin_after_send else "broadcast"
    await message.answer(f"BROADCAST CONFIRMATION\nReview the message before sending it to users.\nAction: {action}\nRecipients: {recipients}", reply_markup=keyboard)

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
            "pin_after_send": bool(draft.get("pin_after_send", False)),
            "report_chat_id": draft.get("report_chat_id", admin_id),
            "report_message_id": draft.get("report_message_id"), "status": "preparing",
            "created_at": now(), "recipients_loaded": False,
        }},
        upsert=True,
    )
    await query.answer("Broadcast queued.")
    if query.message:
        await query.message.edit_text("Broadcast queued. You will receive the delivery report when it finishes.")

async def run_broadcast_worker(bot, db, poll_seconds: int = 5):
    """Resume queued broadcasts without resetting recipients owned by a live worker."""
    # Only recover recipients whose send attempt has been stale for 10+ minutes.
    # A second worker starting must not mark another worker's active send as unknown.
    stale_before = now() - timedelta(minutes=10)
    await db.broadcast_recipients.update_many(
        {"status": "sending", "attempted_at": {"$lte": stale_before}},
        {"$set": {"status": "unknown", "error": "stale send recovered", "recovered_at": now()}},
    )
    while True:
        # Recover the narrow crash window after a draft is marked queued but before its job is inserted.
        async for draft in db.broadcast_drafts.find({"status": "queued"}).limit(20):
            await db.broadcast_jobs.update_one(
                {"draft_id": draft["_id"]},
                {"$setOnInsert": {
                    "draft_id": draft["_id"], "admin_id": draft["admin_id"],
                    "source_chat_id": draft["chat_id"], "source_message_id": draft["message_id"],
                    "reply_markup": draft.get("reply_markup"),
                    "pin_after_send": bool(draft.get("pin_after_send", False)),
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
        claim_time = now()
        recipient = await db.broadcast_recipients.find_one_and_update(
            _eligible_broadcast_recipient_filter(job_id, claim_time),
            {
                "$set": {"status": "sending", "attempted_at": claim_time},
                "$unset": {"retry_at": ""},
            },
            sort=[("user_id", 1)],
        )
        if not recipient:
            # A rate-limited recipient stays pending but must not be reclaimed
            # until Telegram's RetryAfter deadline has passed. Sleep until the
            # earliest deferred recipient instead of spinning MongoDB every 0.1s.
            retry_now = now()
            deferred = await db.broadcast_recipients.find_one(
                {
                    "job_id": job_id,
                    "status": "pending",
                    "retry_at": {"$gt": retry_now},
                },
                sort=[("retry_at", 1)],
                projection={"retry_at": 1},
            )
            if deferred:
                await asyncio.sleep(_retry_wait_seconds(deferred["retry_at"], retry_now))
                continue

            # A job can be observed by multiple workers. Wait for every claimed send
            # to finish and atomically claim report finalization exactly once.
            outstanding = await db.broadcast_recipients.count_documents(
                {"job_id": job_id, "status": {"$in": ["pending", "sending"]}}
            )
            if outstanding:
                await asyncio.sleep(0.5)
                continue
            finalized = await db.broadcast_jobs.update_one(
                {"_id": job_id, "status": {"$in": ["preparing", "pending", "processing"]}},
                {"$set": {"status": "reporting", "reporting_at": now()}},
            )
            if not finalized.modified_count:
                # Another worker is already publishing this job report.
                await asyncio.sleep(0.1)
                continue
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
            # Keep counts based on the durable per-recipient outcomes until the report is built.
            await db.broadcast_drafts.delete_one({"_id": job["draft_id"]})
            await db.broadcast_recipients.delete_many({"job_id": job_id})
            await db.broadcast_jobs.delete_one({"_id": job_id})
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
            copied = await bot.copy_message(
                chat_id=uid, from_chat_id=job["source_chat_id"],
                message_id=job["source_message_id"], reply_markup=markup,
            )
            if job.get("pin_after_send"):
                try:
                    await bot.pin_chat_message(chat_id=uid, message_id=copied.message_id, disable_notification=True)
                except TelegramAPIError:
                    # A pin failure should not mark a successfully delivered broadcast as failed.
                    log.warning("Could not pin /pbroadcast message for user %s", uid, exc_info=True)
            await db.broadcast_recipients.update_one({"_id": recipient["_id"]}, {"$set": {"status": "sent", "finished_at": now()}})
            # Successful delivery means this user is reachable again.
            await db.users.update_one({"user_id": uid}, {"$set": {"blocked": False}})
        except RetryAfter as exc:
            retry_at = now() + timedelta(seconds=max(0.0, float(exc.retry_after)) + 0.5)
            await db.broadcast_recipients.update_one(
                {"_id": recipient["_id"]},
                {"$set": {"status": "pending", "retry_at": retry_at}},
            )
        except TelegramForbiddenError:
            await db.broadcast_recipients.update_one({"_id": recipient["_id"]}, {"$set": {"status": "blocked", "finished_at": now()}})
            # This is Telegram delivery status, not an admin ban.
            await db.users.update_one({"user_id": uid}, {"$set": {"blocked": True}})
        except TelegramAPIError as exc:
            await db.broadcast_recipients.update_one({"_id": recipient["_id"]}, {"$set": {"status": "failed", "error": type(exc).__name__, "finished_at": now()}})
        except Exception as exc:
            log.exception("Unexpected broadcast failure for user %s", uid)
            await db.broadcast_recipients.update_one({"_id": recipient["_id"]}, {"$set": {"status": "failed", "error": type(exc).__name__, "finished_at": now()}})
        # Avoid a fixed per-recipient delay; RetryAfter is handled explicitly above.

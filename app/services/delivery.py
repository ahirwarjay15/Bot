"""Delivery services, durable batch progress, and expiry worker."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, MessageEntity

log = logging.getLogger(__name__)
DEFAULT_NOTICE = "<blockquote><b><i>This file will be automatically deleted in {auto_delete_time}. Make sure to save it before it gets deleted..!</i></b></blockquote>"


def _now():
    return datetime.now(timezone.utc)


def _as_utc_aware(value):
    """MongoDB may decode UTC datetimes as naive; normalize before comparison."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def claim_delivery_request(db, request_key: str, user_id: int, payload: str) -> bool:
    try:
        await db.delivery_requests.insert_one({
            "request_key": request_key, "user_id": user_id, "payload": payload,
            "status": "processing", "created_at": _now(), "updated_at": _now(),
        })
        return True
    except Exception as exc:
        if getattr(exc, "code", None) != 11000:
            raise
        existing = await db.delivery_requests.find_one({"request_key": request_key})
        if not existing or existing.get("status") in ("processing", "done"):
            return False
        result = await db.delivery_requests.update_one(
            {"request_key": request_key, "status": "failed"},
            {"$set": {"status": "processing", "updated_at": _now(), "last_retry_at": _now()}},
        )
        return bool(result.modified_count)


async def finish_delivery_request(db, request_key: str, success: bool):
    # Request deduplication is only needed while the delivery is running.
    await db.delivery_requests.delete_one({"request_key": request_key})


def _minutes(cfg):
    allowed = (2, 5, 10, 15, 30, 60, 180, 360, 720, 1440)
    try:
        value = int((cfg or {}).get("minutes", 10))
    except (TypeError, ValueError):
        return 10
    return value if value in allowed else 10


def format_notice(cfg, minutes, default_notice=DEFAULT_NOTICE):
    """Build the notice and preserve any saved Telegram formatting entities."""
    cfg = cfg or {}
    value = cfg.get("notice") or default_notice
    if minutes % 60 == 0 and minutes >= 60:
        hours = minutes // 60
        label = f"{hours} hour" if hours == 1 else f"{hours} hours"
    elif minutes < 60:
        label = f"{minutes} ᴍɪɴᴜᴛᴇs"
    else:
        label = f"{minutes // 60}h {minutes % 60}m"
    marker = "{auto_delete_time}"
    entity_data = cfg.get("notice_entities")
    custom = entity_data is not None
    entities = [dict(item) for item in entity_data] if custom else []
    def units(text):
        return len(text.encode("utf-16-le")) // 2
    positions, cursor = [], 0
    while True:
        pos = value.find(marker, cursor)
        if pos < 0:
            break
        positions.append(pos)
        cursor = pos + len(marker)
    text = value
    for pos in reversed(positions):
        start, old_len, new_len = units(value[:pos]), units(marker), units(label)
        end, delta = start + old_len, new_len - old_len
        for entity in entities:
            es = int(entity.get("offset", 0))
            ee = es + int(entity.get("length", 0))
            if es >= end:
                entity["offset"] = es + delta
            elif es <= start and ee >= end:
                entity["length"] = int(entity.get("length", 0)) + delta
            elif es < start < ee < end:
                entity["length"] = start - es
            elif start < es < end < ee:
                entity["offset"] = start + new_len
                entity["length"] = ee - end
        text = text[:pos] + label + text[pos + len(marker):]
        value = value[:pos] + label + value[pos + len(marker):]
    return text, [MessageEntity.model_validate(item) for item in entities], custom


def _notice(cfg, minutes):
    return format_notice(cfg, minutes)[0]


def _compact_buttons_markup(buttons):
    """Build a keyboard from flat button rows: message_id|row|column|text|URL|color."""
    if not isinstance(buttons, list) or not buttons:
        return None
    rows = {}
    seen_buttons = set()
    for entry in buttons:
        if not isinstance(entry, str):
            continue
        parts = entry.split("|", 5)
        if len(parts) != 6:
            continue
        _, row_raw, col_raw, text, url, color = parts
        try:
            row_index, column_index = int(row_raw), int(col_raw)
        except (TypeError, ValueError):
            continue
        if row_index < 1 or column_index < 0 or not text or not url:
            continue
        identity = (row_index, column_index, text, url, color.upper())
        if identity in seen_buttons:
            continue
        seen_buttons.add(identity)
        button = {"text": text, "url": url}
        style = {"D": "danger", "S": "success", "P": "primary"}.get(color.upper())
        if style:
            button["style"] = style
        rows.setdefault(row_index, []).append((column_index, button))
    keyboard = [
        [button for _, button in sorted(rows[row_index], key=lambda pair: pair[0])]
        for row_index in sorted(rows)
    ]
    return {"inline_keyboard": keyboard} if keyboard else None


def _stored_message_ids(raw_ids):
    """Read stored IDs preserving repeated selections and their original order."""
    if isinstance(raw_ids, str):
        return [int(part.strip()) for part in raw_ids.split(",") if part.strip().isdigit()]
    if isinstance(raw_ids, list):
        return [int(part) for part in raw_ids]
    if isinstance(raw_ids, dict):
        return [int(value) for value in raw_ids.values()]
    if raw_ids is None:
        return []
    return [int(raw_ids)]


async def deliver_reference(bot: Bot, db, settings, *, user_id: int, file_doc: dict, deeplink: str, request_key: str | None = None):
    """Use compact records while resolving the fixed source channel at runtime."""
    source_chat_id = file_doc.get("chat") or file_doc.get("source_chat_id") or getattr(settings, "source_channel_id", None)
    if not source_chat_id:
        log.error("Source channel is missing for compact file record")
        return (0, 1)
    stored_ids = _stored_message_ids(file_doc.get("id", file_doc.get("source_message_id")))
    message_id = stored_ids[0] if stored_ids else file_doc.get("source_message_id")
    item = {"source_chat_id": source_chat_id, "source_message_id": message_id}
    markup_data = file_doc.get("reply_markup")
    button_data = file_doc.get("buttons")
    if isinstance(button_data, list):
        saved_markup = _compact_buttons_markup([
            entry for entry in button_data
            if isinstance(entry, str) and entry.split("|", 1)[0] == str(message_id)
        ])
        if saved_markup:
            markup_data = saved_markup
    if markup_data:
        item["reply_markup"] = markup_data
    return await deliver_batch(
        bot, db, user_id=user_id, batch={"batch_id": None, "items": [item]},
        deeplink=deeplink, request_key=request_key,
    )

async def deliver_batch(bot: Bot, db, *, user_id: int, batch: dict, deeplink: str, request_key: str | None = None):
    """Deliver directly; persist only message IDs and expiry metadata when Auto Delete is on."""
    items = list(batch.get("items", []))
    if not items and (
        isinstance(batch.get("source_message_id"), list)
        or isinstance(batch.get("id"), dict)
        or (isinstance(batch.get("id"), list) and len(batch.get("id")) > 1)
        or (isinstance(batch.get("id"), str) and "," in batch.get("id"))
    ):
        channel_id = batch.get("chat") or batch.get("source_chat_id") or batch.get("_source_chat_id")
        raw_ids = _stored_message_ids(batch.get("id", batch.get("source_message_id")))
        compact_buttons = batch.get("buttons") if isinstance(batch.get("buttons"), list) else []
        items = []
        for mid in raw_ids:
            item = {"source_chat_id": channel_id, "source_message_id": int(mid)}
            markup_data = _compact_buttons_markup([
                entry for entry in compact_buttons
                if isinstance(entry, str) and entry.split("|", 1)[0] == str(mid)
            ])
            if markup_data:
                item["reply_markup"] = markup_data
            items.append(item)
    if not items:
        return (0, 0)

    auto = await db.settings.find_one({"key": "auto_delete", "enabled": True})
    task_key = None
    deadline = None
    minutes = None
    if auto:
        minutes = _minutes(auto)
        deadline = None
        expiry_suffix = request_key or f"{user_id}:{token_urlsafe(12)}"
        task_key = f"batch-expiry:{expiry_suffix}"
        task = {
            "user_id": user_id,
            "message_ids": [],
            "deadline": None,
            "deeplink": deeplink,
            "status": "delivering",
            "updated_at": _now(),
            "messages_deleted": False,
            "after_delete_attempted": False,
            "request_key": task_key,
        }
        # This is the only persistent delivery record for new deliveries: the
        # IDs to delete, destination chat, deadline, and minimal retry metadata.
        await db.tasks.update_one(
            {"request_key": task_key}, {"$setOnInsert": task}, upsert=True
        )

    custom_cfg = await db.settings.find_one({"key": "custom_buttons"}) or {}
    custom_buttons = custom_cfg.get("buttons", [])
    is_batch = (
        (isinstance(batch.get("items"), list) and bool(batch.get("file_key")))
        or isinstance(batch.get("source_message_id"), list)
        or isinstance(batch.get("id"), dict)
        or (isinstance(batch.get("id"), list) and len(batch.get("id")) > 1)
        or (isinstance(batch.get("id"), str) and "," in batch.get("id"))
    )
    # Original supported URL buttons take priority. Custom buttons are only a fallback.
    valid_custom_buttons = [
        InlineKeyboardButton(text=str(button.get("text", "")), url=str(button.get("url", "")))
        for button in custom_buttons[:2]
        if button.get("text") and button.get("url")
    ]
    batch_markup_data = (
        InlineKeyboardMarkup(inline_keyboard=[valid_custom_buttons]).model_dump(
            mode="json", exclude_none=True
        )
        if valid_custom_buttons else None
    )
    sent_count = failed_count = 0
    # Send batch messages sequentially in the exact order stored in the batch.
    async def _deliver_one(item):
        markup_data = item.get("reply_markup")
        if not markup_data and valid_custom_buttons:
            markup_data = InlineKeyboardMarkup(inline_keyboard=[valid_custom_buttons]).model_dump(
                mode="json", exclude_none=True
            )
        markup = None
        if markup_data:
            try:
                markup = InlineKeyboardMarkup.model_validate(markup_data)
            except (ValueError, TypeError):
                log.warning("Invalid inline buttons for source message %s",
                            item["source_message_id"], exc_info=True)
        try:
            sent = await bot.copy_message(
                chat_id=user_id,
                from_chat_id=item["source_chat_id"],
                message_id=item["source_message_id"],
                reply_markup=markup,
            )
            if task_key:
                await db.tasks.update_one(
                    {"request_key": task_key},
                    {"$push": {"message_ids": sent.message_id}, "$set": {"updated_at": _now()}},
                )
            return (1, 0)
        except TelegramAPIError as exc:
            log.warning("Delivery item %s failed for user %s: %s",
                        item.get("source_message_id"), user_id, type(exc).__name__)
            return (0, 1)

    results = []
    for item in items:
        results.append(await _deliver_one(item))
    sent_count = sum(sent for sent, _ in results)
    failed_count = sum(failed for _, failed in results)

    if task_key:
        if sent_count == 0:
            await db.tasks.delete_one({"request_key": task_key})
        else:
            # Timer starts after delivery; while sending, this record cannot expire.
            deadline = _now() + timedelta(minutes=minutes)
            # Keep notice text/entities/buttons in settings, not in the task record.
            notice_text, notice_entities, custom_format = format_notice(auto, minutes)
            notice_buttons = [
                InlineKeyboardButton(
                    text=str(auto.get(f"button{i}_text", "")),
                    url=str(auto.get(f"button{i}_url", "")),
                )
                for i in (1, 2)
                if auto.get(f"button{i}_enabled", False)
                and auto.get(f"button{i}_text")
                and str(auto.get(f"button{i}_url", "")).startswith(("http://", "https://"))
            ]
            notice_markup = InlineKeyboardMarkup(inline_keyboard=[notice_buttons]) if notice_buttons else None
            try:
                if custom_format:
                    alert = await bot.send_message(
                        user_id, notice_text, entities=notice_entities,
                        parse_mode=None, reply_markup=notice_markup,
                    )
                else:
                    alert = await bot.send_message(
                        user_id, notice_text, parse_mode="HTML", reply_markup=notice_markup
                    )
                await db.tasks.update_one(
                    {"request_key": task_key},
                    {"$set": {"notice_message_id": alert.message_id}},
                )
            except TelegramAPIError:
                log.warning("Auto-delete notice could not be sent; file delivery remains successful", exc_info=True)
            await db.tasks.update_one(
                {"request_key": task_key, "status": "delivering"},
                {"$set": {"status": "pending", "deadline": deadline, "updated_at": _now()}},
            )

    return (sent_count, failed_count)


async def run_task_worker(bot: Bot, db, poll_seconds: int = 1):
    while True:
        now = _now()
        # Recover a delivery interrupted before its expiry timer was armed.
        stale_before = now - timedelta(minutes=10)
        async for stuck in db.tasks.find({
            "status": "delivering",
            "$or": [
                {"updated_at": {"$lte": stale_before}},
                {"updated_at": {"$exists": False}, "created_at": {"$lte": stale_before}},
            ],
        }).limit(50):
            if stuck.get("message_ids"):
                await db.tasks.update_one(
                    {"_id": stuck["_id"], "status": "delivering"},
                    {"$set": {"status": "pending", "deadline": now, "updated_at": now}},
                )
            else:
                await db.tasks.delete_one({"_id": stuck["_id"], "status": "delivering"})
        async for task in db.tasks.find({"status": "pending", "deadline": {"$lte": now}}).sort("deadline", 1).limit(50):
            claimed = await db.tasks.update_one({"_id": task["_id"], "status": "pending"}, {"$set": {"status": "processing", "claimed_at": _now()}})
            if not claimed.modified_count:
                continue
            try:
                # Expiry tasks represent deliveries already scheduled while Auto Delete
                # was enabled. Turning the setting off affects future deliveries only;
                # it must not silently cancel existing expiries.
                if not task.get("messages_deleted", False):
                    deletion_failed = False
                    for mid in task.get("message_ids", []):
                        try:
                            await bot.delete_message(task["user_id"], mid)
                        except TelegramBadRequest as exc:
                            if "message to delete not found" in str(exc).lower():
                                continue  # Already gone; the desired state is reached.
                            deletion_failed = True
                            log.warning("Delete attempt rejected for delivered message %s (user %s): %s", mid, task["user_id"], exc)
                        except TelegramAPIError:
                            deletion_failed = True
                            log.warning("Could not delete delivered message %s for user %s; task will retry", mid, task["user_id"], exc_info=True)
                    if deletion_failed:
                        raise RuntimeError("One or more delivered messages could not be deleted; retrying expiry task")
                    await db.tasks.update_one({"_id": task["_id"]}, {"$set": {"messages_deleted": True}})
                after = await db.settings.find_one({"key": "after_delete", "enabled": True})
                notice_id = task.get("notice_message_id")
                if after and not task.get("after_delete_attempted", False):
                    text = after.get("text") or "Yᴏᴜʀ Vɪᴅᴇᴏ / Fɪʟᴇ Iꜱ Sᴜᴄᴄᴇꜱꜰᴜʟʟʏ Dᴇʟᴇᴛᴇᴅ ✅"
                    link = task.get("deeplink")
                    markup = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text=after.get("button_text") or "Gᴇᴛ ʏᴏᴜʀ ꜰɪʟᴇꜱ ᴀɢᴀɪɴ", url=link)
                    ]]) if link else None
                    try:
                        if notice_id:
                            await bot.edit_message_text(chat_id=task["user_id"], message_id=notice_id, text=text, reply_markup=markup, parse_mode="HTML")
                        else:
                            await bot.send_message(task["user_id"], text, reply_markup=markup, parse_mode="HTML")
                    except (TelegramForbiddenError, TelegramBadRequest):
                        await db.tasks.update_one({"_id": task["_id"]}, {"$set": {"after_delete_attempted": True}})
                        log.warning("After-delete replacement cannot be edited for user %s", task["user_id"])
                    except TelegramAPIError:
                        log.exception("Transient After-delete replacement failure; expiry task will retry")
                        raise
                    else:
                        await db.tasks.update_one({"_id": task["_id"]}, {"$set": {"after_delete_attempted": True}})
                elif not after and notice_id:
                    try:
                        await bot.delete_message(task["user_id"], notice_id)
                    except TelegramAPIError:
                        log.info("Could not delete auto-delete notice %s for user %s", notice_id, task["user_id"])
                await db.tasks.update_one({"_id": task["_id"]}, {"$set": {"status": "done", "finished_at": _now()}})
                # Once expiry is complete, discard temporary delivery progress and IDs.
                # Keep user/file/batch source records untouched.
                expiry_key = task.get("request_key")
                if expiry_key:
                    delivery_key = expiry_key.removeprefix("batch-expiry:")
                    # Batch delivery queue was removed; Auto Delete uses tasks only.
                    await db.delivery_requests.delete_one({"request_key": delivery_key, "status": "done"})
                await db.tasks.delete_one({"_id": task["_id"], "status": "done"})
            except Exception:
                log.exception("Expiry task failed")
                await db.tasks.update_one({"_id": task["_id"]}, {"$set": {"status": "pending", "last_error_at": _now()}})
        await asyncio.sleep(poll_seconds)

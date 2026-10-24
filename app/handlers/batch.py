"""Create a batch deeplink from a first/last message range in the Database channel."""
import asyncio
import logging
import time
from secrets import token_urlsafe
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CopyTextButton
from aiogram.dispatcher.event.bases import SkipHandler

from app.config import Settings
from app.database.files import FileRepository, build_deeplink_token
from app.services.permissions import is_owner_or_moderator
from app.services.deeplink_channel import mirror_deeplink

router = Router(name="batch")
log = logging.getLogger(__name__)


class BatchState(StatesGroup):
    waiting_first = State()
    waiting_last = State()


def _now():
    return datetime.now(timezone.utc)


BATCH_SESSION_TIMEOUT = 10 * 60


async def _expire_batch_session(state: FSMContext, session_token: str):
    await asyncio.sleep(BATCH_SESSION_TIMEOUT)
    data = await state.get_data()
    if data.get("session_token") != session_token:
        return
    try:
        last_activity = float(data.get("last_activity_at", 0))
    except (TypeError, ValueError):
        last_activity = 0
    if time.time() - last_activity >= BATCH_SESSION_TIMEOUT:
        await state.clear()


async def _touch_batch_session(message: Message, state: FSMContext) -> bool:
    data = await state.get_data()
    session_token = data.get("session_token")
    try:
        last_activity = float(data.get("last_activity_at", 0))
    except (TypeError, ValueError):
        last_activity = 0
    if not session_token or time.time() - last_activity >= BATCH_SESSION_TIMEOUT:
        await state.clear()
        await message.answer("Batch session timed out after 10 minutes. Start again with /batch.")
        return False
    await state.update_data(last_activity_at=time.time())
    asyncio.create_task(_expire_batch_session(state, session_token))
    return True


def _origin(message: Message, configured_chat_id):
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None)
    message_id = getattr(origin, "message_id", None)
    if chat is None or not message_id:
        return None
    try:
        if int(chat.id) != int(configured_chat_id):
            return None
        return int(chat.id), int(message_id)
    except (TypeError, ValueError, AttributeError):
        return None



def _database_channel_origin(message: Message, configured_chat_id):
    """Compatibility name for the strict forwarded-origin validator."""
    return _origin(message, configured_chat_id)


def is_batch_file(message) -> bool:
    """Return whether a Telegram message contains a supported file/media item."""
    return any(getattr(message, field, None) for field in (
        "document", "photo", "video", "audio", "animation",
        "voice", "video_note", "sticker",
    ))


def ordered_batch_items(items):
    """Order batch source references by their original channel message IDs."""
    return sorted(items, key=lambda item: int(item.get("source_message_id", 0)))


async def _existing_source_message_ids(bot, destination_chat_id: int, source_chat_id: int,
                                       first_id: int, last_id: int) -> list[int]:
    """Return source IDs that can still be read; deleted posts are skipped safely."""
    available = []
    for source_id in range(first_id, last_id + 1):
        temporary = None
        try:
            temporary = await bot.forward_message(
                chat_id=destination_chat_id,
                from_chat_id=source_chat_id,
                message_id=source_id,
            )
            available.append(source_id)
        except TelegramBadRequest as exc:
            # Telegram reports a deleted/unavailable source message as BadRequest.
            log.info("Skipping unavailable source message %s: %s", source_id, exc)
        except TelegramAPIError:
            # Permission/network failures are not evidence that the source is deleted.
            raise
        finally:
            if temporary is not None:
                try:
                    await bot.delete_message(destination_chat_id, temporary.message_id)
                except TelegramAPIError:
                    log.warning("Could not remove temporary batch validation message %s",
                                temporary.message_id, exc_info=True)
    return available


async def _parse_message_reference(message: Message, bot, configured_chat_id):
    """Accept a forwarded post or a t.me/<channel>/<id> / t.me/c/<id>/<id> link."""
    origin = _origin(message, configured_chat_id)
    if origin:
        return origin[1], (message.caption if message.caption is not None else (message.text or ""))

    raw = (message.text or message.caption or "").strip()
    match = re.search(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([^\s]+)", raw)
    if not match:
        return None
    path = match.group(1).split("?")[0].strip("/")
    parts = path.split("/")
    try:
        if len(parts) >= 3 and parts[0] == "c":
            # Internal channel IDs in t.me/c links omit Telegram's -100 prefix.
            channel_id = int("-100" + parts[1])
            message_id = int(parts[2])
        elif len(parts) >= 2 and parts[0] not in ("+", "joinchat"):
            chat = await bot.get_chat("@" + parts[0].lstrip("@"))
            channel_id = int(chat.id)
            message_id = int(parts[1])
        else:
            return None
    except (ValueError, TelegramAPIError):
        return None
    if channel_id != int(configured_chat_id):
        return None
    return message_id, ""


def _link_keyboard(link: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="ᴄᴏᴘʏ", copy_text=CopyTextButton(text=link))
    ]])

async def _source_post_content(message: Message, source_chat_id: int, message_id: int):
    """Read exact text/caption and formatting entities; temporary forwards are deleted."""
    origin = _origin(message, source_chat_id)
    if origin and origin[1] == int(message_id):
        source = message
        temporary = False
    else:
        source = await message.bot.forward_message(
            chat_id=message.chat.id,
            from_chat_id=source_chat_id,
            message_id=message_id,
        )
        temporary = True
    try:
        if source.caption is not None:
            text = source.caption
            entities = source.caption_entities or []
        else:
            text = source.text or ""
            entities = source.entities or []
        serialized = [entity.model_dump(mode="json", exclude_none=True) for entity in entities]
        return text, serialized
    finally:
        if temporary:
            try:
                await message.bot.delete_message(chat_id=source.chat.id, message_id=source.message_id)
            except TelegramAPIError:
                log.warning("Could not delete temporary source-post lookup message %s", source.message_id, exc_info=True)


def _entities_from_data(data):
    from aiogram.types import MessageEntity
    return [MessageEntity.model_validate(item) for item in (data or [])]



@router.message(Command("batch"))
async def batch_start(message: Message, state: FSMContext, settings: Settings, database):
    if not message.from_user or not await is_owner_or_moderator(database.db, message.from_user.id, settings):
        return
    if not settings.source_channel_id:
        await message.answer("Configure SOURCE_CHANNEL_ID first.")
        return
    await state.clear()
    await state.set_state(BatchState.waiting_first)
    session_token = token_urlsafe(8)
    await state.update_data(
        admin_id=message.from_user.id,
        source_chat_id=int(settings.source_channel_id),
        session_token=session_token,
        last_activity_at=time.time(),
    )
    asyncio.create_task(_expire_batch_session(state, session_token))
    await message.answer(
        "Forward The Batch First Message From Database Channel (With Forward Tag).. or Give Me Batch First Message link from Database channel"
    )


@router.message(Command("cancel"))
async def cancel_batch(message: Message, state: FSMContext):
    current = await state.get_state()
    if current not in (BatchState.waiting_first.state, BatchState.waiting_last.state):
        return
    await state.clear()
    await message.answer("Batch creation cancelled.")


@router.message(BatchState.waiting_first)
async def receive_first(message: Message, state: FSMContext, settings: Settings):
    if not message.from_user:
        return
    if not await _touch_batch_session(message, state):
        return
    if message.text and message.text.startswith("/"):
        raise SkipHandler
    parsed = await _parse_message_reference(message, message.bot, settings.source_channel_id)
    if not parsed:
        await message.answer("Please forward the first Database Channel post with its forward tag, or send its message link.")
        return
    first_id, first_caption = parsed
    # Avoid forwarding posts into the admin DM just to inspect captions/entities.
    try:
        await message.bot.get_chat(int(settings.source_channel_id))
    except TelegramAPIError:
        log.exception("Bot cannot access the configured Database Channel")
        await message.answer("I couldn't access the Database Channel. Check the bot's channel permissions.")
        return
    await state.update_data(first_message_id=first_id)
    await state.set_state(BatchState.waiting_last)
    await message.answer(
        "Forward The Batch Last Message From Database Channel (With Forward Tag).. or Give Me Batch last message link from your batch channel"
    )


@router.message(BatchState.waiting_last)
async def receive_last(message: Message, state: FSMContext, settings: Settings, database):
    if not message.from_user:
        return
    if not await _touch_batch_session(message, state):
        return
    if message.text and message.text.startswith("/"):
        raise SkipHandler
    parsed = await _parse_message_reference(message, message.bot, settings.source_channel_id)
    if not parsed:
        await message.answer("Please forward the last Database Channel post with its forward tag, or send its message link.")
        return
    last_id, last_caption = parsed
    # Do not temporarily forward the last post into the admin DM.
    data = await state.get_data()
    first_id = int(data["first_message_id"])
    source_chat_id = int(data["source_chat_id"])
    if last_id < first_id:
        await message.answer("Last message must be after the first message in the Database Channel. Please send the last post again.")
        return
    count = last_id - first_id + 1
    if count < 2:
        await message.answer("Please choose at least two messages. Send the last post again.")
        return
    # No arbitrary 100-message cap: allow the full selected range. Telegram API
    # limits still apply, so large batches can take longer or need retries.
    # Do not forward every post to the admin DM for range validation.
    # Keep source references only; individual unavailable messages are handled at delivery.
    items = [{"source_chat_id": source_chat_id, "source_message_id": mid,
              "disable_buttons": True}
             for mid in range(first_id, last_id + 1)]
    batch = None
    link_send_attempted = False
    try:
        batch = await FileRepository(database.db).save_batch(items)
        me = await message.bot.get_me()
        link = f"https://t.me/{me.username}?start={build_deeplink_token(batch['file_key'])}"
        keyboard = _link_keyboard(link)
        link_send_attempted = True
        sent_link = await message.answer(link, reply_markup=keyboard)
        # Do not send source captions or source-post buttons alongside the batch link.
        await mirror_deeplink(message.bot, settings, link, keyboard)
        await state.clear()
    except Exception:
        if batch is not None and not link_send_attempted:
            try:
                await database.db.files.delete_one({"file_key": batch["file_key"]})
            except Exception:
                log.warning("Could not clean up an unlinked batch record", exc_info=True)
        log.exception("Range batch generation failed")
        await message.answer("Batch link generation failed. Check that the message links are valid and the bot can access the Database Channel.")

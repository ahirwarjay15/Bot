"""Create a batch deeplink from a first/last message range in the Database channel."""
import logging
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
    await state.update_data(admin_id=message.from_user.id, source_chat_id=int(settings.source_channel_id))
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
    if message.text and message.text.startswith("/"):
        raise SkipHandler
    parsed = await _parse_message_reference(message, message.bot, settings.source_channel_id)
    if not parsed:
        await message.answer("Please forward the first Database Channel post with its forward tag, or send its message link.")
        return
    first_id, first_caption = parsed
    try:
        first_caption, first_entities = await _source_post_content(message, int(settings.source_channel_id), first_id)
    except TelegramAPIError:
        log.exception("Could not read the first source post's text/entities")
        await message.answer("I couldn't read the first post from the Database Channel. Check that the bot can access and forward that post.")
        return
    await state.update_data(first_message_id=first_id, first_caption=first_caption, first_caption_entities=first_entities)
    await state.set_state(BatchState.waiting_last)
    await message.answer(
        "Forward The Batch Last Message From Database Channel (With Forward Tag).. or Give Me Batch last message link from your batch channel"
    )


@router.message(BatchState.waiting_last)
async def receive_last(message: Message, state: FSMContext, settings: Settings, database):
    if not message.from_user:
        return
    if message.text and message.text.startswith("/"):
        raise SkipHandler
    parsed = await _parse_message_reference(message, message.bot, settings.source_channel_id)
    if not parsed:
        await message.answer("Please forward the last Database Channel post with its forward tag, or send its message link.")
        return
    last_id, last_caption = parsed
    try:
        last_caption, last_entities = await _source_post_content(message, int(settings.source_channel_id), last_id)
    except TelegramAPIError:
        log.exception("Could not read the last source post's text/entities")
        await message.answer("I couldn't read the last post from the Database Channel. Check that the bot can access and forward that post.")
        return
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
    try:
        valid_ids = await _existing_source_message_ids(
            message.bot, message.chat.id, source_chat_id, first_id, last_id
        )
    except TelegramAPIError:
        log.exception("Could not validate the selected Database Channel range")
        await message.answer("I couldn't verify the selected messages. Check the bot's access to the Database Channel and try again.")
        return
    if len(valid_ids) < 2:
        await message.answer("At least two existing messages are required. Deleted/unavailable messages were skipped.")
        return
    # Preserve all message types (text, media, GIF, documents, etc.); only unavailable IDs are omitted.
    items = [{"source_chat_id": source_chat_id, "source_message_id": mid}
             for mid in valid_ids]
    batch = None
    link_send_attempted = False
    try:
        batch = await FileRepository(database.db).save_batch(items)
        me = await message.bot.get_me()
        link = f"https://t.me/{me.username}?start=batch_{build_deeplink_token(batch['batch_id'])}"
        keyboard = _link_keyboard(link)
        link_send_attempted = True
        sent_link = await message.answer(link, reply_markup=keyboard)
        # Reply in first-then-last order, preserving the original Telegram entities.
        captions = [
            (data.get("first_caption", ""), data.get("first_caption_entities", [])),
            (last_caption, last_entities),
        ]
        for caption, entities_data in captions:
            if caption:
                await message.bot.send_message(
                    chat_id=message.chat.id,
                    text=caption,
                    entities=_entities_from_data(entities_data),
                    parse_mode=None,
                    reply_to_message_id=sent_link.message_id,
                )
        await mirror_deeplink(message.bot, settings, link, keyboard, captions)
        await state.clear()
    except Exception:
        if batch is not None and not link_send_attempted:
            try:
                await database.db.batches.delete_one({"batch_id": batch["batch_id"], "kind": "batch"})
            except Exception:
                log.warning("Could not clean up an unlinked batch record", exc_info=True)
        log.exception("Range batch generation failed")
        await message.answer("Batch link generation failed. Check that the message links are valid and the bot can access the Database Channel.")

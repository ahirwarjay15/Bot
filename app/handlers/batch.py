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

router = Router(name="batch")
log = logging.getLogger(__name__)


class BatchState(StatesGroup):
    waiting_first = State()
    waiting_last = State()


def _now():
    return datetime.now(timezone.utc)


BATCH_SESSION_TIMEOUT = 10 * 60
MAX_BATCH_MESSAGES = 1000


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


def _origin(message: Message, configured_chat_id=None):
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None)
    message_id = getattr(origin, "message_id", None)
    if chat is None or not message_id:
        return None
    try:
        return int(chat.id), int(message_id)
    except (TypeError, ValueError, AttributeError):
        return None


def _database_channel_origin(message: Message, configured_chat_id):
    """Return the forwarded origin only when it is the configured database channel."""
    origin = _origin(message)
    if origin and int(origin[0]) == int(configured_chat_id):
        return origin
    return None


async def _bot_can_access_chat(bot, chat_id: int) -> bool:
    try:
        chat = await bot.get_chat(int(chat_id))
        if getattr(chat, "type", None) != "channel":
            return False
        me = await bot.get_me()
        member = await bot.get_chat_member(int(chat_id), me.id)
        return member.status in ("member", "administrator", "creator")
    except (TelegramAPIError, TypeError, ValueError):
        log.info("Bot cannot access batch source chat %s", chat_id, exc_info=True)
        return False


def is_batch_file(message) -> bool:
    """Return whether a Telegram message contains a supported file/media item."""
    return any(getattr(message, field, None) for field in (
        "document", "photo", "video", "audio", "animation",
        "voice", "video_note", "sticker",
    ))


def ordered_batch_items(items):
    """Order batch source references by their original channel message IDs."""
    return sorted(items, key=lambda item: int(item.get("source_message_id", 0)))


def same_source_channel(first_chat_id, last_chat_id) -> bool:
    """A range batch must never span multiple Telegram chats."""
    try:
        return int(first_chat_id) == int(last_chat_id)
    except (TypeError, ValueError):
        return False


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
    """Accept forwarded posts and public/private Telegram message links from accessible chats."""
    origin = _origin(message)
    if origin:
        source_chat_id, message_id = origin
        if await _bot_can_access_chat(bot, source_chat_id):
            return source_chat_id, message_id, (
                message.caption if message.caption is not None else (message.text or "")
            )
        return None

    raw = (message.text or message.caption or "").strip()
    match = re.search(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([^\s]+)", raw)
    if not match:
        return None
    path = match.group(1).split("?")[0].strip("/")
    parts = path.split("/")
    try:
        if len(parts) >= 3 and parts[0] == "c":
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
    if not await _bot_can_access_chat(bot, channel_id):
        return None
    return channel_id, message_id, ""


def _link_keyboard(link: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="ᴄᴏᴘʏ", copy_text=CopyTextButton(text=link))
    ]])


def _combine_caption_data(first_text, first_entities, last_text, last_entities):
    """Join first/last captions into one message and preserve Telegram entity offsets."""
    if not first_text:
        return last_text or "", list(last_entities or [])
    if not last_text or first_text == last_text:
        return first_text, list(first_entities or [])
    separator = "\n\n"
    offset = len((first_text + separator).encode("utf-16-le")) // 2
    combined_entities = list(first_entities or [])
    combined_entities.extend(
        {**entity, "offset": int(entity.get("offset", 0)) + offset}
        for entity in (last_entities or [])
    )
    return first_text + separator + last_text, combined_entities


def _entities_from_data(data):
    from aiogram.types import MessageEntity
    return [MessageEntity.model_validate(item) for item in (data or [])]



@router.message(Command("batch"))
async def batch_start(message: Message, state: FSMContext, settings: Settings, database):
    if not message.from_user or not await is_owner_or_moderator(database.db, message.from_user.id, settings):
        return
    if not settings.source_channel_id:
        await message.answer("Configure SOURCE_CHANNEL_ID first.")
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        return
    # Show the new prompt first so /batch feels immediate; clean up old UI afterward.
    prompt = await message.answer(
        "<i>Forward the batch first message from the Database Channel or another channel where the bot is a member (with forward tag), or send its message link.</i>",
        parse_mode="HTML"
    )
    old_data = await state.get_data()
    await state.clear()
    await state.set_state(BatchState.waiting_first)
    session_token = token_urlsafe(8)
    await state.update_data(
        admin_id=message.from_user.id,
        source_chat_id=int(settings.source_channel_id),
        session_token=session_token,
        last_activity_at=time.time(),
        prompt_message_id=prompt.message_id,
    )
    asyncio.create_task(_expire_batch_session(state, session_token))

    # Remove any previous batch/file prompt or collection panel only after the
    # new batch prompt is visible, so the chat never appears blank.
    old_prompt_id = old_data.get("prompt_message_id")
    old_panel_id = old_data.get("panel_message_id")
    old_panel_chat_id = old_data.get("panel_chat_id", message.chat.id)
    if old_prompt_id and int(old_prompt_id) != prompt.message_id:
        try:
            await message.bot.delete_message(message.chat.id, int(old_prompt_id))
        except TelegramAPIError:
            log.debug("Could not delete previous batch prompt", exc_info=True)
    if old_panel_id and int(old_panel_id) != prompt.message_id:
        try:
            await message.bot.delete_message(old_panel_chat_id, int(old_panel_id))
        except TelegramAPIError:
            log.debug("Could not delete previous collection panel", exc_info=True)
    try:
        await message.delete()
    except TelegramAPIError:
        pass


@router.message(Command("cancel"))
async def cancel_batch(message: Message, state: FSMContext):
    current = await state.get_state()
    if current not in (BatchState.waiting_first.state, BatchState.waiting_last.state):
        raise SkipHandler
    await state.clear()
    await message.answer("Batch creation cancelled.")
    try:
        await message.delete()
    except TelegramAPIError:
        pass


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
        await message.answer(
            "<i>Please forward the first post from the Database Channel or another channel where the bot is a member, or send its message link.</i>",
            parse_mode="HTML",
        )
        return
    first_source_chat_id, first_id, first_caption = parsed
    data = await state.get_data()
    await state.update_data(first_source_chat_id=int(first_source_chat_id), first_message_id=int(first_id))
    await state.set_state(BatchState.waiting_last)
    prompt = await message.answer(
        "<i>Forward the batch last message from the same source channel (with forward tag), or send its message link.</i>",
        parse_mode="HTML"
    )
    await state.update_data(prompt_message_id=prompt.message_id)
    try:
        prompt_id = data.get("prompt_message_id")
        if prompt_id and int(prompt_id) != prompt.message_id:
            await message.bot.delete_message(message.chat.id, int(prompt_id))
    except TelegramAPIError:
        log.debug("Could not delete first-step batch prompt", exc_info=True)
    try:
        await message.delete()
    except TelegramAPIError:
        log.debug("Could not delete submitted first batch reference", exc_info=True)


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
        await message.answer(
            "<i>Please forward the last post from the same source channel, or send its message link.</i>",
            parse_mode="HTML",
        )
        return
    last_source_chat_id, last_id, last_caption = parsed
    # Both endpoints must belong to the same accessible source channel.
    data = await state.get_data()
    first_id = int(data["first_message_id"])
    source_chat_id = int(data.get("first_source_chat_id", data["source_chat_id"]))
    if not same_source_channel(source_chat_id, last_source_chat_id):
        await message.answer(
            "These messages are from different channels. The first message is still selected; "
            "send the last message again from that same channel."
        )
        return
    if last_id < first_id:
        await message.answer("Last message must be after the first message in the selected source channel. Please send the last post again.")
        return
    count = last_id - first_id + 1
    if count < 2:
        await message.answer("Please choose at least two messages. Send the last post again.")
        return
    if count > MAX_BATCH_MESSAGES:
        await message.answer(
            f"This batch contains {count} messages. The maximum batch size is "
            f"{MAX_BATCH_MESSAGES}; please choose a smaller range."
        )
        return
    # Bound the range before allocating IDs so an accidental huge message ID
    # range cannot exhaust memory or create an impractically large MongoDB doc.
    # Keep source references only; individual unavailable messages are handled at delivery.
    # Leave buttons enabled so the configured custom buttons are added to every
    # batch item that does not already have its own source keyboard.
    items = [{"source_chat_id": source_chat_id, "source_message_id": mid}
             for mid in range(first_id, last_id + 1)]
    batch = None
    link_send_attempted = False
    progress_text = (
        "Generating link, please wait...\n\n"
        f"➛ Total Messages: {count}\n"
        "➛ Completed: 0\n"
        f"➛ Remaining: {count}"
    )
    try:
        data = await state.get_data()
        prompt_id = data.get("prompt_message_id")
        if prompt_id:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=int(prompt_id),
                text=progress_text,
                parse_mode=None,
                reply_markup=None,
            )
        else:
            progress = await message.answer(progress_text, parse_mode=None)
            prompt_id = progress.message_id
            await state.update_data(prompt_message_id=prompt_id)

        batch = await FileRepository(database.db).save_batch(
            items, chat_id=(source_chat_id if source_chat_id != int(settings.source_channel_id) else None)
        )
        me = await message.bot.get_me()
        link = f"https://t.me/{me.username}?start={build_deeplink_token(batch['key'])}"
        keyboard = _link_keyboard(link)
        link_send_attempted = True
        if prompt_id:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=int(prompt_id),
                text=(
                    "Generating link, please wait...\n\n"
                    f"➛ Total Messages: {count}\n"
                    f"➛ Completed: {count}\n"
                    "➛ Remaining: 0"
                ),
                parse_mode=None,
                reply_markup=None,
            )
        # Remove the submitted last reference, then turn the progress message into the link.
        try:
            await message.delete()
        except TelegramAPIError:
            log.debug("Could not delete submitted last batch reference", exc_info=True)
        if prompt_id:
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=int(prompt_id),
                    text=link,
                    reply_markup=keyboard,
                    parse_mode=None,
                )
                sent_link_message_id = int(prompt_id)
            except TelegramAPIError:
                sent_link = await message.answer(link, reply_markup=keyboard)
                sent_link_message_id = sent_link.message_id
        else:
            sent_link = await message.answer(link, reply_markup=keyboard)
            sent_link_message_id = sent_link.message_id

        await state.clear()
    except Exception:
        if batch is not None and not link_send_attempted:
            try:
                await database.db.files.delete_one({"key": batch["key"]})
            except Exception:
                log.warning("Could not clean up an unlinked batch record", exc_info=True)
        log.exception("Range batch generation failed")
        await message.answer("Batch link generation failed. Check that the message links are valid and the bot is a member of the source channel.")

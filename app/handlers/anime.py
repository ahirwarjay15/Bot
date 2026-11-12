"""Create one individual deeplink per anime message, in upload order."""
import asyncio
import logging

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, CopyTextButton, InlineKeyboardButton,
    InlineKeyboardMarkup, Message, MessageEntity,
)

from app.config import Settings
from app.database.files import FileRepository, build_deeplink_token
from app.services.bot_identity import get_bot_identity
from app.services.permissions import is_owner_or_moderator

router = Router(name="anime")
log = logging.getLogger(__name__)
MAX_ANIME_FILES = 100
_panel_tasks = {}

PANEL_TEXT = (
    "<i>Send more anime episode files/messages. When you're done, generate "
    "individual links below. Each link will contain exactly one file.\n"
    "You can also tap BACK to cancel this process.</i>"
)


class AnimeStates(StatesGroup):
    waiting_for_message = State()
    collecting = State()


def _panel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="GENERATE LINKS", callback_data="anime:generate")],
        [InlineKeyboardButton(text="BACK", callback_data="anime:back")],
    ])


def _source_channel_message_id(message: Message, source_channel_id):
    """Return the source post ID for modern or legacy Telegram forwards."""
    if not source_channel_id:
        return None
    expected_chat_id = int(source_channel_id)
    origin = getattr(message, "forward_origin", None)
    origin_chat = getattr(origin, "chat", None)
    origin_message_id = getattr(origin, "message_id", None)
    if origin_chat is None:
        origin_chat = getattr(message, "forward_from_chat", None)
    if origin_message_id is None:
        origin_message_id = getattr(message, "forward_from_message_id", None)
    if origin_chat is None or origin_message_id is None:
        return None
    try:
        return int(origin_message_id) if int(origin_chat.id) == expected_chat_id else None
    except (AttributeError, TypeError, ValueError):
        return None


def _url_keyboard(message: Message):
    """Keep URL buttons in the same compact format used by /file."""
    markup = message.reply_markup
    if not markup:
        return None
    saved = []
    for row_index, row in enumerate(markup.inline_keyboard, start=1):
        column_index = 0
        for button in row:
            if not button.url or not button.text:
                continue
            style = getattr(button, "style", None)
            style_code = {"danger": "D", "success": "S", "primary": "P"}.get(
                str(style).lower() if style is not None else ""
            ) or ""
            saved.append([row_index, column_index, button.text, button.url, style_code])
            column_index += 1
    return saved or None


def _link_keyboard(link: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="ᴄᴏᴘʏ", copy_text=CopyTextButton(text=link))
    ]])


async def _delete(bot, chat_id, message_id):
    if not chat_id or not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramAPIError:
        log.debug("Could not delete anime collection message %s", message_id, exc_info=True)


async def _clear_collection_ui(bot, state: FSMContext, fallback_chat_id):
    data = await state.get_data()
    await state.clear()
    await _delete(bot, data.get("prompt_chat_id", fallback_chat_id), data.get("prompt_message_id"))
    await _delete(bot, data.get("panel_chat_id", fallback_chat_id), data.get("panel_message_id"))


async def _show_panel(message: Message, state: FSMContext):
    """Send a fresh panel after one full second without another collected message."""
    key = (message.chat.id, message.from_user.id if message.from_user else 0)
    previous_task = _panel_tasks.get(key)
    if previous_task and not previous_task.done():
        previous_task.cancel()

    async def send_after_quiet_period():
        try:
            # Every newly received file restarts this quiet-period timer.
            await asyncio.sleep(1)
            data = await state.get_data()
            if await state.get_state() != AnimeStates.collecting.state:
                return

            count = len(data.get("collected", []))
            if count < MAX_ANIME_FILES:
                panel_text = PANEL_TEXT + f"\n\n<i>Files added: {count}/{MAX_ANIME_FILES}</i>"
            else:
                panel_text = (
                    f"<i>LIMIT REACHED — maximum {MAX_ANIME_FILES} files. Generate your links below.</i>"
                    f"\n\n<i>Files added: {MAX_ANIME_FILES}/{MAX_ANIME_FILES}</i>"
                )

            old_chat_id = data.get("panel_chat_id")
            old_message_id = data.get("panel_message_id")

            # A new batch always gets a new message; never edit the previous panel.
            if old_chat_id and old_message_id:
                await _delete(message.bot, old_chat_id, old_message_id)

            panel = await message.answer(
                panel_text, reply_markup=_panel_keyboard(), parse_mode="HTML"
            )
            await state.update_data(
                panel_message_id=panel.message_id,
                panel_chat_id=panel.chat.id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Could not send anime collection confirmation")
        finally:
            if _panel_tasks.get(key) is asyncio.current_task():
                _panel_tasks.pop(key, None)

    _panel_tasks[key] = asyncio.create_task(send_after_quiet_period())

async def _skip_command(message: Message, state: FSMContext):
    if not message.text or not message.text.startswith("/"):
        return
    command = message.text.split(maxsplit=1)[0].split("@", 1)[0].lower()
    if command == "/cancel":
        return
    if await state.get_state() in (
        AnimeStates.waiting_for_message.state, AnimeStates.collecting.state
    ):
        await _clear_collection_ui(message.bot, state, message.chat.id)
        raise SkipHandler


@router.message(Command("anime"))
async def anime_command(message: Message, state: FSMContext, settings: Settings, database):
    if not message.from_user or not await is_owner_or_moderator(database.db, message.from_user.id, settings):
        return
    if not settings.source_channel_id:
        await message.answer("Storage isn't configured. Set SOURCE_CHANNEL_ID to your private storage channel.")
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        return

    old_data = await state.get_data()
    prompt = await message.answer(
        "<i>Send me the anime episode <b>message/file</b> you want to add.</i>",
        parse_mode="HTML",
    )
    await state.clear()
    await state.set_state(AnimeStates.waiting_for_message)
    await state.update_data(
        prompt_message_id=prompt.message_id,
        prompt_chat_id=prompt.chat.id,
        collected=[],
    )
    await _delete(message.bot, old_data.get("prompt_chat_id", message.chat.id), old_data.get("prompt_message_id"))
    await _delete(message.bot, old_data.get("panel_chat_id", message.chat.id), old_data.get("panel_message_id"))
    try:
        await message.delete()
    except TelegramAPIError:
        pass


@router.message(Command("cancel"))
async def cancel_anime(message: Message, state: FSMContext):
    if await state.get_state() not in (
        AnimeStates.waiting_for_message.state, AnimeStates.collecting.state
    ):
        raise SkipHandler
    await _clear_collection_ui(message.bot, state, message.chat.id)
    await message.answer("Anime link creation cancelled.")
    try:
        await message.delete()
    except TelegramAPIError:
        pass


@router.callback_query(F.data == "anime:back")
async def anime_back(callback: CallbackQuery, state: FSMContext):
    await _clear_collection_ui(callback.bot, state, callback.message.chat.id)
    await callback.answer("Collection cancelled")


@router.callback_query(F.data == "anime:generate")
async def generate_anime_links(
    callback: CallbackQuery, state: FSMContext, settings: Settings, database
):
    data = await state.get_data()
    collected = data.get("collected", [])
    if not collected:
        await callback.answer("Send at least one file first.", show_alert=True)
        return
    if await state.get_state() not in (
        AnimeStates.waiting_for_message.state, AnimeStates.collecting.state
    ):
        await callback.answer("This collection has already ended. Start again with /anime.", show_alert=True)
        return

    await callback.answer()
    progress = callback.message
    await progress.edit_text("<i>Generating individual links in upload order...</i>", parse_mode="HTML")
    await state.clear()

    repo = FileRepository(database.db)
    try:
        me = await get_bot_identity(callback.bot)
    except Exception:
        log.exception("Could not resolve bot identity for anime links")
        await progress.edit_text("Could not generate anime links right now. Please try again.")
        await _delete(
            callback.bot, data.get("prompt_chat_id", callback.message.chat.id),
            data.get("prompt_message_id"),
        )
        return
    successes = 0
    published = 0
    failures = []
    first_link_message = None

    # Deliberately sequential: finish one item's copy, record, link and caption
    # reply before beginning the next item.
    for index, item in enumerate(collected, start=1):
        copied_id = None
        record = None
        link_sent = False
        try:
            if item.get("from_storage_channel"):
                source_message_id = int(item["source_message_id"])
            else:
                copied = await callback.bot.copy_message(
                    chat_id=settings.source_channel_id,
                    from_chat_id=int(item["original_chat_id"]),
                    message_id=int(item["original_message_id"]),
                )
                source_message_id = int(copied.message_id)
                copied_id = source_message_id

            # One save_file call = one unique key and exactly one source message.
            record = await repo.save_file(
                source_message_id=source_message_id,
                buttons=item.get("buttons"),
            )
            token = build_deeplink_token(record["key"])
            link = f"https://t.me/{me.username}?start={token}"

            if first_link_message is None:
                await progress.edit_text(link, reply_markup=_link_keyboard(link), parse_mode=None)
                link_message = progress
                first_link_message = progress
            else:
                link_message = await callback.message.answer(
                    link, reply_markup=_link_keyboard(link), parse_mode=None
                )
            link_sent = True
            published += 1

            # Reply with the exact original caption/text and Telegram entities;
            # do not convert through HTML/Markdown, which can alter formatting.
            reply_text = item.get("reply_text") or item.get("file_name") or "File"
            raw_entities = item.get("reply_entities") or []
            entities = [MessageEntity.model_validate(entity) for entity in raw_entities]
            await callback.bot.send_message(
                chat_id=link_message.chat.id,
                text=reply_text,
                entities=entities or None,
                parse_mode=None,
                reply_to_message_id=link_message.message_id,
            )

            # Delete only the submitted copy in the admin chat after both
            # the link and its caption reply have been sent successfully.
            await _delete(
                callback.bot, item.get("original_chat_id"), item.get("original_message_id")
            )
            successes += 1
        except Exception as exc:
            log.exception("Anime item %s/%s failed", index, len(collected))
            failures.append(index)
            # If no link was published, roll back this item's DB/storage work.
            # Once published, retain the valid single-file record and its link.
            if not link_sent and record:
                try:
                    await database.db.files.delete_one({"key": record.get("key")})
                except Exception:
                    log.warning("Could not roll back anime file record %s", index, exc_info=True)
            if not link_sent and copied_id:
                await _delete(callback.bot, settings.source_channel_id, copied_id)
            # Keep the original submitted message whenever any step failed.
            log.warning("Anime item %s failed: %s", index, exc)
            # A published link must receive its caption before the next link is sent.
            if link_sent:
                failures.extend(range(index + 1, len(collected) + 1))
                break

    await _delete(
        callback.bot, data.get("prompt_chat_id", callback.message.chat.id),
        data.get("prompt_message_id"),
    )
    # If no item could publish a link, turn the progress panel into a clear result.
    if published == 0:
        try:
            await progress.edit_text("No anime links were generated. Failed items: " + ", ".join(map(str, failures)))
        except TelegramAPIError:
            pass
    summary = f"Individual links published: {published}/{len(collected)}; captions replied: {successes}/{len(collected)}."
    if failures:
        summary += " Failed item numbers: " + ", ".join(map(str, failures)) + "."
    await callback.message.answer(summary)


@router.message(AnimeStates.waiting_for_message)
async def receive_first_anime(message: Message, state: FSMContext, settings: Settings):
    if not message.from_user:
        return
    await _skip_command(message, state)
    await state.set_state(AnimeStates.collecting)
    await _receive_anime_item(message, state, settings)


@router.message(AnimeStates.collecting)
async def receive_more_anime(message: Message, state: FSMContext, settings: Settings):
    if not message.from_user:
        return
    await _skip_command(message, state)
    await _receive_anime_item(message, state, settings)


async def _receive_anime_item(message: Message, state: FSMContext, settings: Settings):
    data = await state.get_data()
    collected = list(data.get("collected", []))
    if len(collected) >= MAX_ANIME_FILES:
        await message.answer(f"LIMIT REACHED — maximum {MAX_ANIME_FILES} files. Tap GENERATE LINKS.")
        return

    try:
        source_id = _source_channel_message_id(message, settings.source_channel_id)
        entities = message.caption_entities if message.caption is not None else message.entities
        reply_text = message.caption if message.caption is not None else message.text
        file_name = message.document.file_name if message.document else None
        item = {
            "source_message_id": source_id,
            "from_storage_channel": source_id is not None,
            "original_chat_id": int(message.chat.id),
            "original_message_id": int(message.message_id),
            "buttons": _url_keyboard(message),
            "reply_text": reply_text,
            "reply_entities": [
                entity.model_dump(exclude_none=True) for entity in (entities or [])
            ],
            "file_name": file_name,
        }
        collected.append(item)
        # Telegram message IDs increase in the order messages were sent in this chat.
        # Sort before saving so concurrent handler completion cannot reorder uploads.
        collected.sort(key=lambda entry: int(entry.get("original_message_id", 0)))
        await state.update_data(collected=collected)
        await _show_panel(message, state)
        await _delete(
            message.bot, data.get("prompt_chat_id", message.chat.id),
            data.get("prompt_message_id"),
        )
    except TelegramAPIError:
        log.exception("Could not collect anime message")
        await message.answer("Couldn't add this message to the collection. Please try again.")
    except Exception:
        log.exception("Unexpected error while collecting anime message")
        await message.answer(
            "Something went wrong while processing this message. Your collection is still open; "
            "try again or use /cancel."
        )

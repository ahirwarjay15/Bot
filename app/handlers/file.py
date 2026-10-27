"""Collect up to 50 files, then create one compact deeplink."""
import asyncio
import logging

from aiogram import Router, F
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    CopyTextButton,
)

from app.config import Settings
from app.database.files import FileRepository, build_deeplink_token
from app.services.permissions import is_owner_or_moderator

router = Router(name="file")
log = logging.getLogger(__name__)
MAX_FILES = 50
PANEL_TEXT = (
    "<i>Send more files to create your collection. When you're done, generate your link below.\n"
    "You can also tap BACK to cancel this process.</i>"
)


class FileStates(StatesGroup):
    waiting_for_message = State()
    collecting = State()


def _panel_keyboard(count):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="GENERATE LINK", callback_data="file:generate")],
        [InlineKeyboardButton(text="BACK", callback_data="file:back")],
    ])


def _url_keyboard(message):
    markup = getattr(message, "reply_markup", None)
    if not markup:
        return None
    rows = []
    for row in markup.inline_keyboard:
        saved = []
        for button in row:
            if button.url and button.text:
                saved.append(button.model_dump(mode="json", exclude_none=True))
        if saved:
            rows.append(saved)
    return rows or None


def _link_keyboard(link):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="ᴄᴏᴘʏ", copy_text=CopyTextButton(text=link))
    ]])


async def _delete(bot, chat_id, message_id):
    if not message_id or not chat_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramAPIError:
        log.debug("Could not delete collection message %s", message_id, exc_info=True)


async def _clear_collection_ui(bot, state, fallback_chat_id):
    data = await state.get_data()
    await state.clear()
    await _delete(bot, data.get("prompt_chat_id", fallback_chat_id), data.get("prompt_message_id"))
    await _delete(bot, data.get("panel_chat_id", fallback_chat_id), data.get("panel_message_id"))


async def _show_panel(message, state, *, edit=False):
    data = await state.get_data()
    count = len(data.get("collected", []))
    if count < MAX_FILES:
        text = PANEL_TEXT + f"\n\n<i>Files added: {count}/{MAX_FILES}</i>"
    else:
        text = (
            f"<i>LIMIT REACHED — maximum {MAX_FILES} files. Generate your link below.</i>"
            f"\n\n<i>Files added: {MAX_FILES}/{MAX_FILES}</i>"
        )

    # Always create a fresh panel with the updated count, then remove the old
    # one immediately. Telegram has no atomic replace operation, so this order
    # keeps the panel visible while minimizing any flicker.
    old_chat_id = data.get("panel_chat_id")
    old_message_id = data.get("panel_message_id")
    panel = await message.answer(text, reply_markup=_panel_keyboard(count), parse_mode="HTML")
    await state.update_data(panel_message_id=panel.message_id, panel_chat_id=panel.chat.id)
    if old_message_id and old_chat_id:
        await _delete(message.bot, old_chat_id, old_message_id)


async def _skip_command_and_cancel_collection(message: Message, state: FSMContext):
    """Cancel collection UI, then let aiogram route the command to its real handler."""
    if not message.text or not message.text.startswith("/"):
        return False
    command = message.text.split(maxsplit=1)[0].split("@", 1)[0].lower()
    if command == "/cancel":
        return False
    current = await state.get_state()
    if current not in (FileStates.waiting_for_message.state, FileStates.collecting.state):
        return False
    await _clear_collection_ui(message.bot, state, message.chat.id)
    raise SkipHandler


@router.message(Command("file"))
async def file_command(message: Message, state: FSMContext, settings: Settings, database):
    if not message.from_user or not await is_owner_or_moderator(database.db, message.from_user.id, settings):
        return
    if not settings.source_channel_id:
        await message.answer("Storage isn't configured. Set SOURCE_CHANNEL_ID to your private storage channel.")
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        return
    try:
        chat = await message.bot.get_chat(settings.source_channel_id)
        member = await message.bot.get_chat_member(
            settings.source_channel_id, (await message.bot.get_me()).id
        )
        if member.status not in ("administrator", "creator"):
            await message.answer(f"Bot must be an admin in {chat.title or 'the storage channel'}.")
            try:
                await message.delete()
            except TelegramAPIError:
                pass
            return
    except TelegramAPIError:
        log.exception("Storage channel preflight failed")
        await message.answer("Storage channel check failed. Verify SOURCE_CHANNEL_ID and bot admin access.")
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        return

    # Show the new prompt first so the chat never goes blank during navigation.
    old_data = await state.get_data()
    prompt = await message.answer(
        "<i>Send me the <b>message/file</b> you want to store.</i>",
        parse_mode="HTML",
    )
    await state.clear()
    await state.set_state(FileStates.waiting_for_message)
    await state.update_data(
        prompt_message_id=prompt.message_id,
        prompt_chat_id=prompt.chat.id,
        collected=[],
    )
    await _delete(message.bot, old_data.get("prompt_chat_id", message.chat.id), old_data.get("prompt_message_id"))
    old_panel_id = old_data.get("panel_message_id")
    if old_panel_id != prompt.message_id:
        await _delete(message.bot, old_data.get("panel_chat_id", message.chat.id), old_panel_id)
    try:
        await message.delete()
    except TelegramAPIError:
        pass


@router.message(Command("cancel"))
async def cancel_file(message: Message, state: FSMContext):
    if await state.get_state() not in (
        FileStates.waiting_for_message.state, FileStates.collecting.state
    ):
        raise SkipHandler
    await _clear_collection_ui(message.bot, state, message.chat.id)
    await message.answer("File link creation cancelled.")
    try:
        await message.delete()
    except TelegramAPIError:
        pass


@router.callback_query(F.data == "file:limit")
async def file_limit(callback: CallbackQuery):
    await callback.answer(ff"LIMIT REACHED — maximum {MAX_FILES} files.", show_alert=True)


@router.callback_query(F.data == "file:back")
async def file_back(callback: CallbackQuery, state: FSMContext):
    await _clear_collection_ui(callback.bot, state, callback.message.chat.id)
    await callback.answer("Collection cancelled")


@router.callback_query(F.data == "file:generate")
async def generate_file_link(
    callback: CallbackQuery, state: FSMContext, settings: Settings, database
):
    data = await state.get_data()
    collected = data.get("collected", [])
    if not collected:
        await callback.answer("Send at least one file first.", show_alert=True)
        return
    if await state.get_state() not in (
        FileStates.waiting_for_message.state, FileStates.collecting.state
    ):
        await callback.answer("This collection has already ended. Start again with /file.", show_alert=True)
        return

    await callback.answer()
    progress_message = callback.message
    await progress_message.edit_text("<i>Generating Link Wait a sec...</i>", reply_markup=None, parse_mode="HTML")
    repo = FileRepository(database.db)
    copied_ids = []
    record = None
    try:
        # Copy normal incoming messages only now, after GENERATE LINK is pressed.
        # Forwarded posts originating in the storage channel already have valid IDs.
        async def prepare_item(item, semaphore):
            if item.get("from_storage_channel"):
                return {
                    "source_message_id": int(item["source_message_id"]),
                    "buttons": item.get("buttons"),
                }
            async with semaphore:
                copied = await callback.bot.copy_message(
                    chat_id=settings.source_channel_id,
                    from_chat_id=int(item["original_chat_id"]),
                    message_id=int(item["original_message_id"]),
                )
            return {
                "source_message_id": int(copied.message_id),
                "buttons": item.get("buttons"),
            }

        # Copy a few files concurrently instead of waiting for each Telegram API
        # round-trip one by one. Collect all outcomes so successful copies can be
        # cleaned up if any item fails.
        semaphore = asyncio.Semaphore(4)
        outcomes = await asyncio.gather(
            *(prepare_item(item, semaphore) for item in collected),
            return_exceptions=True,
        )
        final_items = []
        first_error = None
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                if first_error is None:
                    first_error = outcome
                continue
            final_items.append(outcome)
        copied_ids.extend(
            int(outcome["source_message_id"])
            for item, outcome in zip(collected, outcomes)
            if not item.get("from_storage_channel") and not isinstance(outcome, Exception)
        )
        if first_error is not None:
            raise first_error

        ids = [int(item["source_message_id"]) for item in final_items]
        buttons = {
            f"{i + 1:02d}": item["buttons"]
            for i, item in enumerate(final_items) if item.get("buttons")
        }
        if len(ids) == 1:
            record = await repo.save_file(
                source_message_id=ids[0], buttons=buttons.get("01")
            )
        else:
            record = await repo.save_batch(ids, buttons=buttons)

        me = await callback.bot.get_me()
        token = build_deeplink_token(record.get("key", record.get("file_key")))
        link = f"https://t.me/{me.username}?start={token}"
        await progress_message.edit_text(link, reply_markup=_link_keyboard(link), parse_mode=None)

        # Only remove submitted originals after storage, DB record, and link succeeded.
        for item in collected:
            await _delete(
                callback.bot, item.get("original_chat_id"),
                item.get("original_message_id"),
            )
        await _delete(
            callback.bot, data.get("prompt_chat_id", callback.message.chat.id),
            data.get("prompt_message_id"),
        )
        # The progress message is edited into the final link; never delete it
        # as though it were the old collection panel.
        if data.get("panel_message_id") != progress_message.message_id:
            await _delete(
                callback.bot, data.get("panel_chat_id", callback.message.chat.id),
                data.get("panel_message_id"),
            )
        await state.clear()
    except Exception:
        log.exception("Collection link generation failed")
        # Compensate for partial failure so abandoned attempts don't pollute storage.
        if record:
            try:
                await database.db.files.delete_one({"key": record.get("key")})
            except Exception:
                log.warning("Could not remove incomplete file record", exc_info=True)
        for copied_id in copied_ids:
            await _delete(callback.bot, settings.source_channel_id, copied_id)
        try:
            await progress_message.edit_text(
                "Link generation failed. No new files should remain in the storage channel; "
                "check bot logs and try again.",
                reply_markup=None,
                parse_mode=None,
            )
        except TelegramAPIError:
            await callback.message.answer(
                "Link generation failed. No new files should remain in the storage channel; "
                "check bot logs and try again."
            )


@router.message(FileStates.waiting_for_message)
async def receive_first(message: Message, state: FSMContext, settings: Settings):
    if not message.from_user:
        return
    await _skip_command_and_cancel_collection(message, state)
    await state.set_state(FileStates.collecting)
    await _receive_collected(message, state, settings)


@router.message(FileStates.collecting)
async def receive_more(message: Message, state: FSMContext, settings: Settings):
    if not message.from_user:
        return
    await _skip_command_and_cancel_collection(message, state)
    await _receive_collected(message, state, settings)


async def _receive_collected(message: Message, state: FSMContext, settings: Settings):
    data = await state.get_data()
    collected = list(data.get("collected", []))
    if len(collected) >= MAX_FILES:
        await message.answer(ff"LIMIT REACHED — maximum {MAX_FILES} files. Tap GENERATE LINK.")
        return

    try:
        origin = message.forward_origin
        origin_chat = getattr(origin, "chat", None)
        origin_id = getattr(origin, "message_id", None)
        from_storage = False
        if origin_chat and origin_id and int(origin_chat.id) == int(settings.source_channel_id):
            source_id = int(origin_id)
            from_storage = True
            item = {
                "source_message_id": source_id,
                "from_storage_channel": True,
                "original_chat_id": int(message.chat.id),
                "original_message_id": int(message.message_id),
                "buttons": _url_keyboard(message),
            }
        else:
            # Keep the user's original message as a temporary reference. Do not
            # copy it to storage or delete it until GENERATE LINK is requested.
            item = {
                "original_chat_id": int(message.chat.id),
                "original_message_id": int(message.message_id),
                "from_storage_channel": False,
                "buttons": _url_keyboard(message),
            }
        collected.append(item)
        await state.update_data(collected=collected)
        # Replace the UI first; only then remove the obsolete prompt.
        if data.get("panel_message_id"):
            await _show_panel(message, state, edit=True)
        else:
            await _show_panel(message, state)
        await _delete(
            message.bot, data.get("prompt_chat_id", message.chat.id),
            data.get("prompt_message_id"),
        )
    except TelegramAPIError:
        log.exception("Could not collect file")
        await message.answer("Couldn't add this file to the collection. Please try again.")
    except Exception:
        log.exception("Unexpected error while collecting file")
        await message.answer(
            "Something went wrong while processing this message. Your collection is still open; "
            "try again or use /cancel."
        )

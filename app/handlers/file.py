"""Collect up to 20 files, then create one compact deeplink."""
import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, CopyTextButton
from aiogram.exceptions import TelegramAPIError
from app.config import Settings
from app.database.files import FileRepository, build_deeplink_token
from app.services.permissions import is_owner_or_moderator
from app.services.deeplink_channel import mirror_deeplink

router = Router(name="file")
log = logging.getLogger(__name__)
MAX_FILES = 20
PANEL_TEXT = ("<i>Send more files to create your collection. When you're done, generate your link below.\n"
              "You can also tap BACK to cancel this process.</i>")
class FileStates(StatesGroup):
    waiting_for_message = State()
    collecting = State()

def _panel_keyboard(count):
    rows = [[InlineKeyboardButton(text="GENERATE LINK", callback_data="file:generate")]]
    rows.append([InlineKeyboardButton(text="BACK", callback_data="file:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _url_keyboard(message):
    markup = getattr(message, "reply_markup", None)
    if not markup:
        return None
    rows = []
    for row in markup.inline_keyboard:
        saved = []
        for button in row:
            if button.url and button.text:
                saved.append({"text": button.text, "url": button.url})
        if saved:
            rows.append(saved)
    return rows or None

def _markup_from_rows(rows):
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=b["text"], url=b["url"]) for b in row]
        for row in rows if row
    ])

async def _delete(bot, chat_id, message_id):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramAPIError:
        log.debug("Could not delete collection message %s", message_id, exc_info=True)

async def _show_panel(message, state, *, edit=False):
    data = await state.get_data()
    count = len(data.get("collected", []))
    text = (PANEL_TEXT + f"\n\n<i>Files added: {count}/{MAX_FILES}</i>" if count < MAX_FILES
            else "<i>LIMIT REACHED — maximum 20 files. Generate your link below.</i>\n\n<i>Files added: 20/20</i>")
    if edit and data.get("panel_message_id"):
        try:
            await message.bot.edit_message_text(
                chat_id=data["panel_chat_id"], message_id=data["panel_message_id"],
                text=text, reply_markup=_panel_keyboard(count), parse_mode="HTML",
            )
            return
        except TelegramAPIError:
            pass
    panel = await message.answer(text, reply_markup=_panel_keyboard(count), parse_mode="HTML")
    await state.update_data(panel_message_id=panel.message_id, panel_chat_id=panel.chat.id)

@router.message(Command("file"))
async def file_command(message: Message, state: FSMContext, settings: Settings, database):
    if not message.from_user or not await is_owner_or_moderator(database.db, message.from_user.id, settings):
        return
    if not settings.source_channel_id:
        await message.answer("Storage isn't configured. Set SOURCE_CHANNEL_ID to your private storage channel.")
        return
    try:
        chat = await message.bot.get_chat(settings.source_channel_id)
        member = await message.bot.get_chat_member(settings.source_channel_id, (await message.bot.get_me()).id)
        if member.status not in ("administrator", "creator"):
            await message.answer(f"Bot must be an admin in {chat.title or 'the storage channel'}.")
            return
    except TelegramAPIError:
        log.exception("Storage channel preflight failed")
        await message.answer("Storage channel check failed. Verify SOURCE_CHANNEL_ID and bot admin access.")
        return
    await state.clear()
    await state.set_state(FileStates.waiting_for_message)
    prompt = await message.answer("Send the first file. Then you can add up to 20 files before generating the link.")
    await state.update_data(prompt_message_id=prompt.message_id, prompt_chat_id=prompt.chat.id, collected=[])

@router.message(Command("cancel"))
async def cancel_file(message: Message, state: FSMContext):
    data = await state.get_data()
    if await state.get_state() not in (FileStates.waiting_for_message.state, FileStates.collecting.state):
        return
    await state.clear()
    await _delete(message.bot, data.get("prompt_chat_id", message.chat.id), data.get("prompt_message_id", 0))
    await _delete(message.bot, data.get("panel_chat_id", message.chat.id), data.get("panel_message_id", 0))
    await message.answer("File link creation cancelled.")

@router.callback_query(F.data == "file:limit")
async def file_limit(callback: CallbackQuery):
    await callback.answer("LIMIT REACHED — maximum 20 files.", show_alert=True)

@router.callback_query(F.data == "file:back")
async def file_back(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await _delete(callback.bot, data.get("prompt_chat_id", callback.message.chat.id), data.get("prompt_message_id", 0))
    try:
        await callback.message.delete()
    except TelegramAPIError:
        pass
    await callback.answer("Collection cancelled")

@router.callback_query(F.data == "file:generate")
async def generate_file_link(callback: CallbackQuery, state: FSMContext, settings: Settings, database):
    data = await state.get_data()
    collected = data.get("collected", [])
    if not collected:
        await callback.answer("Send at least one file first.", show_alert=True)
        return
    await callback.answer("Generating link…")
    repo = FileRepository(database.db)
    try:
        ids = [int(x["source_message_id"]) for x in collected]
        buttons = {f"{i+1:02d}": item["buttons"] for i, item in enumerate(collected) if item.get("buttons")}
        if len(ids) == 1:
            record = await repo.save_file(source_message_id=ids[0], buttons=buttons.get("01"))
        else:
            record = await repo.save_batch(ids, buttons=buttons)
        me = await callback.bot.get_me()
        link = f"https://t.me/{me.username}?start={build_deeplink_token(record.get('key', record.get('file_key')))}"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="ᴄᴏᴘʏ", copy_text=CopyTextButton(text=link))
        ]])
        await callback.message.answer(link, reply_markup=keyboard)
        await mirror_deeplink(callback.bot, settings, link, keyboard, [])
        await _delete(callback.bot, data.get("prompt_chat_id", callback.message.chat.id), data.get("prompt_message_id", 0))
        await callback.message.delete()
        await state.clear()
    except Exception:
        log.exception("Collection link generation failed")
        await callback.message.answer("Link generation failed. Check bot logs and try again.")

@router.message(FileStates.waiting_for_message)
async def receive_first(message: Message, state: FSMContext, settings: Settings):
    if not message.from_user:
        return
    if message.text and message.text.startswith("/"):
        return
    await state.set_state(FileStates.collecting)
    await _receive_collected(message, state, settings)

@router.message(FileStates.collecting)
async def receive_more(message: Message, state: FSMContext, settings: Settings):
    if not message.from_user:
        return
    if message.text and message.text.startswith("/"):
        return
    await _receive_collected(message, state, settings)

async def _receive_collected(message: Message, state: FSMContext, settings: Settings):
    data = await state.get_data()
    collected = list(data.get("collected", []))
    if len(collected) >= MAX_FILES:
        await _delete(message.bot, message.chat.id, message.message_id)
        await message.answer("LIMIT REACHED — maximum 20 files. Tap GENERATE LINK.")
        return
    try:
        origin = message.forward_origin
        origin_chat = getattr(origin, "chat", None)
        origin_id = getattr(origin, "message_id", None)
        if origin_chat and origin_id and int(origin_chat.id) == int(settings.source_channel_id):
            source_id = int(origin_id)
        else:
            copied = await message.bot.copy_message(
                chat_id=settings.source_channel_id, from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            source_id = int(copied.message_id)
        collected.append({"source_message_id": source_id, "buttons": _url_keyboard(message)})
        await state.update_data(collected=collected)
        await _delete(message.bot, data.get("prompt_chat_id", message.chat.id), data.get("prompt_message_id", 0))
        await _delete(message.bot, message.chat.id, message.message_id)
        if data.get("panel_message_id"):
            await _show_panel(message, state, edit=True)
        else:
            await _show_panel(message, state)
    except TelegramAPIError:
        log.exception("Could not collect file")
        await message.answer("Couldn't store this file. Check storage channel permissions.")

"""Create permanent deeplinks by copying messages into the source channel."""
import logging
from aiogram import F,Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State,StatesGroup
from aiogram.types import Message,InlineKeyboardMarkup,InlineKeyboardButton,CopyTextButton
from aiogram.exceptions import TelegramAPIError
from aiogram.dispatcher.event.bases import SkipHandler
from app.config import Settings
from app.database.files import FileRepository
from app.services.permissions import is_owner_or_moderator
router=Router(name="file");log=logging.getLogger(__name__)
class FileStates(StatesGroup):waiting_for_message=State()

@router.message(Command("file"))
async def file_command(message:Message,state:FSMContext,settings:Settings,database):
    if not message.from_user or not await is_owner_or_moderator(database.db,message.from_user.id,settings):return
    if not settings.source_channel_id:
        await message.answer("Storage isn't configured. Set SOURCE_CHANNEL_ID to the numeric ID of your private storage channel.")
        return
    # Validate the configured ID and bot permissions before asking for a file.
    try:
        storage_chat = await message.bot.get_chat(settings.source_channel_id)
        bot_member = await message.bot.get_chat_member(
            chat_id=settings.source_channel_id,
            user_id=(await message.bot.get_me()).id,
        )
        if bot_member.status not in ("administrator", "creator"):
            await message.answer(
                f"Storage channel found ({storage_chat.title or storage_chat.id}), "
                "but the bot is not an administrator there. Promote it to admin "
                "with permission to post messages, then try /file again."
            )
            return
    except TelegramAPIError as exc:
        log.exception("Storage channel preflight failed for SOURCE_CHANNEL_ID=%s", settings.source_channel_id)
        detail = str(exc).lower()
        if "chat not found" in detail:
            reason = "Telegram cannot find this channel ID. Set SOURCE_CHANNEL_ID to the channel's numeric ID (usually starts with -100)."
        elif "bot is not a member" in detail or "user not found" in detail:
            reason = "The bot is not in this channel. Add the bot to the private storage channel and make it an administrator."
        elif "administrator" in detail or "rights" in detail:
            reason = "The bot needs administrator permission to post/copy messages in the storage channel."
        else:
            reason = f"Telegram rejected the storage channel configuration ({type(exc).__name__}). Verify SOURCE_CHANNEL_ID and bot admin access."
        await message.answer(f"Storage channel check failed. {reason}")
        return
    await state.set_state(FileStates.waiting_for_message)
    await message.answer("Send me the files you want to store. or use /cancel to stop.")

@router.message(FileStates.waiting_for_message,Command("cancel"))
async def cancel_file(message:Message,state:FSMContext):
    await state.clear();await message.answer("File link creation cancelled.")

@router.message(FileStates.waiting_for_message,F.text.startswith("/"))
async def waiting_command(message:Message,state:FSMContext):
    # Do not let file-collection mode swallow other commands such as /batch
    # or /shortener. /cancel has its own handler above.
    await state.clear()
    raise SkipHandler

@router.message(FileStates.waiting_for_message)
async def receive_file(message:Message,state:FSMContext,settings:Settings,database):
    if not message.from_user:return
    try:
        # If the user forwarded a post from the configured storage channel, reuse
        # its original channel message ID instead of trying to copy the forwarded
        # wrapper back into that same channel.
        origin = message.forward_origin
        origin_chat = getattr(origin, "chat", None)
        origin_message_id = getattr(origin, "message_id", None)
        if origin_chat and origin_message_id and origin_chat.id == settings.source_channel_id:
            source_chat_id = origin_chat.id
            source_message_id = origin_message_id
        else:
            copied = await message.bot.copy_message(
                chat_id=settings.source_channel_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            source_chat_id = settings.source_channel_id
            source_message_id = copied.message_id

        name = message.document.file_name if message.document else None
        markup_data = (
            message.reply_markup.model_dump(mode="json", exclude_none=True)
            if message.reply_markup else None
        )
        record = await FileRepository(database.db).save_file(
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            created_by=message.from_user.id,
            file_name=name,
            reply_markup=markup_data,
            original_has_buttons=bool(message.reply_markup and message.reply_markup.inline_keyboard),
        )
        me = await message.bot.get_me()
        link = f"https://t.me/{me.username}?start=file_{record['file_key']}"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="ᴄᴏᴘʏ", copy_text=CopyTextButton(text=link))
        ]])
        await message.answer(link, reply_markup=keyboard)
        # Delete the submitted message after its storage copy and link are saved.
        try:
            await message.delete()
        except TelegramAPIError:
            log.warning("Could not delete submitted message after saving", exc_info=True)
    except TelegramAPIError as exc:
        log.exception("Could not create file link")
        reason = type(exc).__name__
        if "chat not found" in str(exc).lower():
            detail = "Storage channel ID is incorrect or the bot cannot access that channel."
        elif "administrator" in str(exc).lower() or "chat_admin_required" in str(exc).lower():
            detail = "Make the bot an administrator in the private storage channel with permission to post messages."
        elif "message to copy" in str(exc).lower() or "message_id_invalid" in str(exc).lower():
            detail = "The original channel message is unavailable or its ID could not be read."
        else:
            detail = f"Telegram error: {reason}. Check the bot's storage channel access and SOURCE_CHANNEL_ID."
        await message.answer(f"Couldn't store this message. {detail}")
    except Exception:
        log.exception("File link creation failed");await message.answer("Unexpected error while saving this link. Check bot logs.")
    finally:await state.clear()

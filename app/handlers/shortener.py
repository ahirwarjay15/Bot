"""Manual URL shortening through the configured provider adapter."""
import logging
import re
from urllib.parse import urlparse

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CopyTextButton
from aiogram.exceptions import TelegramAPIError
from aiogram.dispatcher.event.bases import SkipHandler
from app.config import Settings
from app.services.permissions import is_owner_or_moderator
from app.services.links import shorten_url

router = Router(name="shortener")
log = logging.getLogger(__name__)


class ShortenerState(StatesGroup):
    waiting_for_url = State()


def extract_target(source: str) -> str | None:
    """Accept a URL alone or a URL embedded in a replied-to message."""
    urls = re.findall(r"https?://[^\s<>]+", source)
    if urls:
        target = urls[0].rstrip(").,;]")
    else:
        candidate = source.strip().rstrip(").,;]")
        if not candidate or candidate.startswith("/"):
            return None
        target = candidate if "://" in candidate else "https://" + candidate
    parsed = urlparse(target)
    return target if parsed.scheme in ("http", "https") and parsed.netloc else None


async def _shorten_and_reply(message: Message, settings: Settings, database, target: str):
    if not message.from_user or not await is_owner_or_moderator(database.db, message.from_user.id, settings):
        return
    cfg = await database.db.settings.find_one({"key": "shortener", "enabled": True})
    if not cfg:
        await message.answer("Shortener isn't configured. Open /settings → Link Shortener → ADD.")
        return
    try:
        result = await shorten_url(cfg.get("url", ""), cfg.get("token", ""), target)
        await message.answer(result, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="ᴄᴏᴘʏ", copy_text=CopyTextButton(text=result))
        ]]))
    except Exception as exc:
        log.warning("Shortener request failed (%s)", type(exc).__name__, exc_info=True)
        if isinstance(exc, ValueError):
            detail = str(exc)
        elif hasattr(exc, "status") and getattr(exc, "status", None):
            detail = f"Provider returned HTTP {exc.status}."
        else:
            detail = f"Provider request failed ({type(exc).__name__}). Check the provider URL/API token in /settings → Link Shortener."
        await message.answer(f"Couldn't shorten this URL. {detail}")


@router.message(Command("shortener"))
async def shortener_command(
    message: Message, settings: Settings, database, command: CommandObject, state: FSMContext
):
    if not message.from_user or not await is_owner_or_moderator(database.db, message.from_user.id, settings):
        return
    cfg = await database.db.settings.find_one({"key": "shortener", "enabled": True})
    if not cfg:
        await message.answer("Shortener isn't configured. Open /settings → Link Shortener → ADD.")
        return
    source = (command.args or "").strip()
    if not source and message.reply_to_message:
        source = message.reply_to_message.text or message.reply_to_message.caption or ""
    target = extract_target(source) if source else None
    if target:
        await state.clear()
        await _shorten_and_reply(message, settings, database, target)
        return
    await state.set_state(ShortenerState.waiting_for_url)
    await message.answer("Send the URL you want to shorten, or use /cancel to stop.")


@router.message(ShortenerState.waiting_for_url, Command("cancel"))
async def cancel_shortener(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("URL shortening cancelled.")


@router.message(ShortenerState.waiting_for_url, F.text.startswith("/"))
async def waiting_command(message: Message, state: FSMContext):
    """Let commands other than /cancel escape URL input mode."""
    await state.clear()
    raise SkipHandler


@router.message(ShortenerState.waiting_for_url)
async def receive_shortener_url(message: Message, state: FSMContext, settings: Settings, database):
    if not message.from_user or not await is_owner_or_moderator(database.db, message.from_user.id, settings):
        await state.clear()
        return
    source = message.text or message.caption or ""
    target = extract_target(source)
    if not target:
        await message.answer("Please send a valid URL such as https://example.com, or use /cancel.")
        return
    await state.clear()
    await _shorten_and_reply(message, settings, database, target)

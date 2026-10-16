"""Blueprint-aligned inline settings pages with persistent MongoDB values."""
from urllib.parse import urlparse
from datetime import datetime, timezone
from aiogram import Router, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, MessageEntity, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, KeyboardButtonRequestChat, ReplyKeyboardRemove
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.dispatcher.event.bases import SkipHandler
from app.config import Settings
from app.services.permissions import is_owner_or_moderator
from app.services.text_format import normalize_telegram_text
from app.services.links import shorten_url
from app.handlers.start import effective_force_join_channels

router = Router(name="settings")

def shortener_validation_error(exc: Exception, token: str) -> str:
    """Return useful provider diagnostics without echoing the submitted API token."""
    status = getattr(exc, "status", None)
    if status:
        return f"Provider returned HTTP {status}. Check the API endpoint and token."
    reason = str(exc).replace(token, "[hidden]").strip()
    if not reason:
        reason = type(exc).__name__
    return f"Provider validation failed: {reason[:160]}"


def normalize_http_url(raw: str) -> str | None:
    """Accept common URL forms while storing a canonical clickable URL."""
    value = str(raw or "").strip().strip("<>").strip()
    if not value:
        return None
    if value.startswith("@"):
        value = "https://t.me/" + value[1:]
    elif value.startswith(("t.me/", "telegram.me/", "www.")):
        value = "https://" + value
    elif "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc or "." not in parsed.netloc:
        return None
    return value


def unicode_buttons(value: str) -> str:
    if str(value) == "《 BACK" or str(value) == "📟 UPDATE CHANNEL":
        return str(value)
    table = str.maketrans({
        "A": "ᴀ", "B": "ʙ", "C": "ᴄ", "D": "ᴅ", "E": "ᴇ", "F": "ꜰ",
        "G": "ɢ", "H": "ʜ", "I": "ɪ", "J": "ᴊ", "K": "ᴋ", "L": "ʟ",
        "M": "ᴍ", "N": "ɴ", "O": "ᴏ", "P": "ᴘ", "Q": "ǫ", "R": "ʀ",
        "S": "ꜱ", "T": "ᴛ", "U": "ᴜ", "V": "ᴠ", "W": "ᴡ", "X": "x",
        "Y": "ʏ", "Z": "ᴢ",
    })
    return "".join(ch.translate(table) if ch.isascii() and ch.isalpha() else ch for ch in str(value))
AUTO_DELETE_MINUTES = (2, 5, 15, 30, 60, 180, 360, 720, 1440)
DEFAULT_NOTICE = "<b>⚠️ Cᴏᴘʏʀɪɢʜᴛ Nᴏᴛɪᴄᴇ..</b>\n<blockquote>Yᴏᴜʀ ғɪʟᴇs ᴡɪʟʟ ʙᴇ ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ ᴅᴇʟᴇᴛᴇᴅ ɪɴ {auto_delete_time}.</blockquote>\n<blockquote>ᴘʟᴇᴀsᴇ ғᴏʀᴡᴀʀᴅ ᴏʀ sᴀᴠᴇ ʏᴏᴜʀ ғɪʟᴇs sᴏᴍᴇᴡʜᴇʀᴇ sᴀғᴇ sᴏ ʏᴏᴜ ᴄᴀɴ ᴀᴄᴄᴇss ᴛʜᴇᴍ ᴀɴʏᴛɪᴍᴇ!</blockquote>"
DEFAULT_START_TEXT = "<b>Hey {user_mention}✨</b>\n\n<i>Welcome to the fastest and most secure file store bot!</i>"
DEFAULT_AFTER_TEXT = "Yᴏᴜʀ Vɪᴅᴇᴏ / Fɪʟᴇ Iꜱ Sᴜᴄᴄᴇꜱꜱꜰᴜʟʟʏ Dᴇʟᴇᴛᴇᴅ ✅"
DEFAULT_AFTER_BUTTON = "Gᴇᴛ ʏᴏᴜʀ ꜰɪʟᴇꜱ ᴀɢᴀɪɴ"
DEFAULT_INVALID = "Hi {mention}, this bot is used to deliver files. Please use a valid file link."
DEFAULT_FORCE_JOIN_MESSAGE = "Hey {mention}\nPlease Join My {remaining_count} Channel's To Use Me!"


def formatted_input_text(message, fallback):
    """Preserve Telegram formatting entities as HTML for HTML-rendered bot messages."""
    html_text = getattr(message, "html_text", None)
    if html_text:
        return html_text.strip()
    return normalize_telegram_text(fallback).strip()


def plain_preview(value):
    """Convert saved HTML-formatted text to readable plain text for alert popups."""
    import re
    from html import unescape
    return unescape(re.sub(r"</?[^>]+>", "", str(value or ""))).strip()

class SettingInput(StatesGroup):
    waiting = State()

def force_join_mode_for_chat(chat) -> str:
    """Public usernames use the public link; private chats require join approval."""
    return "normal" if getattr(chat, "username", None) else "join_request"

def valid_button_text(value: str) -> bool:
    """Telegram inline keyboard button labels must contain 1 to 64 characters."""
    text = str(value or "").strip()
    return 1 <= len(text) <= 64

async def save_force_join_channel_label(bot, db, channel: str, label: str):
    """Save a label only on its channel and repair that channel's join URL if needed."""
    values = {"key": "force_join_channel", "channel": channel, "button_text": label}
    try:
        chat_arg = int(channel) if str(channel).lstrip("-").isdigit() else channel
        chat = await bot.get_chat(chat_arg)
        values.update({
            "channel_title": chat.title or str(chat.id),
            "chat_type": chat.type,
            "mode": force_join_mode_for_chat(chat),
        })
        if chat.username:
            values["button_url"] = f"https://t.me/{chat.username}"
        else:
            # Older buggy saves could associate another channel's invite URL
            # with this ID. Generate a correct invite for the selected private chat.
            invite = await bot.create_chat_invite_link(
                chat_id=chat.id, creates_join_request=True, name="FB Force Join - Request"
            )
            values["button_url"] = invite.invite_link
    except TelegramAPIError:
        # Preserve the label update even if Telegram temporarily cannot resolve
        # the chat; a later edit can repair its URL.
        pass
    result = await db.settings.update_one(
        {"key": "force_join_channel", "channel": channel},
        {"$set": values},
        upsert=False,
    )
    if not getattr(result, "matched_count", 0):
        await db.settings.update_one(
            {"key": "force_join_channel", "channel": channel},
            {"$set": values},
            upsert=True,
        )

async def allowed(m, settings, database):
    return bool(m.from_user and await is_owner_or_moderator(database.db, m.from_user.id, settings))

async def patch(db, key, values, defaults=None):
    update = {"$set": {"key": key, **values}}
    if defaults:
        update["$setOnInsert"] = defaults
    await db.settings.update_one({"key": key}, update, upsert=True)

def time_label(minutes: int) -> str:
    return f"{minutes // 60}h" if minutes >= 60 else f"{minutes}m"

def time_words(minutes: int) -> str:
    if minutes >= 60 and minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} minutes"

def custom_button_settings_rows(buttons):
    """Render at most two configured custom buttons, one per row with its remove control."""
    rows = []
    for i, item in enumerate(buttons[:2]):
        rows.append([
            InlineKeyboardButton(text=str(item.get("text", f"Button {i+1}"))[:64], url=item.get("url")),
            InlineKeyboardButton(text="✖", callback_data=f"settings:custom_button_remove:{i}"),
        ])
    return rows


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=unicode_buttons(label), callback_data=data) for label, data in row]
        for row in rows
    ])

def format_settings_page(text: str) -> str:
    """Normalize a settings page: one underlined title, never a divider."""
    import re

    raw = str(text).replace("\u0332", "")
    lines = raw.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return ""

    heading = lines.pop(0).strip()
    # Strip old markup/decorations from title before applying one canonical style.
    heading = re.sub(r"</?(?:b|u|i)>", "", heading, flags=re.IGNORECASE)
    heading = heading.replace("_", "").strip()
    heading = " ".join(heading.split())
    # Drop any blank or divider-only lines directly beneath the title.
    while lines and (not lines[0].strip() or re.fullmatch(r"[_—\-\s]+", lines[0])):
        lines.pop(0)
    body = "\n".join(lines).rstrip()
    title = f"<b><u>{heading}</u></b>"
    if body and "<i>" not in body and "</i>" not in body:
        body = f"<i>{body}</i>"
    return title + (f"\n\n{body}" if body else "")

async def show_input_prompt(query, state, field, prompt, back_action, **extra):
    """Send a separate prompt; remember its ID so Back/Cancel can remove it."""
    if not query.message:
        return
    prompt_message = await query.message.answer(
        prompt,
        parse_mode="HTML",
        link_preview_options={"is_disabled": True},
    )
    await track_temporary_prompt(state, prompt_message)
    await state.set_state(SettingInput.waiting)
    await state.update_data(
        field=field,
        input_prompt_chat_id=prompt_message.chat.id,
        input_prompt_message_id=prompt_message.message_id,
        input_back_action=back_action,
        **extra,
    )


async def track_temporary_prompt(state, message):
    """Track standalone bot prompts so navigation can remove them."""
    data = await state.get_data()
    ids = list(data.get("temporary_prompt_ids", []))
    ids.append({"chat_id": message.chat.id, "message_id": message.message_id})
    await state.update_data(temporary_prompt_ids=ids)


async def tracked_answer(message, state, *args, **kwargs):
    """Send a settings-flow message and remember it for cleanup on page navigation."""
    sent = await message.answer(*args, **kwargs)
    await track_temporary_prompt(state, sent)
    return sent


async def clear_state_preserving_prompts(state):
    """Clear the active input flow without losing messages to delete on navigation."""
    data = await state.get_data()
    prompt_ids = list(data.get("temporary_prompt_ids", []))
    await state.clear()
    if prompt_ids:
        await state.update_data(temporary_prompt_ids=prompt_ids)


async def remove_temporary_prompts(bot, data):
    """Delete standalone prompts associated with the current settings flow."""
    prompts = list(data.get("temporary_prompt_ids", []))
    prompt_chat = data.get("input_prompt_chat_id")
    prompt_message = data.get("input_prompt_message_id")
    if prompt_chat and prompt_message:
        prompts.append({"chat_id": prompt_chat, "message_id": prompt_message})
    seen = set()
    for prompt in prompts:
        chat_id, message_id = prompt.get("chat_id"), prompt.get("message_id")
        if not chat_id or not message_id or (chat_id, message_id) in seen:
            continue
        seen.add((chat_id, message_id))
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError:
            pass


async def remove_input_prompt(bot, data):
    chat_id = data.get("input_prompt_chat_id")
    message_id = data.get("input_prompt_message_id")
    if chat_id and message_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError:
            pass


async def finish_input_panel(message, state, title, back_action):
    """Delete the temporary input prompt; send success separately and keep the panel."""
    data = await state.get_data()
    await remove_input_prompt(message.bot, data)
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    await tracked_answer(message, state, f"<b><u>{title}</u></b>\n\nYour changes have been saved successfully.", parse_mode="HTML")


async def edit_page(query, text, keyboard):
    """Edit the current settings panel, returning the message that now owns it."""
    if not query.message:
        return None
    text = format_settings_page(text)
    try:
        await query.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML", link_preview_options={"is_disabled": True})
        return query.message
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return query.message
    except TelegramAPIError:
        pass
    # If Telegram refuses the edit, replace the panel and remove the previous
    # message. Return the replacement so multi-step flows store the correct ID.
    try:
        replacement = await query.message.answer(
            text, reply_markup=keyboard, parse_mode="HTML",
            link_preview_options={"is_disabled": True},
        )
    except TelegramAPIError:
        return query.message
    try:
        await query.message.delete()
    except TelegramAPIError:
        pass
    return replacement

async def render_home(query):
    me = await query.bot.get_me()
    # The bot name remains a clickable mention, while previews are disabled in edit_page.
    import html
    name = f'<a href="https://t.me/{me.username}">{html.escape(me.full_name)}</a>' if me.username else html.escape(me.full_name)
    await edit_page(query, f"CUSTOMIZE\n<i>✰ Name: {name}</i>\n\nConfigure Bot Settings Using the Buttons Below.", kb([
        [("START MSG", "settings:start"), ("FORCE JOIN", "settings:forcejoin")],
        [("ANIMATION", "settings:forcejoin_animation"), ("AUTO DELETE", "settings:auto")],
        [("INVALID", "settings:invalid"), ("SHORTENER", "settings:shortener")],
        [("MODERATORS", "settings:mods"), ("STATS", "settings:stats")],
        [("CUSTOM BUTTON", "settings:custom_button")],
    ]))

async def build_forcejoin_page(bot, db, settings):
    channels = await effective_force_join_channels(type("DatabaseView", (), {"db": db})(), settings)
    cfg = await db.settings.find_one({"key": "force_join"}) or {}
    channel_buttons = []
    for i, channel in enumerate(channels[:10]):
        channel_cfg = await db.settings.find_one({"key": "force_join_channel", "channel": channel}) or {}
        # The admin's Added Channels list always shows the channel/group name,
        # never the user-facing custom verification button text.
        try:
            chat = await bot.get_chat(int(channel) if str(channel).lstrip("-").isdigit() else channel)
            label = chat.title or chat.full_name or channel_cfg.get("channel_title") or chat.username
        except (TelegramAPIError, ValueError):
            label = channel_cfg.get("channel_title") or channel.lstrip("@")
        channel_buttons.append(((label or channel.lstrip("@"))[:64], f"settings:forcejoin_channel:{i}"))
    rows = [channel_buttons[i:i+2] for i in range(0, len(channel_buttons), 2)]
    if len(channels) < 10:
        rows.append([("✚ ADD", "settings:forcejoin_add")])
    rows.extend([
        [("FORCE JOIN MESSAGE", "settings:forcejoin_message"), ("FORCE JOIN MEDIA", "settings:forcejoin_media")],
        [("VERIFY", "settings:forcejoin_verify_text"),
         ("DISABLE" if cfg.get("enabled", False) else "ENABLE", "settings:forcejoin_toggle")],
        [("《 BACK", "settings:home")],
    ])
    text = ("FORCE JOIN\n\n"
            f"Status: {'ENABLED ✅' if cfg.get('enabled', False) else 'DISABLED ❌'}\n\n"
            "Users must verify the required channels before receiving files.\n\n"
            "You can add up to 10 force-join channels.")
    return text, kb(rows)


async def render_forcejoin(query, db, settings):
    text, markup = await build_forcejoin_page(query.bot, db, settings)
    await edit_page(query, text, markup)

async def render_forcejoin_message(query):
    await edit_page(query, "FORCE JOIN MESSAGE\nCustomize the message shown to users who still need to join required channels.", kb([
        [("EDIT", "settings:forcejoin_message_edit"), ("SEE", "settings:forcejoin_message_see")],
        [("DEFAULT", "settings:forcejoin_message_default")],
        [("《 BACK", "settings:forcejoin")],
    ]))


async def render_invalid_text(query):
    await edit_page(query, "INVALID POST TEXT\nCustomize the reply shown when users send messages instead of a valid file link.", kb([
        [("EDIT", "settings:invalid_text_edit"), ("SEE", "settings:invalid_text_see")],
        [("DEFAULT", "settings:invalid_text_default")],
        [("《 BACK", "settings:invalid")],
    ]))


async def render_start(query, db):
    cfg = await db.settings.find_one({"key": "start_message"}) or {}
    about_enabled = bool(cfg.get("about_enabled", True))
    await edit_page(query, f"START MESSAGE\n\nCustomize your bot's start message using the following options.\n\nStatus: {'ENABLED ✅' if cfg.get('enabled', True) else 'DISABLED ❌'}\nAbout Button: {'ON ✅' if about_enabled else 'OFF ❌'}", kb([
        [("DISABLE" if cfg.get("enabled", True) else "ENABLE", "settings:start_toggle")],
        [("BUTTON", "settings:start_button"), ("START TEXT", "settings:start_text")],
        [("START POSTER", "settings:start_poster"), ("ABOUT", "settings:start_about_toggle")],
        [("《 BACK", "settings:home")],
    ]))

async def render_start_text(query):
    await edit_page(query, "START TEXT\n\nCustomize your bot's start message.", kb([
        [("EDIT", "settings:start_text_edit"), ("SEE", "settings:start_text_see")],
        [("DEFAULT", "settings:start_text_default")],
        [("《 BACK", "settings:start")],
    ]))

async def render_start_button(query, db):
    cfg = await db.settings.find_one({"key": "start_message"}) or {}
    status1 = "ON ✅" if cfg.get("button1_enabled", cfg.get("button_enabled", True)) else "OFF ❌"
    status2 = "ON ✅" if cfg.get("button2_enabled", False) else "OFF ❌"
    text = f"START MESSAGE BUTTONS\nCustomize buttons shown with your start message.\n\nButton 1: {status1}\nButton 2: {status2}"
    await edit_page(query, text, kb([
        [(f"1 {status1}", "settings:start_button_toggle:1"), (f"2 {status2}", "settings:start_button_toggle:2")],
        [("1 BUTTON", "settings:start_button_1"), ("2 BUTTON", "settings:start_button_2")],
        [("《 BACK", "settings:start")],
    ]))
    return query.message


async def refresh_button_panel(bot, chat_id, message_id, db, kind):
    """Refresh the existing button-settings panel after text/link input."""
    if not chat_id or not message_id:
        return
    is_start = kind == "start"
    cfg = await db.settings.find_one({"key": "start_message" if is_start else "invalid_post"}) or {}
    status1 = "ON ✅" if cfg.get("button1_enabled", cfg.get("button_enabled", True) if is_start else bool(cfg.get("button_text") and cfg.get("button_url"))) else "OFF ❌"
    status2 = "ON ✅" if cfg.get("button2_enabled", False) else "OFF ❌"
    title = "START MESSAGE BUTTONS" if is_start else "INVALID POST BUTTONS"
    back = "settings:start" if is_start else "settings:invalid"
    prefix = "start" if is_start else "invalid"
    page_text = format_settings_page(f"{title}\nCustomize buttons shown with {'your start message' if is_start else 'invalid posts'}.\n\nButton 1: {status1}\nButton 2: {status2}")
    keyboard = kb([
        [(f"1 {status1}", f"settings:{prefix}_button_toggle:1"), (f"2 {status2}", f"settings:{prefix}_button_toggle:2")],
        [("1 BUTTON", f"settings:{prefix}_button_1"), ("2 BUTTON", f"settings:{prefix}_button_2")],
        [("《 BACK", back)],
    ])
    try:
        # Keep the inline controls attached when refreshing the existing panel.
        # In particular, the BACK callback must remain available after saving text/link.
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=page_text,
            parse_mode="HTML",
            reply_markup=keyboard,
            link_preview_options={"is_disabled": True},
        )
    except TelegramAPIError:
        pass

async def render_start_poster(query):
    await edit_page(query, "START MEDIA\n\nInclude an image or GIF with your start message.", kb([
        [("✚ ADD", "settings:start_poster_add"), ("REMOVE", "settings:start_poster_remove")],
        [("《 BACK", "settings:start")],
    ]))


async def render_start_about(query, db):
    await render_start(query, db)

def _animation_mode_for_action(action: str) -> str:
    """Translate the two explicit mode buttons to their persisted values."""
    return "new_message" if action == "animation_mode_new" else "force_join_poster"


async def render_animation(query, db):
    cfg = await db.settings.find_one({"key": "animation"}) or {}
    legacy_default = not cfg.get("prefix") or cfg.get("prefix") == "Checking.."
    if legacy_default:
        cfg.update({"prefix": "Checking", "frames": [".", "..", "..."], "speed": 0.25})
        await db.settings.update_one(
            {"key": "animation"},
            {"$set": {"prefix": "Checking", "frames": [".", "..", "..."], "speed": 0.25}},
            upsert=True,
        )
    frames = cfg.get("frames")
    if not isinstance(frames, list) or len(frames) != 3:
        frames = [".", "..", "..."]
    frames_text = " | ".join(str(frame) for frame in frames)
    prefix = str(cfg.get("prefix") or "Checking")
    mode = cfg.get("mode", "force_join_poster")
    mode_label = "NEW MESSAGE" if mode == "new_message" else "FORCE JOIN POSTER"
    try:
        speed = float(cfg.get("speed", 0.25))
    except (TypeError, ValueError):
        speed = 1.0
    speed = min(1.0, max(0.25, speed))
    if speed != cfg.get("speed"):
        await db.settings.update_one({"key": "animation"}, {"$set": {"speed": speed}}, upsert=True)
    await edit_page(query, f"ANIMATION\n\nAdd a custom animation for the verification process.\n\nPrefix: {prefix}\nFrames: {frames_text}\nSpeed: {speed:.2f}s per frame\nMode: {mode_label}\nStatus: {'ENABLED ✅' if cfg.get('enabled', False) else 'DISABLED ❌'}", kb([
        [("PREFIX", "settings:animation_prefix"), ("FRAMES", "settings:animation_frames")],
        [("DISABLE" if cfg.get("enabled", False) else "ENABLE", "settings:animation_toggle"), ("SPEED", "settings:animation_speed")],
        [("MODE", "settings:animation_mode_toggle")],
        [("《 BACK", "settings:home")],
    ]))
async def render_auto(query, db):
    cfg = await db.settings.find_one({"key": "auto_delete"}) or {}
    after = await db.settings.find_one({"key": "after_delete"}) or {}
    minutes = int(cfg.get("minutes", 30))
    status = "ENABLED ✅" if cfg.get("enabled", False) else "DISABLED ❌"
    after_status = "ENABLED ✅" if after.get("enabled", False) and cfg.get("enabled", False) else "DISABLED ❌"
    await edit_page(query, f"AUTO DELETE\nAutomatically delete delivered files and the auto-delete message after {time_label(minutes)}.\nStatus: {status}", kb([
        [("DISABLE" if cfg.get("enabled", False) else "ENABLE", "settings:auto_toggle")],
        [("TIME", "settings:auto_time"), ("MESSAGE", "settings:auto_message")],
        [(f"AFTER DELETE ({after_status})", "settings:after")],
        [("《 BACK", "settings:home")],
    ]))

async def render_auto_time(query, db):
    cfg = await db.settings.find_one({"key": "auto_delete"}) or {}
    minutes = int(cfg.get("minutes", 30))
    label = time_label(minutes)
    await edit_page(query, f"AUTO DELETE TIME\nCurrent time: {label}\nTap below to change the auto-delete time.", kb([
        [("CHANGE TIME", "settings:auto_time_change")],
        [("《 BACK", "settings:auto")],
    ]))

async def render_auto_message(query, db):
    cfg = await db.settings.find_one({"key": "auto_delete"}) or {}
    await edit_page(query, "AUTO DELETE MESSAGE\nCustomize the message sent after file delivery.", kb([
        [("EDIT", "settings:auto_message_edit"), ("SEE", "settings:auto_message_see")],
        [("DEFAULT", "settings:auto_message_default")],
        [("《 BACK", "settings:auto")],
    ]))

async def render_after(query, db):
    auto = await db.settings.find_one({"key": "auto_delete"}) or {}
    after = await db.settings.find_one({"key": "after_delete"}) or {}
    enabled = bool(after.get("enabled", False) and auto.get("enabled", False))
    await edit_page(query, f"AFTER DELETE\nCustomize the message and button shown after delivered files are deleted.\nStatus: {'ENABLED ✅' if enabled else 'DISABLED ❌'}", kb([
        [("DISABLE" if enabled else "ENABLE", "settings:after_toggle")],
        [("TEXT", "settings:after_text")],
        [("《 BACK", "settings:auto")],
    ]))

async def render_after_text(query):
    await edit_page(query, "AFTER DELETE TEXT\nCustomize the message and button text shown after file deletion.", kb([
        [("MESSAGE TEXT", "settings:after_text_message")],
        [("BUTTON TEXT", "settings:after_text_button")],
        [("《 BACK", "settings:after")],
    ]))

async def render_invalid(query, db):
    cfg = await db.settings.find_one({"key": "invalid_post"}) or {}
    about_enabled = bool(cfg.get("about_enabled", False))
    await edit_page(query, f"INVALID POST\nSet a response for users who send normal messages to the bot.\nStatus: {'ENABLED ✅' if cfg.get('enabled', False) else 'DISABLED ❌'}\nAbout Button: {'ON ✅' if about_enabled else 'OFF ❌'}", kb([
        [("DISABLE" if cfg.get("enabled", False) else "ENABLE", "settings:invalid_toggle")],
        [("TEXT", "settings:invalid_text"), ("BUTTON", "settings:invalid_button")],
        [("POSTER", "settings:invalid_poster"), ("ABOUT" if about_enabled else "ABOUT", "settings:invalid_about_toggle")],
        [("《 BACK", "settings:home")],
    ]))

async def render_invalid_button(query, db):
    cfg = await db.settings.find_one({"key": "invalid_post"}) or {}
    status1 = "ON ✅" if cfg.get("button1_enabled", bool(cfg.get("button_text") and cfg.get("button_url"))) else "OFF ❌"
    status2 = "ON ✅" if cfg.get("button2_enabled", False) else "OFF ❌"
    text = f"INVALID POST BUTTONS\nCustomize buttons shown with invalid posts.\n\nButton 1: {status1}\nButton 2: {status2}"
    await edit_page(query, text, kb([
        [(f"1 {status1}", "settings:invalid_button_toggle:1"), (f"2 {status2}", "settings:invalid_button_toggle:2")],
        [("1 BUTTON", "settings:invalid_button_1"), ("2 BUTTON", "settings:invalid_button_2")],
        [("《 BACK", "settings:invalid")],
    ]))
    return query.message


async def render_poster(query):
    await edit_page(query, "INVALID POSTER\nAdd an image or GIF to your invalid post.", kb([
        [("✚ ADD", "settings:poster_add"), ("REMOVE", "settings:poster_remove")],
        [("《 BACK", "settings:invalid")],
    ]))

async def render_about(query, db):
    cfg = await db.settings.find_one({"key": "invalid_post"}) or {}
    await edit_page(query, f"ABOUT BUTTON\n\nChoose whether to show the About button with invalid posts.\n\nStatus: {'ENABLED ✅' if cfg.get('about_enabled', False) else 'DISABLED ❌'}", kb([
        [("DISABLE" if cfg.get("about_enabled", False) else "ENABLE", "settings:invalid_about_toggle")],
        [("《 BACK", "settings:invalid")],
    ]))

async def render_shortener(query, db):
    cfg = await db.settings.find_one({"key": "shortener", "enabled": True})
    if not cfg:
        await edit_page(query, "LINK SHORTENER\nAdd a shortener provider to shorten links.", kb([
            [("✚ ADD", "settings:shortener_add")], [("《 BACK", "settings:home")],
        ]))
        return
    host = urlparse(cfg.get("url", "")).netloc or "Configured provider"
    token = cfg.get("token", "")
    masked = (token[:2] + "••••••" + token[-2:]) if len(token) > 4 else "••••••"
    await edit_page(query, f"LINK SHORTENER\nShortener: {host}\nShortener API: {masked}\nYou can now use /shortener to shorten links.", kb([
        [("REMOVE", "settings:shortener_remove")], [("《 BACK", "settings:home")],
    ]))

async def render_custom_buttons(query, db):
    cfg = await db.settings.find_one({"key": "custom_buttons"}) or {}
    buttons = cfg.get("buttons", [])[:2]
    rows = custom_button_settings_rows(buttons)
    if len(buttons) < 2:
        rows.append([InlineKeyboardButton(text="✚", callback_data="settings:custom_button_add")])
    rows.append([InlineKeyboardButton(text="《 BACK", callback_data="settings:home")])
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    await edit_page(query, "Custom Button:\n\nYou can add a custom buttons to your files.", markup)


async def render_mods(query, db):
    mods = await db.moderators.find({}).sort("user_id", 1).to_list(length=3)
    lines = [f"• {m.get('first_name') or ('User ' + str(m['user_id']))} — {m['user_id']}" for m in mods]
    rows = []
    if len(mods) < 3:
        rows.append([("✚ ADD", "settings:mod_add")])
    if mods:
        rows.append([("REMOVE", "settings:mod_remove")])
    rows.append([("《 BACK", "settings:home")])
    await edit_page(query, "MODERATORS\nModerators can manage bot features, including broadcasting.\n\nModerators List\n" + ("\n".join(lines) if lines else "No moderators added."), kb(rows))

@router.message(Command("settings"))
async def settings_command(message: Message, settings: Settings, database):
    if not await allowed(message, settings, database):
        return
    me = await message.bot.get_me()
    name = f'<a href="https://t.me/{me.username}">{__import__("html").escape(me.full_name)}</a>' if me.username else __import__("html").escape(me.full_name)
    await message.answer(f"<b><u>CUSTOMIZE</u></b>\n\n<i>✰ Name: {name}</i>\n\n<i>Configure Bot Settings Using the Buttons Below.</i>", parse_mode="HTML", link_preview_options={"is_disabled": True}, reply_markup=kb([
        [("START MSG", "settings:start"), ("FORCE JOIN", "settings:forcejoin")],
        [("ANIMATION", "settings:forcejoin_animation"), ("AUTO DELETE", "settings:auto")],
        [("INVALID", "settings:invalid"), ("SHORTENER", "settings:shortener")],
        [("MODERATORS", "settings:mods"), ("STATS", "settings:stats")],
        [("CUSTOM BUTTON", "settings:custom_button")],
    ]))
    # Keep the user's /settings command out of the visible panel history.
    try:
        await message.delete()
    except TelegramAPIError:
        pass

@router.callback_query(F.data.startswith("settings:"))
async def settings_callbacks(query: CallbackQuery, state: FSMContext, settings: Settings, database):
    if not query.from_user or not await is_owner_or_moderator(database.db, query.from_user.id, settings):
        await query.answer("Not authorized.", show_alert=True)
        return
    action = query.data.split(":", 1)[1]
    db = database.db
    pending = await state.get_data()
    # Leaving the chat-picker page must also dismiss its reply keyboard.
    if pending.get("field") == "forcejoin_wait_chat" and query.message:
        try:
            keyboard_removal = await query.message.answer(" ", reply_markup=ReplyKeyboardRemove())
            await keyboard_removal.delete()
        except TelegramAPIError:
            pass
    # Any inline navigation/control action supersedes the previous text-input
    # wait. Keep FSM data (some multi-step flows need it), but disarm the old
    # state before this callback optionally starts a new input flow.
    await state.set_state(None)
    # Every inline settings action removes tracked standalone prompts.
    await remove_temporary_prompts(query.bot, pending)
    await remove_input_prompt(query.bot, pending)
    await state.update_data(temporary_prompt_ids=[], input_prompt_chat_id=None, input_prompt_message_id=None)
    if action in {"home", "home_back", "start", "start_text", "start_button", "start_poster", "start_about",
                  "auto", "auto_time", "auto_message", "after", "after_text", "invalid", "invalid_text",
                  "invalid_button", "invalid_poster", "invalid_about", "forcejoin", "forcejoin_add",
                  "forcejoin_message", "forcejoin_media", "forcejoin_animation", "animation_mode",
                  "mods", "shortener", "custom_button"}:
        await clear_state_preserving_prompts(state)
    if action in {"home", "home_back"}: await render_home(query)
    elif action == "forcejoin": await render_forcejoin(query, db, settings)
    elif action == "forcejoin_toggle":
        cfg = await db.settings.find_one({"key": "force_join"}) or {}
        await patch(db, "force_join", {"enabled": not cfg.get("enabled", False)}, {"channels": [], "message": "Hey {mention}\nPlease Join My {remaining_count} Channel's To Use Me!"})
        await render_forcejoin(query, db, settings)
    elif action == "forcejoin_add":
        panel_message = await edit_page(query, "ADD FORCE JOIN\n\nChoose Group or Channel from the keyboard below. Public chats use Normal Mode automatically; private chats use Join Request Mode automatically.", kb([
            [("《 BACK", "settings:forcejoin")],
        ]))
        if panel_message:
            await state.update_data(forcejoin_panel_chat_id=panel_message.chat.id, forcejoin_panel_message_id=panel_message.message_id)
        if query.message:
            # Telegram requires a message to display a request-chat keyboard;
            # keep that carrier visually empty instead of adding another prompt.
            await tracked_answer(query.message, state,
                "\u2063",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[
                        [KeyboardButton(text="Group", request_chat=KeyboardButtonRequestChat(request_id=5101, chat_is_channel=False, request_title=True, request_username=True)),
                         KeyboardButton(text="Channel", request_chat=KeyboardButtonRequestChat(request_id=5102, chat_is_channel=True, request_title=True, request_username=True))],
                        [KeyboardButton(text="Cancel")],
                    ], resize_keyboard=True, one_time_keyboard=True,
                ),
            )
        await state.set_state(SettingInput.waiting)
        await state.update_data(field="forcejoin_wait_chat")
    elif action == "forcejoin_join_request":
        await state.set_state(SettingInput.waiting)
        await state.update_data(field="forcejoin_wait_chat")
        if query.message:
            await tracked_answer(query.message, state, 
                "Choose Group or Channel below.",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[
                        [KeyboardButton(text="Group", request_chat=KeyboardButtonRequestChat(request_id=5101, chat_is_channel=False, request_title=True, request_username=True))],
                        [KeyboardButton(text="Channel", request_chat=KeyboardButtonRequestChat(request_id=5102, chat_is_channel=True, request_title=True, request_username=True))],
                        [KeyboardButton(text="Cancel")],
                    ], resize_keyboard=True, one_time_keyboard=True,
                ),
            )
    elif action.startswith("forcejoin_channel:"):
        channels = await effective_force_join_channels(database, settings)
        index = int(action.split(":", 1)[1])
        if index < len(channels):
            channel = channels[index]
            cfg = await db.settings.find_one({"key": "force_join_channel", "channel": channel}) or {}
            channel_title = cfg.get("channel_title") or channel.lstrip("@")
            await edit_page(query, f"CHANNEL SETTINGS\n\n{channel_title}\n\nSelect an option below.", kb([
                [("BUTTON TEXT", f"settings:forcejoin_label:{index}"), ("REMOVE", f"settings:forcejoin_remove_one:{index}")],
                [("《 BACK", "settings:forcejoin")],
            ]))
    elif action.startswith("forcejoin_label:"):
        channels = await effective_force_join_channels(database, settings)
        index = int(action.split(":", 1)[1])
        if index < len(channels):
            await state.set_state(SettingInput.waiting); await state.update_data(field="forcejoin_label", channel=channels[index])
            if query.message: await tracked_answer(query.message, state, "Send new button text for force-join channel button.")
    elif action.startswith("forcejoin_remove_one:"):
        channels = await effective_force_join_channels(database, settings)
        index = int(action.split(":", 1)[1])
        if index < len(channels):
            channel = channels.pop(index)
            await patch(db, "force_join", {"channels": channels, "channels_override": True})
            await db.settings.delete_one({"key": "force_join_channel", "channel": channel})
        await render_forcejoin(query, db, settings)
    elif action == "forcejoin_message": await render_forcejoin_message(query)
    elif action == "forcejoin_message_edit":
        await show_input_prompt(query, state, "forcejoin_message",
            "Send the Force Join message. Telegram bold, italic, quotes and monospace formatting will be preserved.\n\n<code>{bot_mention}</code> — mention your bot\n<code>{mention}</code> — mention the user\n<code>{owner}</code> — owner link\n<code>{hindi_anime}</code> — Lexi Anime Hindi link\n<code>{remaining_count}</code> — remaining channels count",
            "forcejoin")
    elif action == "forcejoin_message_see":
        cfg = await db.settings.find_one({"key": "force_join"}) or {}
        me = await query.bot.get_me()
        preview = str(cfg.get("message") or DEFAULT_FORCE_JOIN_MESSAGE)
        user_label = query.from_user.full_name if query.from_user else "User"
        for token, replacement in {
            "{bot_mention}": me.full_name or me.username or "Bot",
            "{mention}": user_label, "{user_mention}": user_label, "{user}": user_label,
            "{owner}": "Lexi", "{hindi_anime}": "Lexi Anime Hindi", "{remaining_count}": "3",
        }.items():
            preview = preview.replace(token, replacement)
        await query.answer(plain_preview(preview)[:190], show_alert=True)
        return
    elif action == "forcejoin_message_default":
        await patch(db, "force_join", {"message": DEFAULT_FORCE_JOIN_MESSAGE}, {"enabled": False, "channels": [], })
        me = await query.bot.get_me()
        preview = DEFAULT_FORCE_JOIN_MESSAGE.replace("{mention}", query.from_user.full_name if query.from_user else "User").replace("{remaining_count}", "3")
        preview = preview.replace("{bot_mention}", me.full_name or me.username or "Bot")
        await query.answer("✅ Force Join message restored to the recommended default.\n\n" + plain_preview(preview)[:130], show_alert=True)
        await render_forcejoin_message(query)
        return
    elif action == "forcejoin_media":
        await edit_page(query, "FORCE JOIN MEDIA\n\nAdd or remove the image/GIF shown with the Force Join message.", kb([
            [("✚ ADD", "settings:forcejoin_media_add"), ("REMOVE", "settings:forcejoin_media_remove")],
            [("《 BACK", "settings:forcejoin")],
        ]))
    elif action == "forcejoin_media_add":
        await state.set_state(SettingInput.waiting); await state.update_data(field="forcejoin_media")
        if query.message: await tracked_answer(query.message, state, "Only GIFs and images are supported. Send the new force-join poster here.")
    elif action == "forcejoin_media_remove":
        await db.settings.update_one({"key": "force_join"}, {"$unset": {"poster_file_id": "", "poster_photo_id": ""}}, upsert=True)
        if query.message: await tracked_answer(query.message, state, "✨ Force-join poster removed successfully.")
        await render_forcejoin(query, db, settings)
    elif action == "forcejoin_verify_text":
        await state.set_state(SettingInput.waiting); await state.update_data(field="forcejoin_verify_text")
        if query.message: await tracked_answer(query.message, state, "Send your new verification button text.")
    elif action == "forcejoin_remove":
        channels = await effective_force_join_channels(database, settings)
        rows = [[(channel, f"settings:forcejoin_del:{i}")] for i, channel in enumerate(channels)]
        rows.append([("《 BACK", "settings:forcejoin")])
        await edit_page(query, "REMOVE FORCE JOIN CHANNEL\nSelect a channel to remove.", kb(rows))
    elif action.startswith("forcejoin_del:"):
        channels = await effective_force_join_channels(database, settings)
        index = int(action.split(":", 1)[1])
        if 0 <= index < len(channels):
            channels.pop(index)
            await patch(db, "force_join", {"channels": channels, "channels_override": True})
        await render_forcejoin(query, db, settings)
    elif action == "custom_button": await render_custom_buttons(query, db)
    elif action == "custom_button_add":
        cfg = await db.settings.find_one({"key": "custom_buttons"}) or {}
        if len(cfg.get("buttons", [])) >= 2:
            await query.answer("Maximum 2 custom buttons allowed.", show_alert=True)
            await render_custom_buttons(query, db)
            return
        await show_input_prompt(
            query, state, "custom_button_text",
            "Send the custom button text.", "custom_button",
            custom_panel_chat_id=query.message.chat.id if query.message else None,
            custom_panel_message_id=query.message.message_id if query.message else None,
        )
    elif action.startswith("custom_button_remove:"):
        index = int(action.rsplit(":", 1)[1])
        cfg = await db.settings.find_one({"key": "custom_buttons"}) or {}
        buttons = list(cfg.get("buttons", []))
        if 0 <= index < len(buttons):
            buttons.pop(index)
            await patch(db, "custom_buttons", {"buttons": buttons})
        await render_custom_buttons(query, db)
    elif action == "custom_button_unused":
        await query.answer("This button is a preview of the saved label.", show_alert=False)
    elif action == "start": await render_start(query, db)
    elif action == "start_toggle":
        cfg = await db.settings.find_one({"key": "start_message"}) or {}
        await patch(db, "start_message", {"enabled": not cfg.get("enabled", True)}, {"text": DEFAULT_START_TEXT})
        await render_start(query, db)
    elif action == "start_text": await render_start_text(query)
    elif action == "start_text_edit":
        await show_input_prompt(query, state, "start_text",
            "Send the new Start Message text.\n\n<code>{bot_mention}</code> — mention your bot\n<code>{mention}</code> — mention the user\n<code>{owner}</code> — link to the owner\n<code>{hindi_anime}</code> — link to Lexi Anime Hindi",
            "start_text")
    elif action == "start_text_see":
        import re
        from html import unescape
        cfg = await db.settings.find_one({"key": "start_message"}) or {}
        preview = normalize_telegram_text(cfg.get("text") or DEFAULT_START_TEXT)
        me = await query.bot.get_me()
        bot_label = me.full_name or me.username or "Bot"
        user_label = query.from_user.full_name or query.from_user.username or "User"
        values = {
            "{bot_mention}": bot_label, "{bot}": bot_label,
            "{mention}": user_label, "{user_mention}": user_label, "{user}": user_label,
            "{owner}": "Lexi", "{hindi_anime}": "Lexi Anime Hindi", "{anime}": "Lexi Anime",
        }
        for token, replacement in values.items():
            preview = preview.replace(token, replacement)
        preview = plain_preview(preview)
        preview = re.sub(r"[*_`]", "", preview)
        await query.answer(preview[:190], show_alert=True)
        return
    elif action == "start_text_default":
        await patch(db, "start_message", {"text": DEFAULT_START_TEXT}, {"enabled": True})
        preview = plain_preview(DEFAULT_START_TEXT).replace("{user_mention}", query.from_user.full_name if query.from_user else "User")
        await query.answer("✅ Start message restored to the recommended default.\n\n" + preview[:130], show_alert=True)
        await render_start_text(query)
        return
    elif action == "start_button":
        await state.update_data(button_panel_kind="start")
        panel = await render_start_button(query, db)
        if panel:
            await state.update_data(button_panel_chat_id=panel.chat.id, button_panel_message_id=panel.message_id)
    elif action.startswith("start_button_toggle:"):
        index = action.rsplit(":", 1)[1]
        cfg = await db.settings.find_one({"key": "start_message"}) or {}
        key = f"button{index}_enabled"
        enabled = cfg.get(key, cfg.get("button_enabled", True) if index == "1" else False)
        text_value = cfg.get(f"button{index}_text", cfg.get("button_text", "") if index == "1" else "")
        url_value = cfg.get(f"button{index}_url", cfg.get("button_url", "") if index == "1" else "")
        if not enabled and not (text_value and url_value):
            await query.answer("Set this button's text and link before enabling it.", show_alert=True)
            await render_start_button(query, db)
        else:
            await patch(db, "start_message", {key: not enabled}, {"enabled": True, "text": DEFAULT_START_TEXT})
            await render_start_button(query, db)
    elif action in ("start_button_1", "start_button_2"):
        index = action.rsplit("_", 1)[1]
        await state.set_state(SettingInput.waiting); await state.update_data(field=f"start_button_{index}_text", button_index=index, panel_chat_id=query.message.chat.id if query.message else None, panel_message_id=query.message.message_id if query.message else None, panel_kind="start")
        if query.message: await tracked_answer(query.message, state, f"Send text for Start Button {index}.")
    elif action in ("start_button_text", "start_button_link"):
        await state.set_state(SettingInput.waiting); await state.update_data(field="start_button_1_text" if action == "start_button_text" else "start_button_1_link", button_index="1", panel_chat_id=query.message.chat.id if query.message else None, panel_message_id=query.message.message_id if query.message else None, panel_kind="start")
        if query.message: await tracked_answer(query.message, state, "Send text for Start Button 1." if action == "start_button_text" else "Now send the URL for Start Button 1.")
    elif action == "start_poster": await render_start_poster(query)
    elif action == "start_poster_add":
        await state.set_state(SettingInput.waiting); await state.update_data(field="start_poster")
        if query.message: await tracked_answer(query.message, state, "Only GIFs and images are supported. Send the new start message poster here.")
    elif action == "start_poster_remove":
        await db.settings.update_one({"key": "start_message"}, {"$unset": {"poster_file_id": "", "poster_photo_id": ""}}, upsert=True)
        if query.message: await tracked_answer(query.message, state, "✨ Successfully removed your bot start poster.")
        await render_start_poster(query)
    elif action == "start_about":
        await render_start_about(query, db)
    elif action == "start_about_toggle":
        cfg = await db.settings.find_one({"key": "start_message"}) or {}
        await patch(db, "start_message", {"about_enabled": not cfg.get("about_enabled", True)}, {"enabled": True, "text": DEFAULT_START_TEXT})
        await render_start(query, db)
    elif action == "start_about_text":
        await render_start(query, db)
    elif action == "auto": await render_auto(query, db)
    elif action == "auto_toggle":
        cfg = await db.settings.find_one({"key": "auto_delete"}) or {}
        turning_on = not cfg.get("enabled", False)
        await patch(db, "auto_delete", {"enabled": turning_on}, {"minutes": 30, "notice": DEFAULT_NOTICE})
        if not turning_on:
            await patch(db, "after_delete", {"enabled": False}, {"text": DEFAULT_AFTER_TEXT, "button_text": DEFAULT_AFTER_BUTTON})
            await db.tasks.update_many(
                {"kind": "delete_delivery", "status": {"$in": ["pending", "processing"]}},
                {"$set": {"status": "cancelled", "cancelled_at": datetime.now(timezone.utc)}},
            )
        await render_auto(query, db)
    elif action == "auto_time": await render_auto_time(query, db)
    elif action == "auto_time_change":
        cfg = await db.settings.find_one({"key": "auto_delete"}) or {}
        current = int(cfg.get("minutes", 30))
        next_value = AUTO_DELETE_MINUTES[(AUTO_DELETE_MINUTES.index(current) + 1) % len(AUTO_DELETE_MINUTES)] if current in AUTO_DELETE_MINUTES else 2
        await patch(db, "auto_delete", {"minutes": next_value}, {"enabled": False, "notice": DEFAULT_NOTICE})
        await render_auto_time(query, db)
    elif action == "auto_message": await render_auto_message(query, db)
    elif action == "auto_message_edit":
        await state.set_state(SettingInput.waiting); await state.update_data(field="auto_notice")
        if query.message: await tracked_answer(query.message, state, "Send your new auto-delete message. Use {auto_delete_time} to insert the timer.")
    elif action == "auto_message_see":
        import re
        from html import unescape
        cfg = await db.settings.find_one({"key": "auto_delete"}) or {}
        from app.services.delivery import format_notice
        saved, _, is_custom = format_notice(cfg, int(cfg.get("minutes", 30)), default_notice=DEFAULT_NOTICE)
        # Match Start Message SEE: Telegram popup alerts are plain text and
        # have a short length limit, so strip formatting tags and show preview there.
        preview = unescape(re.sub(r"</?[^>]+>", "", str(saved)))
        await query.answer(preview[:190], show_alert=True)
        return
    elif action == "auto_message_default":
        await patch(db, "auto_delete", {"notice": DEFAULT_NOTICE}, {"enabled": False, "minutes": 30})
        await db.settings.update_one({"key": "auto_delete"}, {"$unset": {"notice_entities": ""}})
        cfg = await db.settings.find_one({"key": "auto_delete"}) or {}
        import re
        from html import unescape
        default_text = DEFAULT_NOTICE.replace("{auto_delete_time}", time_words(int(cfg.get("minutes", 30))))
        preview = unescape(re.sub(r"</?[^>]+>", "", default_text))
        # Match Start Message DEFAULT: show the restored default in the popup.
        await query.answer("✅ Default successfully set!\n\n" + preview[:145], show_alert=True)
        await render_auto_message(query, db)
        return
    elif action == "after": await render_after(query, db)
    elif action == "after_toggle":
        auto = await db.settings.find_one({"key": "auto_delete", "enabled": True})
        if not auto:
            await patch(db, "after_delete", {"enabled": False}, {"text": DEFAULT_AFTER_TEXT, "button_text": DEFAULT_AFTER_BUTTON})
            if query.message: await tracked_answer(query.message, state, "After Delete only works when Auto Delete is enabled.")
            await render_after(query, db)
            return
        cfg = await db.settings.find_one({"key": "after_delete"}) or {}
        await patch(db, "after_delete", {"enabled": not cfg.get("enabled", False)}, {"text": DEFAULT_AFTER_TEXT, "button_text": DEFAULT_AFTER_BUTTON})
        await render_after(query, db)
    elif action == "after_text": await render_after_text(query)
    elif action in ("after_text_message", "after_text_button"):
        await state.set_state(SettingInput.waiting); await state.update_data(field=action)
        if query.message: await tracked_answer(query.message, state, "Send the new after-delete message text." if action == "after_text_message" else "Send new button text for the after-delete message.")
    elif action == "invalid": await render_invalid(query, db)
    elif action == "invalid_toggle":
        cfg = await db.settings.find_one({"key": "invalid_post"}) or {}
        await patch(db, "invalid_post", {"enabled": not cfg.get("enabled", False)}, {"text": DEFAULT_INVALID, "about_enabled": False})
        await render_invalid(query, db)
    elif action == "invalid_text": await render_invalid_text(query)
    elif action == "invalid_text_edit":
        await show_input_prompt(query, state, "invalid_text",
            "Send the new Invalid Message text. Telegram bold, italic, quotes and monospace formatting will be preserved.\n\n<code>{bot_mention}</code> — mention your bot\n<code>{mention}</code> — mention the user\n<code>{owner}</code> — owner link\n<code>{hindi_anime}</code> — Lexi Anime Hindi link",
            "invalid")
    elif action == "invalid_text_see":
        cfg = await db.settings.find_one({"key": "invalid_post"}) or {}
        me = await query.bot.get_me()
        preview = str(cfg.get("text") or DEFAULT_INVALID)
        user_label = query.from_user.full_name if query.from_user else "User"
        for token, replacement in {
            "{bot_mention}": me.full_name or me.username or "Bot",
            "{mention}": user_label, "{user_mention}": user_label, "{user}": user_label,
            "{owner}": "Lexi", "{hindi_anime}": "Lexi Anime Hindi",
        }.items():
            preview = preview.replace(token, replacement)
        await query.answer(plain_preview(preview)[:190], show_alert=True)
        return
    elif action == "invalid_text_default":
        await patch(db, "invalid_post", {"text": DEFAULT_INVALID}, {"enabled": False, "about_enabled": False})
        me = await query.bot.get_me()
        preview = DEFAULT_INVALID.replace("{mention}", query.from_user.full_name if query.from_user else "User")
        preview = preview.replace("{bot_mention}", me.full_name or me.username or "Bot")
        await query.answer("✅ Invalid-message text restored to the recommended default.\n\n" + plain_preview(preview)[:130], show_alert=True)
        await render_invalid_text(query)
        return
    elif action == "invalid_button":
        await state.update_data(button_panel_kind="invalid")
        panel = await render_invalid_button(query, db)
        if panel:
            await state.update_data(button_panel_chat_id=panel.chat.id, button_panel_message_id=panel.message_id)
    elif action.startswith("invalid_button_toggle:"):
        index = action.rsplit(":", 1)[1]
        cfg = await db.settings.find_one({"key": "invalid_post"}) or {}
        key = f"button{index}_enabled"
        enabled = cfg.get(key, bool(cfg.get("button_text") and cfg.get("button_url")) if index == "1" else False)
        text_value = cfg.get(f"button{index}_text", cfg.get("button_text", "") if index == "1" else "")
        url_value = cfg.get(f"button{index}_url", cfg.get("button_url", "") if index == "1" else "")
        if not enabled and not (text_value and url_value):
            await query.answer("Set this button's text and link before enabling it.", show_alert=True)
            await render_invalid_button(query, db)
        else:
            await patch(db, "invalid_post", {key: not enabled}, {"enabled": False})
            await render_invalid_button(query, db)
    elif action in ("invalid_button_1", "invalid_button_2"):
        index = action.rsplit("_", 1)[1]
        await state.set_state(SettingInput.waiting); await state.update_data(field=f"invalid_button_{index}_text", button_index=index, panel_chat_id=query.message.chat.id if query.message else None, panel_message_id=query.message.message_id if query.message else None, panel_kind="invalid")
        if query.message: await tracked_answer(query.message, state, f"Send text for Invalid Button {index}.")
    elif action in ("invalid_button_text", "invalid_button_link"):
        await state.set_state(SettingInput.waiting); await state.update_data(field="invalid_button_1_text" if action == "invalid_button_text" else "invalid_button_1_link", button_index="1", panel_chat_id=query.message.chat.id if query.message else None, panel_message_id=query.message.message_id if query.message else None, panel_kind="invalid")
        if query.message: await tracked_answer(query.message, state, "Send text for Invalid Button 1." if action == "invalid_button_text" else "Now send the URL for Invalid Button 1.")
    elif action == "invalid_poster": await render_poster(query)
    elif action == "poster_add":
        await state.set_state(SettingInput.waiting); await state.update_data(field="poster")
        if query.message: await tracked_answer(query.message, state, "Only GIFs and images are supported. Send the new invalid-post poster here.")
    elif action == "poster_remove":
        await db.settings.update_one({"key": "invalid_post"}, {"$unset": {"poster_file_id": "", "poster_photo_id": ""}}, upsert=True)
        if query.message: await tracked_answer(query.message, state, "✨ Invalid-post poster removed successfully.")
        await render_poster(query)
    elif action == "invalid_about": await render_about(query, db)
    elif action == "invalid_about_toggle":
        cfg = await db.settings.find_one({"key": "invalid_post"}) or {}
        await patch(db, "invalid_post", {"about_enabled": not cfg.get("about_enabled", False)}, {"enabled": False})
        await render_invalid(query, db)
    elif action == "forcejoin_animation":
        await render_animation(query, db)
    elif action == "animation_prefix":
        await state.set_state(SettingInput.waiting); await state.update_data(field="animation_prefix")
        if query.message: await tracked_answer(query.message, state, "Send new prefix text for the loop animation.")
    elif action == "animation_frames":
        await state.set_state(SettingInput.waiting); await state.update_data(field="animation_frame_1", pending_frames=[])
        if query.message: await tracked_answer(query.message, state, "Send the first frame for the loop animation.")
    elif action == "animation_toggle":
        cfg = await db.settings.find_one({"key": "animation"}) or {}
        await patch(db, "animation", {"enabled": not cfg.get("enabled", False)}, {"prefix": "Checking", "frames": [".", "..", "..."], "speed": 0.25, "mode": "force_join_poster"})
        await render_animation(query, db)
    elif action == "animation_speed":
        cfg = await db.settings.find_one({"key": "animation"}) or {}
        speed = float(cfg.get("speed", 1.0))
        speed = min(1.0, max(0.25, speed))
        next_speed = round(0.25 + ((round((speed - 0.25) / 0.05) + 1) % 16) * 0.05, 2)
        await patch(db, "animation", {"speed": next_speed}, {"enabled": False, "prefix": "Checking", "frames": [".", "..", "..."], "mode": "force_join_poster"})
        await render_animation(query, db)
    elif action == "animation_mode_toggle":
        cfg = await db.settings.find_one({"key": "animation"}) or {}
        current = cfg.get("mode", "force_join_poster")
        await patch(db, "animation", {"mode": "new_message" if current != "new_message" else "force_join_poster"}, {"enabled": False, "prefix": "Checking", "frames": [".", "..", "..."], "speed": 0.25})
        await render_animation(query, db)
    elif action == "animation_mode":
        cfg = await db.settings.find_one({"key": "animation"}) or {}
        await edit_page(query, f"MODE\n\nNew Message: Run the animation in a separate message.\n\nForce Join Poster: Run the animation on the force-join poster.\n\nCurrent mode: {cfg.get('mode', 'force_join_poster')}", kb([
            [("NEW MESSAGE", "settings:animation_mode_new"), ("FORCE JOIN POSTER", "settings:animation_mode_poster")],
            [("《 BACK", "settings:forcejoin_animation")],
        ]))
    elif action in ("animation_mode_new", "animation_mode_poster"):
        mode = _animation_mode_for_action(action)
        await patch(db, "animation", {"mode": mode}, {"enabled": False, "prefix": "Checking", "frames": [".", "..", "..."], "speed": 0.25})
        await render_animation(query, db)
    elif action == "shortener": await render_shortener(query, db)
    elif action == "shortener_add":
        await state.set_state(SettingInput.waiting); await state.update_data(field="shortener_url")
        if query.message: await tracked_answer(query.message, state, "Send your shortener website URL here (for example https://gplinks.com). Compatible provider API endpoints are also accepted.")
    elif action == "shortener_remove":
        await db.settings.update_one({"key": "shortener"}, {"$set": {"enabled": False}, "$unset": {"token": ""}})
        await render_shortener(query, db)
    elif action == "mods": await render_mods(query, db)
    elif action == "mod_add":
        if query.from_user.id != settings.owner_id:
            await query.answer("Only the owner can add moderators.", show_alert=True); return
        if await db.moderators.count_documents({}) >= 3:
            await query.answer("Maximum 3 moderators allowed.", show_alert=True); return
        await state.set_state(SettingInput.waiting); await state.update_data(field="mod_id")
        if query.message: await tracked_answer(query.message, state, "Please send the Telegram user ID of the new moderator.")
    elif action == "mod_remove":
        if query.from_user.id != settings.owner_id:
            await query.answer("Only the owner can remove moderators.", show_alert=True); return
        mods = await db.moderators.find({}).sort("user_id", 1).to_list(length=3)
        rows = [[(m.get("first_name") or f"User {m['user_id']}", f"settings:mod_del:{m['user_id']}")] for m in mods]
        rows.append([("《 BACK", "settings:mods")])
        await edit_page(query, "REMOVE MODERATOR\nSelect the moderator you want to remove.", kb(rows))
    elif action.startswith("mod_del:"):
        if query.from_user.id != settings.owner_id:
            await query.answer("Only the owner can remove moderators.", show_alert=True); return
        uid = int(action.split(":", 1)[1])
        result = await db.moderators.delete_one({"user_id": uid})
        if query.message: await tracked_answer(query.message, state, "✨ Successfully removed moderator." if result.deleted_count else "Moderator not found.")
        await render_mods(query, db)
    elif action == "stats":
        total = await db.users.count_documents({})
        blocked = await db.users.count_documents({"blocked": True})
        await query.answer(f"⛦ Total Users: {total}\n⛦ Banned Users: {blocked}", show_alert=True)
        return
    await query.answer()


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def handle_button_settings_reply_keyboard(message: Message, state: FSMContext, settings: Settings, database):
    if not message.from_user or not await is_owner_or_moderator(database.db, message.from_user.id, settings):
        raise SkipHandler
    value = (message.text or "").strip()
    data = await state.get_data()
    kind = data.get("button_panel_kind")
    if value == "《 BACK" and kind in ("start", "invalid"):
        await remove_temporary_prompts(message.bot, data)
        await clear_state_preserving_prompts(state)
        # Return directly to the parent settings page while removing the reply keyboard.
        if kind == "start":
            parent_text = format_settings_page("START MESSAGE\nCustomize your bot's start message using the following options.")
            parent_kb = kb([
                [("DISABLE/ENABLE", "settings:start_toggle")], [("BUTTON", "settings:start_button"), ("START TEXT", "settings:start_text")],
                [("START POSTER", "settings:start_poster"), ("ABOUT BUTTON", "settings:start_about")], [("《 BACK", "settings:home")],
            ])
        else:
            parent_text = format_settings_page("INVALID POST\nSet a response for users who send normal messages to the bot.")
            parent_kb = kb([
                [("DISABLE/ENABLE", "settings:invalid_toggle")], [("TEXT", "settings:invalid_text"), ("BUTTON", "settings:invalid_button")],
                [("POSTER", "settings:invalid_poster"), ("ABOUT", "settings:invalid_about_toggle")], [("《 BACK", "settings:home")],
            ])
        parent = await message.answer(parent_text, parse_mode="HTML", reply_markup=ReplyKeyboardRemove(), link_preview_options={"is_disabled": True})
        try:
            await parent.edit_reply_markup(reply_markup=parent_kb)
        except TelegramAPIError:
            pass
        return
    if kind not in ("start", "invalid"):
        raise SkipHandler
    db = database.db
    is_start = kind == "start"
    cfg_key = "start_message" if is_start else "invalid_post"
    if value in ("1 ON ✅", "1 OFF ❌", "2 ON ✅", "2 OFF ❌"):
        index = value[0]
        cfg = await db.settings.find_one({"key": cfg_key}) or {}
        key = f"button{index}_enabled"
        enabled = cfg.get(key, cfg.get("button_enabled", True) if index == "1" and is_start else bool(cfg.get("button_text") and cfg.get("button_url")) if index == "1" else False)
        text_value = cfg.get(f"button{index}_text", cfg.get("button_text", "") if index == "1" else "")
        url_value = cfg.get(f"button{index}_url", cfg.get("button_url", "") if index == "1" else "")
        if not enabled and not (text_value and url_value):
            await tracked_answer(message, state, "Set this button's text and link before enabling it.")
        else:
            await patch(db, cfg_key, {key: not enabled}, {"enabled": True, "text": DEFAULT_START_TEXT} if is_start else {"enabled": False})
        panel_data = await state.get_data()
        await send_button_settings_reply_panel(message, db, kind, panel_data.get("button_panel_chat_id"), panel_data.get("button_panel_message_id"))
        return
    if value in ("1 BUTTON", "2 BUTTON"):
        index = value[0]
        await state.set_state(SettingInput.waiting)
        await state.update_data(field=f"{kind}_button_{index}_text", button_index=index, panel_kind=kind)
        await tracked_answer(message, state, f"Send text for {'Start' if is_start else 'Invalid'} Button {index}.")
        return

async def send_button_settings_reply_panel(message: Message, db, kind: str, panel_chat_id=None, panel_message_id=None):
    is_start = kind == "start"
    cfg = await db.settings.find_one({"key": "start_message" if is_start else "invalid_post"}) or {}
    status1 = "ON ✅" if cfg.get("button1_enabled", cfg.get("button_enabled", True) if is_start else bool(cfg.get("button_text") and cfg.get("button_url"))) else "OFF ❌"
    status2 = "ON ✅" if cfg.get("button2_enabled", False) else "OFF ❌"
    title = "START MESSAGE BUTTONS" if is_start else "INVALID POST BUTTONS"
    body = "Customize buttons shown with your start message." if is_start else "Customize buttons shown with invalid posts."
    markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=f"1 {status1}"), KeyboardButton(text=f"2 {status2}")], [KeyboardButton(text="1 BUTTON"), KeyboardButton(text="2 BUTTON")], [KeyboardButton(text="《 BACK")]], resize_keyboard=True)
    page_text = format_settings_page(f"{title}\n{body}\n\nButton 1: {status1}\nButton 2: {status2}")
    edited = False
    if panel_chat_id and panel_message_id:
        try:
            await message.bot.edit_message_text(chat_id=panel_chat_id, message_id=panel_message_id, text=page_text, parse_mode="HTML")
            edited = True
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                edited = True
        except TelegramAPIError:
            pass
    if not edited:
        await message.answer(page_text, parse_mode="HTML", reply_markup=markup)

@router.message(SettingInput.waiting, Command("cancel"))
async def cancel_setting_input(message: Message, state: FSMContext):
    pending = await state.get_data()
    back_action = pending.get("input_back_action") or "home"
    await remove_temporary_prompts(message.bot, pending)
    await remove_input_prompt(message.bot, pending)
    await clear_state_preserving_prompts(state)
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    # Never leave the user at a dead end after cancelling an input prompt.
    await tracked_answer(message, state, 
        "Input cancelled. Use Back to return to settings.",
        reply_markup=kb([[("《 BACK", f"settings:{back_action}")]]),
    )

@router.message(SettingInput.waiting)
async def receive_setting_input(message: Message, state: FSMContext, settings: Settings, database):
    if message.text and message.text.startswith("/"):
        raise SkipHandler
    if not message.from_user or not await is_owner_or_moderator(database.db, message.from_user.id, settings):
        return
    data = await state.get_data()
    field = data.get("field")
    db = database.db
    value = (message.text or "").strip()

    button_label_fields = {
        "custom_button_text", "forcejoin_label", "forcejoin_verify_text",
        "start_button_text", "invalid_button_text", "after_text_button",
    }
    dynamic_button_label = (
        isinstance(field, str)
        and (field.startswith("start_button_") or field.startswith("invalid_button_"))
        and field.endswith("_text")
    )
    if field in button_label_fields or dynamic_button_label:
        if not valid_button_text(value):
            await tracked_answer(message, state, "Button text must be between 1 and 64 characters.")
            return

    if field in {"forcejoin_wait_chat", "forcejoin_wait_mode"} and value.casefold() == "cancel":
        pending = await state.get_data()
        panel_chat_id, panel_message_id = pending.get("forcejoin_panel_chat_id"), pending.get("forcejoin_panel_message_id")
        await remove_temporary_prompts(message.bot, pending)
        await remove_input_prompt(message.bot, pending)
        await clear_state_preserving_prompts(state)
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        # Remove the reply keyboard without leaving a cancellation prompt behind.
        try:
            keyboard_removal = await message.answer(" ", reply_markup=ReplyKeyboardRemove())
            await keyboard_removal.delete()
        except TelegramAPIError:
            pass
        if panel_chat_id and panel_message_id:
            panel_text, panel_markup = await build_forcejoin_page(message.bot, db, settings)
            try:
                # The Add page is the existing settings panel: turn it back into
                # the Force Join page so the newly added chat appears immediately.
                await message.bot.edit_message_text(
                    chat_id=panel_chat_id,
                    message_id=panel_message_id,
                    text=format_settings_page(panel_text),
                    parse_mode="HTML",
                    reply_markup=panel_markup,
                    link_preview_options={"is_disabled": True},
                )
            except TelegramAPIError:
                # If Telegram refuses the edit, replace the Add page rather than
                # leaving the user stranded on it.
                try:
                    replacement = await message.bot.send_message(
                        chat_id=panel_chat_id,
                        text=format_settings_page(panel_text),
                        parse_mode="HTML",
                        reply_markup=panel_markup,
                        link_preview_options={"is_disabled": True},
                    )
                    try:
                        await message.bot.delete_message(chat_id=panel_chat_id, message_id=panel_message_id)
                    except TelegramAPIError:
                        pass
                except TelegramAPIError:
                    pass
        return

    if field == "forcejoin_wait_chat":
        shared = getattr(message, "chat_shared", None)
        if not shared:
            await tracked_answer(message, state, "Please use the Group or Channel buttons to select a chat, or tap Cancel.")
            return
        # Telegram's chat-shared update appears as a user-side message; remove it
        # immediately so the “You shared … with …” line does not remain in chat.
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        if shared.request_id not in (5101, 5102):
            await tracked_answer(message, state, "That chat selection expired. Open /settings → Force Join → Add again.")
            await clear_state_preserving_prompts(state)
            return
        expected_type = "channel" if shared.request_id == 5102 else "group"
        try:
            chat = await message.bot.get_chat(shared.chat_id)
            allowed_types = {"channel"} if expected_type == "channel" else {"group", "supergroup"}
            if chat.type not in allowed_types:
                await tracked_answer(message, state, "Please select the matching Group or Channel type.")
                return
            me = await message.bot.get_me()
            bot_member = await message.bot.get_chat_member(chat.id, me.id)
            if bot_member.status not in ("administrator", "creator"):
                await tracked_answer(message, state, "The bot is not an administrator in this chat. Add/promote the bot as admin, then try again.")
                await clear_state_preserving_prompts(state)
                return
            # Public chats use their public t.me link and do not need a new invite link.
            # Private chats need invite-link permission because they are added in Join Request mode.
            if not chat.username and bot_member.status != "creator" and not getattr(bot_member, "can_invite_users", False):
                await tracked_answer(message, state, "The bot is admin but lacks permission to create an approval-required invite link. Enable invite-link permission and try again.")
                await clear_state_preserving_prompts(state)
                return
        except TelegramAPIError:
            await tracked_answer(message, state, "The bot cannot access this chat. Make sure it is a chat where the bot is an administrator.")
            await clear_state_preserving_prompts(state)
            return
        await state.update_data(
            pending_chat_id=chat.id,
            pending_chat_title=chat.title or str(chat.id),
            pending_chat_type=chat.type,
            pending_chat_username=chat.username,
        )
        data = await state.get_data()
        channels = await effective_force_join_channels(database, settings)
        channel_id = str(chat.id)
        if channel_id in channels or (chat.username and "@" + chat.username in channels):
            await remove_temporary_prompts(message.bot, data)
            await clear_state_preserving_prompts(state)
            await tracked_answer(message, state, "That Force Join chat is already configured.", reply_markup=ReplyKeyboardRemove())
            return
        if len(channels) >= 10:
            await remove_temporary_prompts(message.bot, data)
            await clear_state_preserving_prompts(state)
            await tracked_answer(message, state, "Maximum 10 Force Join channels/groups are allowed.", reply_markup=ReplyKeyboardRemove())
            return
        mode = force_join_mode_for_chat(chat)
        link = f"https://t.me/{chat.username}" if chat.username else None
        if mode == "join_request":
            try:
                invite = await message.bot.create_chat_invite_link(chat_id=chat.id, creates_join_request=True, name="FB Force Join - Request")
                link = invite.invite_link
            except TelegramAPIError:
                await remove_temporary_prompts(message.bot, data)
                await clear_state_preserving_prompts(state)
                await tracked_answer(message, state, "Telegram could not create an approval-required invite link. Check the bot's admin status and invite-link permission.", reply_markup=ReplyKeyboardRemove())
                return
        channels.append(channel_id)
        await patch(db, "force_join", {"channels": channels, "channels_override": True})
        await db.settings.update_one({"key": "force_join_channel", "channel": channel_id},
            {"$set": {"key": "force_join_channel", "channel": channel_id, "button_url": link,
                      "channel_title": chat.title or str(chat.id), "chat_type": chat.type, "mode": mode}}, upsert=True)
        panel_chat_id, panel_message_id = data.get("forcejoin_panel_chat_id"), data.get("forcejoin_panel_message_id")
        await remove_temporary_prompts(message.bot, data)
        await remove_input_prompt(message.bot, data)
        await clear_state_preserving_prompts(state)
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        # Hide the chat-picker keyboard without leaving a new prompt in the chat.
        try:
            keyboard_removal = await tracked_answer(message, state, " ", reply_markup=ReplyKeyboardRemove())
            await keyboard_removal.delete()
        except TelegramAPIError:
            pass
        # Delete the selected-chat message too; all picker prompts are removed above.
        # Refresh the original Add panel in place. If Telegram rejects the edit,
        # send the main panel as a fallback and remove the stale Add panel.
        if panel_chat_id and panel_message_id:
            panel_text, panel_markup = await build_forcejoin_page(message.bot, db, settings)
            try:
                await message.bot.edit_message_text(
                    chat_id=panel_chat_id, message_id=panel_message_id,
                    text=format_settings_page(panel_text), parse_mode="HTML",
                    reply_markup=panel_markup, link_preview_options={"is_disabled": True},
                )
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    try:
                        replacement = await message.bot.send_message(
                            chat_id=panel_chat_id, text=format_settings_page(panel_text),
                            parse_mode="HTML", reply_markup=panel_markup,
                            link_preview_options={"is_disabled": True},
                        )
                    except TelegramAPIError:
                        replacement = None
                    if replacement:
                        try:
                            await message.bot.delete_message(chat_id=panel_chat_id, message_id=panel_message_id)
                        except TelegramAPIError:
                            pass
            except TelegramAPIError:
                try:
                    replacement = await message.bot.send_message(
                        chat_id=panel_chat_id, text=format_settings_page(panel_text),
                        parse_mode="HTML", reply_markup=panel_markup,
                        link_preview_options={"is_disabled": True},
                    )
                except TelegramAPIError:
                    replacement = None
                if replacement:
                    try:
                        await message.bot.delete_message(chat_id=panel_chat_id, message_id=panel_message_id)
                    except TelegramAPIError:
                        pass
        return

    if field == "custom_button_text":
        if not value or len(value) > 64:
            await tracked_answer(message, state, "Button text must be between 1 and 64 characters.")
            return
        data = await state.get_data()
        await state.update_data(custom_button_pending_text=value, field="custom_button_url")
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        await remove_input_prompt(message.bot, data)
        prompt = await tracked_answer(message, state, "Now send the button URL (https://... or t.me/...).")
        await state.update_data(
            input_prompt_chat_id=prompt.chat.id,
            input_prompt_message_id=prompt.message_id,
        )
        return
    if field == "custom_button_url":
        normalized = normalize_http_url(value)
        if not normalized:
            await tracked_answer(message, state, "Please send a valid http/https URL.")
            return
        prev = await state.get_data()
        cfg = await db.settings.find_one({"key": "custom_buttons"}) or {}
        buttons = list(cfg.get("buttons", []))
        if len(buttons) >= 2:
            await tracked_answer(message, state, "Maximum 2 custom buttons allowed.")
            await clear_state_preserving_prompts(state)
            return
        buttons.append({"text": prev.get("custom_button_pending_text", ""), "url": normalized})
        await patch(db, "custom_buttons", {"buttons": buttons})
        await remove_input_prompt(message.bot, prev)
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        # Refresh the original Custom Button panel in place after saving.
        panel_chat = prev.get("custom_panel_chat_id")
        panel_message = prev.get("custom_panel_message_id")
        if panel_chat and panel_message:
            current = buttons[:2]
            rows = []
            for i, item in enumerate(current):
                rows.append([
                    InlineKeyboardButton(text=str(item.get("text", f"Button {i+1}"))[:64], url=item.get("url")),
                    InlineKeyboardButton(text="✖", callback_data=f"settings:custom_button_remove:{i}"),
                ])
            if len(current) < 2:
                rows.append([InlineKeyboardButton(text="✚", callback_data="settings:custom_button_add")])
            rows.append([InlineKeyboardButton(text="《 BACK", callback_data="settings:home")])
            try:
                await message.bot.edit_message_text(
                    chat_id=panel_chat,
                    message_id=panel_message,
                    text=format_settings_page("Custom Button:\n\nYou can add a custom buttons to your files."),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                    link_preview_options={"is_disabled": True},
                )
            except TelegramAPIError:
                pass
        await clear_state_preserving_prompts(state)
        return
    if field == "animation_prefix":
        await patch(db, "animation", {"prefix": value}, {"enabled": False, "frames": [".", "..", "..."], "speed": 0.25, "mode": "force_join_poster"})
        await clear_state_preserving_prompts(state)
        await tracked_answer(message, state, "✨ Animation prefix updated successfully.")
        return
    if field in ("animation_frame_1", "animation_frame_2", "animation_frame_3"):
        if not message.text or not value:
            await tracked_answer(message, state, "Please send a non-empty text frame.")
            return
        frames = list(data.get("pending_frames", []))
        frames.append(value)
        if len(frames) < 3:
            next_field = f"animation_frame_{len(frames)+1}"
            await state.update_data(field=next_field, pending_frames=frames)
            prompt = "Now send the middle frame." if len(frames) == 1 else "Send the last frame."
            await tracked_answer(message, state, prompt)
            return
        await patch(db, "animation", {"frames": frames}, {"enabled": False, "prefix": "Checking", "speed": 0.25, "mode": "force_join_poster"})
        await clear_state_preserving_prompts(state)
        await tracked_answer(message, state, "✨ Animation frames updated successfully.")
        return
    if field == "forcejoin_label":
        if not value or len(value) > 64:
            await tracked_answer(message, state, "Button text must be between 1 and 64 characters.")
            return
        prev = await state.get_data()
        channel = prev.get("channel")
        if not channel:
            await tracked_answer(message, state, "Channel setting expired. Please reopen it.")
            await clear_state_preserving_prompts(state)
            return
        await save_force_join_channel_label(message.bot, db, channel, value)
        await clear_state_preserving_prompts(state)
        await tracked_answer(message, state, f"✨ Successfully updated button text for {channel} force-join button.")
        return
    if field == "forcejoin_message":
        value = formatted_input_text(message, value)
        await patch(db, "force_join", {"message": value})
        await finish_input_panel(message, state, "FORCE JOIN MESSAGE", "forcejoin")
        await clear_state_preserving_prompts(state)
        return
    if field == "forcejoin_verify_text":
        await patch(db, "force_join", {"verify_text": value})
        await clear_state_preserving_prompts(state)
        await tracked_answer(message, state, "✨ Successfully updated verification button text.")
        return
    if field == "forcejoin_media":
        if message.animation:
            await patch(db, "force_join", {"poster_file_id": message.animation.file_id, "poster_photo_id": None})
        elif message.photo:
            await patch(db, "force_join", {"poster_photo_id": message.photo[-1].file_id, "poster_file_id": None})
        else:
            await tracked_answer(message, state, "Only GIFs and images are supported. Send a photo or GIF.")
            return
        await clear_state_preserving_prompts(state)
        await tracked_answer(message, state, "✨ Force-join poster updated successfully.")
        return
    if field == "forcejoin_channel":
        await clear_state_preserving_prompts(state)
        await tracked_answer(message, state, "Please add a Group or Channel using the chat-selection keyboard from /settings → Force Join → Add.")
        return
    if field == "start_poster":
        if message.animation:
            await patch(db, "start_message", {"poster_file_id": message.animation.file_id, "poster_photo_id": None}, {"enabled": True})
        elif message.photo:
            await patch(db, "start_message", {"poster_photo_id": message.photo[-1].file_id, "poster_file_id": None}, {"enabled": True})
        else:
            await tracked_answer(message, state, "Only GIFs and images are supported. Send a photo or GIF.")
            return
        await clear_state_preserving_prompts(state); await tracked_answer(message, state, "✨ Start poster updated successfully."); return
    if field == "poster":
        if message.animation:
            await patch(db, "invalid_post", {"poster_file_id": message.animation.file_id, "poster_photo_id": None}, {"enabled": False})
        elif message.photo:
            await patch(db, "invalid_post", {"poster_photo_id": message.photo[-1].file_id, "poster_file_id": None}, {"enabled": False})
        else:
            await tracked_answer(message, state, "Only GIFs and images are supported. Send a photo or GIF.")
            return
        await clear_state_preserving_prompts(state); await tracked_answer(message, state, "✨ Invalid-post poster updated successfully."); return
    if not value:
        await tracked_answer(message, state, "Please send a non-empty value.")
        return
    if field.startswith("start_button_") and field.endswith("_text"):
        index = field.split("_")[2]
        await state.update_data(button_text=value, field=f"start_button_{index}_link")
        await tracked_answer(message, state, f"Now send the URL for Start Button {index}.")
        return
    if field.startswith("start_button_") and field.endswith("_link"):
        normalized_url = normalize_http_url(value)
        if not normalized_url:
            await tracked_answer(message, state, "Button link must be a valid URL, e.g. example.com, t.me/channel, @channel, or https://example.com.")
            return
        value = normalized_url
        index = field.split("_")[2]
        prev = await state.get_data()
        await patch(db, "start_message", {f"button{index}_text": prev.get("button_text", ""), f"button{index}_url": value, f"button{index}_enabled": True}, {"enabled": True, "text": DEFAULT_START_TEXT})
        await refresh_button_panel(message.bot, prev.get("panel_chat_id"), prev.get("panel_message_id"), db, "start")
        await clear_state_preserving_prompts(state)
        await state.update_data(button_panel_kind=prev.get("panel_kind"), button_panel_chat_id=prev.get("button_panel_chat_id"), button_panel_message_id=prev.get("button_panel_message_id"))
        return
    if field.startswith("invalid_button_") and field.endswith("_text"):
        index = field.split("_")[2]
        await state.update_data(button_text=value, field=f"invalid_button_{index}_link")
        await tracked_answer(message, state, f"Now send the URL for Invalid Button {index}.")
        return
    if field.startswith("invalid_button_") and field.endswith("_link"):
        parsed = urlparse(value)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            await tracked_answer(message, state, "Button link must be a valid http/https URL.")
            return
        index = field.split("_")[2]
        prev = await state.get_data()
        await patch(db, "invalid_post", {f"button{index}_text": prev.get("button_text", ""), f"button{index}_url": value, f"button{index}_enabled": True}, {"enabled": False})
        await refresh_button_panel(message.bot, prev.get("panel_chat_id"), prev.get("panel_message_id"), db, "invalid")
        await clear_state_preserving_prompts(state)
        await state.update_data(button_panel_kind=prev.get("panel_kind"))
        return
    if field == "start_text":
        value = formatted_input_text(message, value)
        await patch(db, "start_message", {"text": value}, {"enabled": True})
        await finish_input_panel(message, state, "START MESSAGE TEXT", "start_text")
        await clear_state_preserving_prompts(state)
        return
    elif field == "start_button_text":
        await patch(db, "start_message", {"button_text": value, "button_enabled": True}, {"enabled": True, "text": DEFAULT_START_TEXT, "button_url": "http://t.me/lexiAnime", "about_enabled": True})
        await tracked_answer(message, state, "✨ Successfully updated start message button text.")
    elif field == "start_button_link":
        parsed = urlparse(value)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            await tracked_answer(message, state, "Button link must be a valid http/https URL.")
            return
        await patch(db, "start_message", {"button_url": value, "button_enabled": True}, {"enabled": True, "button_text": "📟 UPDATE CHANNEL", "button_url": "http://t.me/lexiAnime"})
        await tracked_answer(message, state, "✨ Successfully updated start message button link.")
    elif field == "auto_notice":
        # Telegram sends formatting as entities, not HTML/Markdown in message.text.
        # Save the original text and entities together so bold, mono, quotes,
        # links, spoilers and other Telegram formatting can be reproduced exactly.
        notice_text = message.text if message.text is not None else value
        notice_entities = [
            entity.model_dump(mode="json", exclude_none=True)
            for entity in (message.entities or [])
        ]
        await patch(
            db, "auto_delete",
            {"notice": notice_text, "notice_entities": notice_entities},
            {"enabled": False, "minutes": 30},
        )
        await tracked_answer(message, state, "✨ Auto-delete message updated successfully.")
    elif field == "after_text_message":
        value = formatted_input_text(message, value)
        await patch(db, "after_delete", {"text": value}, {"enabled": False, "button_text": DEFAULT_AFTER_BUTTON})
        await tracked_answer(message, state, "✨ After-delete message text updated successfully.")
    elif field == "after_text_button":
        await patch(db, "after_delete", {"button_text": value}, {"enabled": False, "text": DEFAULT_AFTER_TEXT})
        await tracked_answer(message, state, "✨ After-delete button text updated successfully.")
    elif field == "invalid_text":
        value = formatted_input_text(message, value)
        await patch(db, "invalid_post", {"text": value}, {"enabled": False, "about_enabled": False})
        await finish_input_panel(message, state, "INVALID MESSAGE TEXT", "invalid")
        await clear_state_preserving_prompts(state)
        return
    elif field == "invalid_button_text":
        await state.update_data(button_text=value, field="invalid_button_link")
        await tracked_answer(message, state, "Send new button link for the invalid post.")
        return
    elif field == "invalid_button_link":
        parsed = urlparse(value)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            await tracked_answer(message, state, "Button link must be a valid http/https URL.")
            return
        prev = await state.get_data()
        values = {"button_url": value}
        if prev.get("button_text"): values["button_text"] = prev["button_text"]
        await patch(db, "invalid_post", values, {"enabled": False})
        await tracked_answer(message, state, "✨ Successfully updated invalid post button link.")
    elif field == "shortener_url":
        normalized_url = normalize_http_url(value)
        if not normalized_url:
            await tracked_answer(message, state, "Please send a valid shortener URL, e.g. example.com or https://example.com/api.")
            return
        await state.update_data(shortener_url=normalized_url, field="shortener_token")
        await tracked_answer(message, state, "Send your API token. It will be masked in settings and never logged.")
        return
    elif field == "shortener_token":
        prev = await state.get_data()
        try:
            await shorten_url(prev["shortener_url"], value, "https://example.com")
        except Exception as exc:
            await tracked_answer(message, state, shortener_validation_error(exc, value))
            return
        await patch(db, "shortener", {"enabled": True, "url": prev["shortener_url"], "token": value})
        await tracked_answer(message, state, f"✨ Successfully added {urlparse(prev['shortener_url']).netloc} as your link shortener provider.")
    elif field == "mod_id":
        if not value.isdigit() or int(value) <= 0:
            await tracked_answer(message, state, "Please send a valid numeric Telegram user ID.")
            return
        uid = int(value)
        if uid == settings.owner_id:
            await tracked_answer(message, state, "Owner is not stored as a moderator.")
            return
        if await db.users.find_one({"user_id": uid, "banned": True}):
            await tracked_answer(message, state, "Unban this user before adding them as a moderator.")
            await clear_state_preserving_prompts(state); return
        if await db.moderators.find_one({"user_id": uid}):
            await tracked_answer(message, state, "This user is already a moderator.")
            await clear_state_preserving_prompts(state); return
        if await db.moderators.count_documents({}) >= 3:
            await tracked_answer(message, state, "Maximum 3 moderators allowed.")
            await clear_state_preserving_prompts(state); return
        user = await db.users.find_one({"user_id": uid}) or {}
        name = user.get("first_name") or f"User {uid}"
        await db.moderators.insert_one({"user_id": uid, "first_name": name, "username": user.get("username")})
        await tracked_answer(message, state, f"✨ Successfully added {name} as moderator.")
    else:
        await tracked_answer(message, state, "Unknown setting. Open /settings to try again.")
    await clear_state_preserving_prompts(state)

# Backwards-compatible command aliases for owners/moderators who prefer commands.
@router.message(Command("setstart"))
async def setstart(m: Message, settings: Settings, database):
    if not await allowed(m, settings, database): return
    p=(m.text or "").split(maxsplit=1)
    if len(p)!=2 or p[1].lower() not in ("on","off"): await m.answer("Usage: /setstart on|off"); return
    await patch(database.db, "start_message", {"enabled": p[1].lower()=="on"}, {"text": DEFAULT_START_TEXT, "button_enabled": True, "button_text": "📟 UPDATE CHANNEL", "button_url": "http://t.me/lexiAnime", "about_enabled": True})
    await m.answer("Start message setting updated.")

@router.message(Command("setstarttext"))
async def setstarttext(m: Message, settings: Settings, database):
    if not await allowed(m, settings, database): return
    p=(m.text or "").split(maxsplit=1)
    if len(p)<2: await m.answer("Usage: /setstarttext TEXT"); return
    await patch(database.db, "start_message", {"text":p[1]}, {"enabled":True})
    await m.answer("Start message text updated.")

@router.message(Command("setinvalid"))
async def setinvalid(m: Message, settings: Settings, database):
    if not await allowed(m, settings, database): return
    p=(m.text or "").split(maxsplit=1)
    if len(p)!=2 or p[1].lower() not in ("on","off"): await m.answer("Usage: /setinvalid on|off"); return
    await patch(database.db, "invalid_post", {"enabled":p[1].lower()=="on"}, {"text":DEFAULT_INVALID, "about_enabled": False})
    await m.answer("Invalid-post setting updated.")

@router.message(Command("setinvalidtext"))
async def setinvalidtext(m: Message, settings: Settings, database):
    if not await allowed(m, settings, database): return
    p=(m.text or "").split(maxsplit=1)
    if len(p)<2: await m.answer("Usage: /setinvalidtext TEXT"); return
    await patch(database.db, "invalid_post", {"text":p[1]}, {"enabled":False})
    await m.answer("✨ Successfully updated your bot's invalid message text.")

@router.message(Command("setautodelete"))
async def setautodelete(m: Message, settings: Settings, database):
    if not await allowed(m, settings, database): return
    p=(m.text or "").split(maxsplit=1)
    if len(p)!=2 or p[1].lower() not in ("on","off"): await m.answer("Usage: /setautodelete on|off"); return
    turning_on = p[1].lower() == "on"
    await patch(database.db, "auto_delete", {"enabled": turning_on}, {"minutes": 30, "notice": DEFAULT_NOTICE})
    if not turning_on:
        await patch(database.db, "after_delete", {"enabled": False}, {"text": DEFAULT_AFTER_TEXT, "button_text": DEFAULT_AFTER_BUTTON})
        await database.db.tasks.update_many(
            {"kind": "delete_delivery", "status": {"$in": ["pending", "processing"]}},
            {"$set": {"status": "cancelled", "cancelled_at": datetime.now(timezone.utc)}},
        )
    await m.answer("Auto Delete setting updated.")

@router.message(Command("setautodeletemin"))
async def setautodeletemin(m: Message, settings: Settings, database):
    if not await allowed(m, settings, database): return
    p=(m.text or "").split()
    if len(p)!=2 or not p[1].isdigit() or int(p[1]) not in AUTO_DELETE_MINUTES:
        await m.answer("Choose one: 2, 5, 15, 30, 60, 180, 360, 720, or 1440 minutes."); return
    await patch(database.db, "auto_delete", {"minutes":int(p[1])}, {"enabled":False,"notice":DEFAULT_NOTICE})
    await m.answer(f"✨ Auto-delete time changed to {time_label(int(p[1]))}.")

@router.message(Command("setnotice"))
async def setnotice(m: Message, settings: Settings, database):
    if not await allowed(m, settings, database): return
    p=(m.text or "").split(maxsplit=1)
    if len(p)<2: await m.answer("Usage: /setnotice TEXT"); return
    await patch(database.db, "auto_delete", {"notice":p[1]}, {"enabled":False,"minutes":30})
    await m.answer("✨ Auto-delete message updated successfully.")

@router.message(Command("setafterdelete"))
async def setafterdelete(m: Message, settings: Settings, database):
    if not await allowed(m, settings, database): return
    p=(m.text or "").split(maxsplit=1)
    if len(p)!=2 or p[1].lower() not in ("on","off"): await m.answer("Usage: /setafterdelete on|off"); return
    turning_on = p[1].lower() == "on"
    auto = await database.db.settings.find_one({"key": "auto_delete", "enabled": True})
    if turning_on and not auto:
        await patch(database.db, "after_delete", {"enabled": False}, {"text": DEFAULT_AFTER_TEXT, "button_text": DEFAULT_AFTER_BUTTON})
        await m.answer("Enable Auto Delete before enabling After Delete.")
        return
    await patch(database.db, "after_delete", {"enabled": turning_on}, {"text": DEFAULT_AFTER_TEXT, "button_text": DEFAULT_AFTER_BUTTON})
    await m.answer("After Delete setting updated.")

@router.message(Command("setafterdeletetext"))
async def setafterdeletetext(m: Message, settings: Settings, database):
    if not await allowed(m, settings, database): return
    p=(m.text or "").split(maxsplit=1)
    if len(p)<2: await m.answer("Usage: /setafterdeletetext TEXT"); return
    await patch(database.db, "after_delete", {"text":p[1]}, {"enabled":False,"button_text":DEFAULT_AFTER_BUTTON})
    await m.answer("✨ After-delete message text updated successfully.")

@router.message(Command("setafterdeletebutton"))
async def setafterdeletebutton(m: Message, settings: Settings, database):
    if not await allowed(m, settings, database): return
    p=(m.text or "").split(maxsplit=1)
    if len(p)<2: await m.answer("Usage: /setafterdeletebutton BUTTON_TEXT"); return
    if not valid_button_text(p[1]):
        await m.answer("Button text must be between 1 and 64 characters.")
        return
    await patch(database.db, "after_delete", {"button_text":p[1]}, {"enabled":False,"text":DEFAULT_AFTER_TEXT})
    await m.answer("✨ After-delete button text updated successfully.")

@router.message(Command("setshortener"))
async def setshortener(m: Message, settings: Settings, database):
    if not await allowed(m, settings, database): return
    p=(m.text or "").split(maxsplit=2)
    if len(p)!=3: await m.answer("Usage: /setshortener API_URL API_TOKEN"); return
    parsed=urlparse(p[1])
    if parsed.scheme not in ("https","http") or not parsed.netloc: await m.answer("API URL must be a valid http/https URL."); return
    try:
        await shorten_url(p[1], p[2], "https://example.com")
    except Exception as exc:
        await m.answer(shortener_validation_error(exc, p[2]))
        return
    await patch(database.db, "shortener", {"enabled":True,"url":p[1],"token":p[2]})
    await m.answer(f"Shortener provider configured: {parsed.netloc}. API token is stored privately.")

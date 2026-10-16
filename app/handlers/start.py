"""Start command, Force Join gate, and single/batch deeplink delivery."""
import logging
import hashlib
import html
import asyncio
import time
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from aiogram import Router,F
from aiogram.filters import CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import Message,InlineKeyboardMarkup,InlineKeyboardButton,CallbackQuery
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from app.config import Settings
from app.database.files import FileRepository
from app.services.delivery import deliver_reference,deliver_batch,claim_delivery_request,finish_delivery_request
from app.services.permissions import is_owner_or_moderator
from app.services.text_format import normalize_telegram_text
router=Router(name="start");log=logging.getLogger(__name__)

def render_common_variables(raw_text, user, bot):
    """Render shared message variables as Telegram HTML without nesting links."""
    text = str(raw_text or "")
    username = getattr(bot, "username", None)
    bot_name = html.escape(getattr(bot, "full_name", None) or getattr(bot, "first_name", None) or "Bot")
    bot_mention = f'<a href="https://t.me/{html.escape(username)}">{bot_name}</a>' if username else bot_name
    values = {
        "{bot_mention}": bot_mention,
        "{bot}": bot_mention,
        "{user_mention}": user.mention_html(),
        "{user}": user.mention_html(),
        "{first_name}": html.escape(user.first_name or ""),
        "{last_name}": html.escape(user.last_name or ""),
        "{owner}": '<a href="https://t.me/lexxiowner">Lexi</a>',
        "{anime}": '<a href="https://t.me/lexianime">Lexi Anime</a>',
        "{hindi_anime}": '<a href="https://t.me/lexianimehindi">Lexi Anime Hindi</a>',
        "{mention}": user.mention_html(),
    }
    for token, replacement in values.items():
        text = text.replace(token, replacement)
    return text


async def effective_force_join_channels(database, settings) -> list[str]:
    cfg = await database.db.settings.find_one({"key": "force_join"})
    configured = cfg.get("channels") if cfg is not None else None
    # Keep environment defaults until an admin explicitly edits the channel list.
    # Once channels_override is set, an empty list is an intentional "no channels".
    if cfg is not None and cfg.get("channels_override") is True and isinstance(configured, list):
        raw_channels = configured
    else:
        raw_channels = configured if isinstance(configured, list) and configured else settings.force_join_channels
    channels = []
    for raw in raw_channels:
        channel = str(raw).strip()
        if channel.lower().startswith(("https://t.me/", "http://t.me/", "https://telegram.me/", "http://telegram.me/", "t.me/", "telegram.me/")):
            parsed_channel = urlparse(channel if "://" in channel else "https://" + channel)
            channel = "@" + parsed_channel.path.strip("/").split("/", 1)[0].lstrip("@")
        elif channel.startswith("@"):
            channel = channel.split("/", 1)[0]
        elif channel.startswith("tg://resolve?domain="):
            channel = "@" + channel.split("domain=", 1)[1].split("&", 1)[0]
        if channel:
            channels.append(channel)
    return channels

def animation_frame_text(prefix: str, frame: str) -> str:
    """Render a normal-weight prefix followed by one bold frame."""
    return f"{html.escape(str(prefix))}<b>{html.escape(str(frame))}</b>"


def normalize_animation_config(cfg: dict | None) -> dict:
    """Normalize legacy defaults and clamp the timer for both animation modes."""
    normalized = dict(cfg or {})
    legacy_default = not normalized.get("prefix") or normalized.get("prefix") == "Checking.."
    if legacy_default:
        normalized["prefix"] = "Checking"
        normalized["frames"] = [".", "..", "..."]
        normalized["speed"] = 0.15
    frames = normalized.get("frames")
    if not isinstance(frames, list) or len(frames) != 3:
        normalized["frames"] = [".", "..", "..."]
    try:
        speed = float(normalized.get("speed", 0.25))
    except (TypeError, ValueError):
        speed = 1.0
    normalized["speed"] = min(1.0, max(0.15, speed))
    if normalized.get("mode") not in ("force_join_poster", "new_message"):
        normalized["mode"] = "force_join_poster"
    return normalized


async def load_animation_config(db) -> dict:
    """Load and persist the corrected legacy defaults once, so old saved values do not flash."""
    raw = await db.settings.find_one({"key": "animation"}) or {}
    normalized = normalize_animation_config(raw)
    if any(raw.get(key) != normalized.get(key) for key in ("prefix", "frames", "speed", "mode")):
        await db.settings.update_one(
            {"key": "animation"},
            {"$set": {key: normalized[key] for key in ("prefix", "frames", "speed", "mode")}},
            upsert=True,
        )
    return normalized


def _join_signature(channels: list[str]) -> str:
    return hashlib.sha256(",".join(sorted(c.strip() for c in channels if c.strip())).encode()).hexdigest()

async def _is_verified(db, user_id: int, channels: list[str]) -> bool:
    if not channels:
        return True
    user = await db.users.find_one({"user_id": user_id}, {"force_join_signature": 1})
    return bool(user and user.get("force_join_signature") == _join_signature(channels))

async def _mark_verified(db, user_id: int, channels: list[str]):
    await db.users.update_one(
        {"user_id": user_id},
        {"$set": {"force_join_signature": _join_signature(channels), "force_join_verified_at": datetime.now(timezone.utc)}},
    )

def force_join_button_label(channel_cfg: dict, position: int) -> str:
    """Use a saved custom label or number the visible missing-channel list."""
    custom = str(channel_cfg.get("button_text") or "").strip()
    return (custom or f"Join {position}")[:64]


def build_force_join_channel_button(
    channel_cfg: dict, position: int, target: str | None, channel: str | None = None
):
    """Always render the channel; retry URL resolution on tap if it is not ready yet."""
    label = force_join_button_label(channel_cfg, position)
    if target:
        return InlineKeyboardButton(text=label, url=target)
    if channel:
        return InlineKeyboardButton(text=label, callback_data=f"forcejoin:repair:{channel}")
    return None


async def resolve_force_join_button_target(bot, db, channel: str, channel_cfg: dict) -> str | None:
    """Resolve a valid join URL and repair missing per-channel metadata when possible."""
    target = channel_cfg.get("button_url")
    chat = None
    try:
        chat_arg = int(channel) if channel.lstrip("-").isdigit() else channel
        chat = await bot.get_chat(chat_arg)
    except ValueError:
        log.warning("Invalid Force Join chat identifier %r", channel, exc_info=True)
    except TelegramAPIError as exc:
        log.warning("Could not resolve Force Join chat %r via Telegram: %s", channel, exc, exc_info=True)

    if chat and chat.username:
        # Public channels should always use their own canonical username link,
        # even if a legacy settings write left another URL on the record.
        target = f"https://t.me/{chat.username}"
        values = {
            "key": "force_join_channel", "channel": channel,
            "button_url": target, "channel_title": chat.title or str(chat.id),
            "chat_type": chat.type, "mode": "normal",
        }
        if any(
            channel_cfg.get(key) != value
            for key, value in values.items()
            if key not in {"key", "channel"}
        ):
            await db.settings.update_one(
                {"key": "force_join_channel", "channel": channel},
                {"$set": values},
                upsert=True,
            )
        return target

    stale_private_metadata = bool(
        chat and target and (
            not channel_cfg.get("channel_title")
            or not channel_cfg.get("chat_type")
            or (chat.title and channel_cfg.get("channel_title") != chat.title)
            or channel_cfg.get("chat_type") != chat.type
            or channel_cfg.get("mode", "join_request") != "join_request"
        )
    )
    if target and not stale_private_metadata:
        return target
    if stale_private_metadata:
        # A previous channel-key collision could leave another channel's invite
        # attached to this record. Recreate it when the saved metadata disagrees.
        target = None

    if chat:
        try:
            invite = await bot.create_chat_invite_link(
                chat_id=chat.id, creates_join_request=True, name="FB Force Join - Request"
            )
        except TelegramAPIError as exc:
            log.error(
                "Could not create a private Force Join invite for chat %s: %s",
                chat.id, exc, exc_info=True,
            )
            # Reuse the saved URL if Telegram refuses a replacement; never invent
            # a private invite URL.
            return channel_cfg.get("button_url") or None
        target = invite.invite_link
        await db.settings.update_one(
            {"key": "force_join_channel", "channel": channel},
            {"$set": {
                "key": "force_join_channel", "channel": channel,
                "button_url": target, "channel_title": chat.title or str(chat.id),
                "chat_type": chat.type, "mode": "join_request",
            }},
            upsert=True,
        )
        return target

    if channel.startswith(("https://t.me/", "http://t.me/")):
        return channel
    if not channel.lstrip("-").isdigit():
        return f"https://t.me/{channel.lstrip('@')}"
    return None


def _is_private_chat(chat) -> bool:
    """Return true for private channels and groups without a public username."""
    chat_type = getattr(chat, "type", None)
    return chat_type in ("channel", "group", "supergroup") and not getattr(chat, "username", None)


def _is_member_status(member) -> bool:
    """Normalize membership checks for Telegram ChatMember objects."""
    status = getattr(member, "status", member)
    return status in ("creator", "administrator", "member") or (
        status == "restricted" and getattr(member, "is_member", False)
    )


def _pending_request_is_recent(pending: dict | None, now=None, max_age_seconds: int = 15 * 24 * 60 * 60) -> bool:
    """Only honor a private join request for up to 15 days; legacy records are invalid."""
    requested_at = (pending or {}).get("requested_at")
    if not isinstance(requested_at, datetime):
        return False
    now = now or datetime.now(timezone.utc)
    if requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=timezone.utc)
    age = (now - requested_at.astimezone(timezone.utc)).total_seconds()
    # At exactly 15 days the request is expired, matching the cleanup worker's cutoff.
    return 0 <= age < max_age_seconds


async def join_status(bot, user_id: int, channels: list[str], db=None) -> list[str]:
    """Check memberships concurrently while preserving configured channel order."""
    async def check_channel(raw: str) -> str | None:
        channel = raw.strip()
        if not channel:
            return None
        try:
            chat_arg = int(channel) if channel.lstrip("-").isdigit() else channel
            chat = await bot.get_chat(chat_arg)
            member = await bot.get_chat_member(chat_id=chat.id, user_id=user_id)
            # Always trust live membership first, including private channels;
            # a pending request is only a fallback when the user is not a member.
            verified = _is_member_status(member)
            channel_cfg = (
                await db.settings.find_one({"key": "force_join_channel", "channel": channel})
                if db is not None else None
            )
            mode = (channel_cfg or {}).get("mode", "join_request")
            if not verified and _is_private_chat(chat) and mode == "join_request" and db is not None:
                pending = await db.pending_join_requests.find_one({"chat_id": chat.id, "user_id": user_id})
                verified = _pending_request_is_recent(pending)
            return None if verified else channel
        except TelegramAPIError:
            return channel

    results = await asyncio.gather(*(check_channel(channel) for channel in channels))
    return [channel for channel in results if channel is not None]


# Active text-animation tasks are process-local; file/gate state itself remains in MongoDB.
_gate_animation_tasks: dict[tuple[int, str], tuple[asyncio.Task, int | None]] = {}
_gate_animation_stop_events: dict[tuple[int, str], asyncio.Event] = {}
_forcejoin_locks: dict[tuple[int, str], asyncio.Lock] = {}


async def _cancel_gate_animation(bot, user_id: int, payload: str,
                                 delete_separate: bool = True) -> int | None:
    key = (user_id, payload)
    entry = _gate_animation_tasks.pop(key, None)
    stop_event = _gate_animation_stop_events.pop(key, None)
    if stop_event:
        stop_event.set()
    if not entry:
        return None
    task, separate_message_id = entry
    # Wake the animation immediately if it is sleeping. If a Telegram edit is
    # already in flight, let that one request finish before the final message
    # edit/delete so an old animation frame cannot overwrite the final state.
    if not task.done():
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.debug("Force Join animation task ended during shutdown", exc_info=True)
    if separate_message_id and delete_separate:
        async def cleanup_animation_message():
            try:
                await bot.delete_message(user_id, separate_message_id)
            except TelegramAPIError:
                pass
        asyncio.create_task(cleanup_animation_message())
    return separate_message_id


async def _animate_gate(bot, user_id: int, payload: str, gate_message: Message,
                        base_text: str, markup: InlineKeyboardMarkup, cfg: dict,
                        separate_message_id: int | None, animation_only: bool = False,
                        stop_event: asyncio.Event | None = None):
    frames = cfg.get("frames")
    if not isinstance(frames, list) or len(frames) != 3:
        frames = [".", "..", "..."]
    prefix_value = str(cfg.get("prefix") or "Checking")
    if prefix_value == "Checking..":
        prefix_value = "Checking"
    try:
        speed = float(cfg.get("speed", 0.25))
    except (TypeError, ValueError):
        speed = 1.0
    speed = min(1.0, max(0.15, speed))
    mode = cfg.get("mode", "force_join_poster")
    target_id = separate_message_id if mode == "new_message" else gate_message.message_id
    frame_index = 0  # The first frame is already visible.
    next_beat = time.monotonic() + speed
    key = (user_id, payload)
    try:
        while True:
            if stop_event and stop_event.is_set():
                break
            delay = max(0.0, next_beat - time.monotonic())
            if stop_event:
                # Event-driven wait means shutdown never waits for a frame beat.
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                if stop_event.is_set():
                    break
            else:
                await asyncio.sleep(delay)
            # Re-check immediately before issuing an edit. Verification may
            # have completed while the timer was waking up.
            if stop_event and stop_event.is_set():
                break
            frame_index = (frame_index + 1) % len(frames)
            animated = animation_frame_text(prefix_value, frames[frame_index])
            while True:
                try:
                    if mode == "new_message":
                        await bot.edit_message_text(
                            chat_id=user_id, message_id=target_id, text=animated, parse_mode="HTML",
                        )
                    else:
                        content = animated if animation_only else f"{base_text}\n\n{animated}"
                        if gate_message.photo or gate_message.animation or gate_message.document:
                            await bot.edit_message_caption(
                                chat_id=user_id, message_id=target_id, caption=content,
                                parse_mode="HTML", reply_markup=markup,
                            )
                        else:
                            await bot.edit_message_text(
                                chat_id=user_id, message_id=target_id, text=content,
                                parse_mode="HTML", reply_markup=markup,
                            )
                except TelegramRetryAfter as exc:
                    retry_delay = max(speed, float(exc.retry_after))
                    if stop_event:
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=retry_delay)
                        except asyncio.TimeoutError:
                            pass
                        if stop_event.is_set():
                            return
                    else:
                        await asyncio.sleep(retry_delay)
                    next_beat = time.monotonic()
                    continue
                except TelegramBadRequest as exc:
                    if "message is not modified" not in str(exc).lower():
                        log.debug("Force Join animation edit rejected", exc_info=True)
                        return
                except TelegramAPIError:
                    log.debug("Force Join animation stopped because its message could not be edited", exc_info=True)
                    return
                break
            # Keep a fixed beat when requests are fast; avoid catch-up bursts when
            # Telegram takes longer than one configured interval.
            next_beat += speed
            if next_beat <= time.monotonic():
                next_beat = time.monotonic() + speed
    finally:
        if _gate_animation_stop_events.get(key) is stop_event:
            _gate_animation_stop_events.pop(key, None)


async def _start_gate_animation(bot, user_id: int, payload: str, gate_message: Message,
                                base_text: str, markup: InlineKeyboardMarkup, cfg: dict,
                                animation_only: bool = False):
    if not cfg.get("enabled"):
        return
    await _cancel_gate_animation(bot, user_id, payload)
    mode = cfg.get("mode", "force_join_poster")
    separate_id = None
    if mode == "new_message":
        prefix_value = str(cfg.get("prefix") or "Checking")
        if prefix_value == "Checking..":
            prefix_value = "Checking"
        frames = cfg.get("frames")
        if not isinstance(frames, list) or len(frames) != 3:
            frames = [".", "..", "..."]
        first = animation_frame_text(prefix_value, frames[0])
        try:
            animation_message = await bot.send_message(user_id, first, parse_mode="HTML")
            separate_id = animation_message.message_id
        except TelegramAPIError:
            log.warning("Could not send separate Force Join animation", exc_info=True)
            return
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        _animate_gate(
            bot, user_id, payload, gate_message, base_text, markup, cfg,
            separate_id, animation_only, stop_event,
        )
    )
    key = (user_id, payload)
    _gate_animation_stop_events[key] = stop_event
    _gate_animation_tasks[key] = (task, separate_id)


async def gate_or_prompt(message:Message,payload:str,settings,database,bot)->bool:
    user=message.from_user
    if not user or user.id==settings.owner_id or await is_owner_or_moderator(database.db,user.id,settings):
        return False
    cfg = await database.db.settings.find_one({"key": "force_join"}) or {}
    if not cfg.get("enabled", False):
        return False
    channels = await effective_force_join_channels(database, settings)
    if not channels:
        return False

    animation_cfg = await load_animation_config(database.db)
    animation_enabled = bool(animation_cfg.get("enabled"))
    animation_mode = animation_cfg.get("mode", "force_join_poster")
    animation_post = None

    # Deeplink entry performs a membership check too, so show the configured
    # animation during that check rather than only after the user taps Verify.
    if animation_enabled:
        if animation_mode == "new_message":
            await _start_gate_animation(bot, user.id, payload, message, "", InlineKeyboardMarkup(inline_keyboard=[]), animation_cfg)
        else:
            frames = animation_cfg.get("frames")
            if not isinstance(frames, list) or len(frames) != 3:
                frames = [".", "..", "..."]
            prefix_value = str(animation_cfg.get("prefix") or "Checking")
            if prefix_value == "Checking..":
                prefix_value = "Checking"
            first = animation_frame_text(prefix_value, frames[0])
            try:
                if cfg.get("poster_file_id"):
                    animation_post = await bot.send_animation(user.id, cfg["poster_file_id"], caption=first, parse_mode="HTML")
                elif cfg.get("poster_photo_id"):
                    animation_post = await bot.send_photo(user.id, cfg["poster_photo_id"], caption=first, parse_mode="HTML")
                else:
                    animation_post = await bot.send_message(user.id, first, parse_mode="HTML")
                await _start_gate_animation(bot, user.id, payload, animation_post, "", InlineKeyboardMarkup(inline_keyboard=[]), animation_cfg, animation_only=True)
            except TelegramAPIError:
                log.warning("Could not create Force Join animation poster during deeplink check", exc_info=True)
                animation_post = None

    missing = await join_status(bot, user.id, channels, database.db)
    # The stop helper below lets the current visible cycle finish.
    if not missing:
        await _cancel_gate_animation(bot, user.id, payload)
        if animation_post:
            try:
                await animation_post.delete()
            except TelegramAPIError:
                pass
        await _mark_verified(database.db, user.id, channels)
        return False

    join_buttons = []
    for index, channel in enumerate(missing):
        channel_cfg = await database.db.settings.find_one({"key": "force_join_channel", "channel": channel}) or {}
        target = await resolve_force_join_button_target(bot, database.db, channel, channel_cfg)
        button = build_force_join_channel_button(channel_cfg, index + 1, target, channel)
        if button:
            join_buttons.append(button)
    rows = [join_buttons[i:i+2] for i in range(0, len(join_buttons), 2)]
    rows.append([InlineKeyboardButton(text=cfg.get("verify_text") or "ɪ ᴊᴏɪɴᴇᴅ — ᴄʜᴇᴄᴋ ᴀɢᴀɪɴ",callback_data=f"forcejoin:check:{payload}")])
    raw_text = cfg.get("message") or "Hey {user_mention}! Please join my {remaining_count} remaining channel(s) to use me."
    me = await bot.get_me()
    rendered = render_common_variables(raw_text, user, me).replace("{remaining_count}", str(len(missing)))
    markup = InlineKeyboardMarkup(inline_keyboard=rows)

    separate_animation_id = None
    if animation_enabled:
        separate_animation_id = await _cancel_gate_animation(
            bot, user.id, payload, delete_separate=(animation_mode != "new_message")
        )
    if animation_enabled and animation_mode == "force_join_poster" and animation_post:
        # The animation poster becomes the Force Join prompt; keep one post only.
        if await _edit_force_join_post(animation_post, rendered, markup):
            return True
        try:
            await animation_post.delete()
        except TelegramAPIError:
            pass
    if animation_enabled and animation_mode == "new_message" and (cfg.get("poster_file_id") or cfg.get("poster_photo_id")):
        # Text animation messages cannot be converted into media posts. Keep the
        # configured Force Join poster visible in the final verification prompt.
        await _send_force_join_post(bot, user.id, cfg, rendered, markup)
        if separate_animation_id:
            try:
                await bot.delete_message(chat_id=user.id, message_id=separate_animation_id)
            except TelegramAPIError:
                pass
        return True
    if animation_enabled and animation_mode == "new_message" and separate_animation_id:
        # Reuse the exact message that showed the animation as the Force Join post.
        try:
            await bot.edit_message_text(
                chat_id=user.id,
                message_id=separate_animation_id,
                text=rendered,
                reply_markup=markup,
                parse_mode="HTML",
            )
            return True
        except TelegramAPIError:
            log.warning("Could not convert deeplink animation message into Force Join post", exc_info=True)
    new_post = await _send_force_join_post(bot, user.id, cfg, rendered, markup)
    if separate_animation_id:
        try:
            await bot.delete_message(chat_id=user.id, message_id=separate_animation_id)
        except TelegramAPIError:
            pass
    return True

async def deliver_payload(bot,database,settings,user_id:int,payload:str,record:dict,request_key:str|None=None):
    me=await bot.get_me();link=f"https://t.me/{me.username}?start={payload}"
    if payload.startswith("file_"):return await deliver_reference(bot,database.db,settings,user_id=user_id,file_doc=record,deeplink=link,request_key=request_key)
    return await deliver_batch(bot,database.db,user_id=user_id,batch=record,deeplink=link,request_key=request_key)

@router.message(CommandStart())
async def start_command(message:Message,command:CommandObject,settings:Settings,database,bot):
    user=message.from_user
    if not user:return
    repo=FileRepository(database.db);await repo.save_user(user)
    if await repo.is_banned(user.id):return
    payload=command.args or ""
    if payload:
        if payload.startswith("file_"):record=await repo.get_file(payload[5:])
        elif payload.startswith("batch_"):record=await repo.get_batch(payload[6:])
        else:
            await message.answer("This link is invalid or expired.");return
        if not record or len(payload.split("_",1)[1])>40:
            await message.answer("This link is invalid or expired.");return
        if await gate_or_prompt(message,payload,settings,database,bot):return
        request_key = f"start:{user.id}:{message.message_id}"
        if not await claim_delivery_request(database.db, request_key, user.id, payload):
            return
        success = False
        try:
            result=await deliver_payload(bot,database,settings,message.chat.id,payload,record,request_key=request_key)
            if payload.startswith("batch_") and result[0]==0:
                await message.answer("None of the batch files could be delivered. The source messages may be unavailable.")
            elif payload.startswith("batch_") and result[1]:
                log.warning("Batch delivery partial: %s delivered, %s failed",*result)
            elif payload.startswith("file_") and result[0] == 0:
                await message.answer("This file is currently unavailable. The source message may have been deleted or become inaccessible.")
            success = result[0] > 0
        except TelegramAPIError:
            log.exception("Delivery failed for %s",payload);await message.answer("This file is currently unavailable. Please contact the bot owner.")
        except Exception:
            log.exception("Unexpected delivery failure for %s", payload)
            await message.answer("Delivery failed due to a temporary error. Please try the link again.")
        finally:
            try: await finish_delivery_request(database.db, request_key, success)
            except Exception: log.exception("Could not update delivery request status")
        return
    if await is_owner_or_moderator(database.db,user.id,settings):return
    cfg=await database.db.settings.find_one({"key":"start_message"}) or {}
    if cfg.get("enabled", True):
        rows = []
        button1_url = cfg.get("button1_url", cfg.get("button_url", "http://t.me/lexiAnime"))
        button1_text = cfg.get("button1_text", cfg.get("button_text", "📟 UPDATE CHANNEL"))
        parsed1 = urlparse(button1_url)
        configured_buttons = []
        if cfg.get("button1_enabled", cfg.get("button_enabled", True)) and button1_text and parsed1.scheme in ("http", "https") and parsed1.netloc:
            configured_buttons.append(InlineKeyboardButton(text=button1_text, url=button1_url))
        button2_url = cfg.get("button2_url", "")
        button2_text = cfg.get("button2_text", "")
        parsed2 = urlparse(button2_url)
        if cfg.get("button2_enabled", False) and button2_text and parsed2.scheme in ("http", "https") and parsed2.netloc:
            configured_buttons.append(InlineKeyboardButton(text=button2_text, url=button2_url))
        if configured_buttons:
            rows.append(configured_buttons)
        if cfg.get("about_enabled", True):
            rows.append([InlineKeyboardButton(text=cfg.get("about_button_text", "ᴀʙᴏᴜᴛ"), callback_data="start:about")])
        markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        raw_text = normalize_telegram_text(cfg.get("text") or "<b>Hey {user_mention}✨</b>\\n\\n<i>Welcome to the fastest and most secure file store bot!</i>")
        me = await message.bot.get_me()
        text = render_common_variables(raw_text, user, me)
        if cfg.get("poster_file_id"):
            await message.answer_animation(cfg["poster_file_id"], caption=text, reply_markup=markup, parse_mode="HTML")
        elif cfg.get("poster_photo_id"):
            await message.answer_photo(cfg["poster_photo_id"], caption=text, reply_markup=markup, parse_mode="HTML")
        else:
            await message.answer(text, reply_markup=markup, parse_mode="HTML")

@router.callback_query(F.data == "start:about")
async def start_about(query: CallbackQuery, database):
    await query.answer()
    if not query.from_user or await FileRepository(database.db).is_banned(query.from_user.id):
        return
    cfg = await database.db.settings.find_one({"key": "start_message"}) or {}
    if not cfg.get("enabled", True) or not cfg.get("about_enabled", True) or not query.message:
        return
    me = await query.bot.get_me()
    from app.handlers.misc import load_shared_about_text
    about_text = await load_shared_about_text(database.db)
    rendered = render_common_variables(about_text, query.from_user, me).replace("{bot_name}", render_common_variables("{bot_mention}", query.from_user, me))
    back_markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="《 BACK", callback_data="start:about_back")]])
    try:
        if query.message.photo or query.message.animation:
            await query.message.edit_caption(caption=rendered, parse_mode="HTML", reply_markup=back_markup)
        else:
            await query.message.edit_text(rendered, parse_mode="HTML", reply_markup=back_markup)
    except TelegramAPIError:
        pass

@router.callback_query(F.data == "start:about_back")
async def start_about_back(query: CallbackQuery, settings: Settings, database):
    await query.answer()
    user = query.from_user
    msg = query.message
    if not user or not msg or await FileRepository(database.db).is_banned(user.id):
        return
    cfg = await database.db.settings.find_one({"key": "start_message"}) or {}
    if not cfg.get("enabled", True):
        return
    me = await query.bot.get_me()
    rows = []
    url = cfg.get("button1_url", cfg.get("button_url", "http://t.me/lexiAnime"))
    label = cfg.get("button1_text", cfg.get("button_text", "📟 UPDATE CHANNEL"))
    parsed = urlparse(url)
    configured_buttons = []
    if cfg.get("button1_enabled", cfg.get("button_enabled", True)) and label and parsed.scheme in ("http", "https") and parsed.netloc:
        configured_buttons.append(InlineKeyboardButton(text=label, url=url))
    url2 = cfg.get("button2_url", "")
    label2 = cfg.get("button2_text", "")
    parsed2 = urlparse(url2)
    if cfg.get("button2_enabled", False) and label2 and parsed2.scheme in ("http", "https") and parsed2.netloc:
        configured_buttons.append(InlineKeyboardButton(text=label2, url=url2))
    if configured_buttons:
        rows.append(configured_buttons)
    if cfg.get("about_enabled", True):
        rows.append([InlineKeyboardButton(text=cfg.get("about_button_text", "ᴀʙᴏᴜᴛ"), callback_data="start:about")])
    markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    raw = cfg.get("text") or '<b>Hey {user_mention}✨</b>\n\n<i>Welcome to the fastest and most secure file store bot!.</i>'
    rendered = render_common_variables(raw, user, me)
    try:
        if msg.photo:
            await msg.edit_caption(caption=rendered, parse_mode="HTML", reply_markup=markup)
        elif msg.animation:
            await msg.edit_caption(caption=rendered, parse_mode="HTML", reply_markup=markup)
        else:
            await msg.edit_text(rendered, parse_mode="HTML", reply_markup=markup)
    except TelegramAPIError:
        pass

async def cleanup_expired_pending_join_requests(db, now=None, max_age_days: int = 15):
    """Delete pending-request records only after their 15-day expiry."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    result = await db.pending_join_requests.delete_many({"requested_at": {"$lte": cutoff}})
    return getattr(result, "deleted_count", 0)


async def run_pending_join_request_cleanup_worker(db, interval_seconds: int = 3600):
    """Periodically remove expired pending join requests, independent of verification."""
    while True:
        await cleanup_expired_pending_join_requests(db)
        await asyncio.sleep(interval_seconds)


@router.chat_join_request()
async def remember_pending_join_request(event, database):
    # Telegram emits this only when the bot has the appropriate channel admin rights.
    await database.db.pending_join_requests.update_one(
        {"chat_id": event.chat.id, "user_id": event.from_user.id},
        {"$set": {"chat_id": event.chat.id, "user_id": event.from_user.id,
                  "requested_at": datetime.now(timezone.utc)}},
        upsert=True,
    )

@router.chat_member()
async def clear_accepted_private_join_request(event, database):
    """Drop a saved request only when a private-channel request becomes membership."""
    if not _is_private_chat(event.chat):
        return
    if _is_member_status(event.old_chat_member) or not _is_member_status(event.new_chat_member):
        return
    await database.db.pending_join_requests.delete_one(
        {"chat_id": event.chat.id, "user_id": event.new_chat_member.user.id}
    )


async def _send_force_join_post(bot, user_id: int, cfg: dict, text: str, markup: InlineKeyboardMarkup):
    """Send a replacement Force Join post before removing the previous one."""
    try:
        if cfg.get("poster_file_id"):
            return await bot.send_animation(user_id, cfg["poster_file_id"], caption=text, reply_markup=markup, parse_mode="HTML")
        if cfg.get("poster_photo_id"):
            return await bot.send_photo(user_id, cfg["poster_photo_id"], caption=text, reply_markup=markup, parse_mode="HTML")
        return await bot.send_message(user_id, text, reply_markup=markup, parse_mode="HTML")
    except TelegramAPIError:
        log.warning("Configured Force Join poster could not be sent; using text post", exc_info=True)
        return await bot.send_message(user_id, text, reply_markup=markup, parse_mode="HTML")


async def _edit_force_join_post(message: Message, text: str, markup: InlineKeyboardMarkup) -> bool:
    """Edit the existing poster in place to show only channels still needing a join."""
    try:
        if message.animation or message.photo or message.document:
            await message.edit_caption(caption=text, reply_markup=markup, parse_mode="HTML")
        else:
            await message.edit_text(text=text, reply_markup=markup, parse_mode="HTML")
        return True
    except TelegramAPIError:
        log.warning("Could not edit animation poster into Force Join prompt", exc_info=True)
        return False

@router.callback_query(F.data.startswith("forcejoin:repair:"))
async def forcejoin_repair_link(query: CallbackQuery, database):
    """Retry resolving a real join URL and replace the temporary callback button."""
    if not query.message or not query.message.reply_markup:
        await query.answer()
        return
    channel = query.data.split(":", 2)[2]
    channel_cfg = await database.db.settings.find_one(
        {"key": "force_join_channel", "channel": channel}
    ) or {}
    target = await resolve_force_join_button_target(query.bot, database.db, channel, channel_cfg)
    if target:
        rows = []
        for row in query.message.reply_markup.inline_keyboard:
            updated_row = []
            for button in row:
                if button.callback_data == query.data:
                    updated_row.append(InlineKeyboardButton(text=button.text, url=target))
                else:
                    updated_row.append(button)
            rows.append(updated_row)
        try:
            await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        except TelegramAPIError:
            log.warning("Could not replace repaired Force Join URL button", exc_info=True)
    await query.answer()


@router.callback_query(F.data.startswith("forcejoin:check:"))
async def forcejoin_check(query: CallbackQuery, settings: Settings, database, bot):
    if not query.from_user:
        await query.answer()
        return
    payload = query.data.split(":", 2)[2]
    key = (query.from_user.id, payload)
    lock = _forcejoin_locks.setdefault(key, asyncio.Lock())
    if lock.locked():
        await query.answer("Verification is already running.", show_alert=False)
        return
    async with lock:
        try:
            await _forcejoin_check_impl(query, settings, database, bot)
        finally:
            if _forcejoin_locks.get(key) is lock:
                _forcejoin_locks.pop(key, None)


async def _forcejoin_check_impl(query: CallbackQuery, settings: Settings, database, bot):
    await query.answer()
    payload=query.data.split(":",2)[2]
    if not query.from_user or await FileRepository(database.db).is_banned(query.from_user.id):
        return

    user_id = query.from_user.id
    old_post = query.message
    # Stop animation without waiting for its frame/request to finish.
    await _cancel_gate_animation(bot, user_id, payload, delete_separate=False)
    # Load independent settings concurrently so the Verify animation can start sooner.
    force_cfg_result, animation_cfg = await asyncio.gather(
        database.db.settings.find_one({"key": "force_join"}),
        load_animation_config(database.db),
    )
    force_cfg = force_cfg_result or {}
    channels = await effective_force_join_channels(database, settings) if force_cfg.get("enabled", False) else []
    animation_enabled = bool(animation_cfg.get("enabled"))
    animation_mode = animation_cfg.get("mode", "force_join_poster")
    animation_post = None

    # In poster mode, the configured Force Join media must remain visible during
    # verification. Editing a text-only old post would otherwise silently skip it.
    if old_post and animation_enabled:
        if animation_mode == "new_message":
            await _start_gate_animation(
                bot, user_id, payload, old_post, "",
                InlineKeyboardMarkup(inline_keyboard=[]), animation_cfg,
            )
        else:
            frames = animation_cfg.get("frames")
            if not isinstance(frames, list) or len(frames) != 3:
                frames = [".", "..", "..."]
            prefix_value = str(animation_cfg.get("prefix") or "Checking")
            if prefix_value == "Checking..":
                prefix_value = "Checking"
            first = animation_frame_text(prefix_value, frames[0])
            empty_markup = InlineKeyboardMarkup(inline_keyboard=[])
            try:
                if force_cfg.get("poster_file_id"):
                    animation_post = await bot.send_animation(
                        user_id, force_cfg["poster_file_id"], caption=first, parse_mode="HTML"
                    )
                elif force_cfg.get("poster_photo_id"):
                    animation_post = await bot.send_photo(
                        user_id, force_cfg["poster_photo_id"], caption=first, parse_mode="HTML"
                    )
                else:
                    # No configured poster: retain the existing post and animate its text.
                    if await _edit_force_join_post(old_post, first, empty_markup):
                        animation_post = old_post
                    else:
                        animation_post = await bot.send_message(user_id, first, parse_mode="HTML")
                await _start_gate_animation(
                    bot, user_id, payload, animation_post, "", empty_markup,
                    animation_cfg, animation_only=True,
                )
            except TelegramAPIError:
                log.warning("Could not create Force Join animation poster", exc_info=True)
                animation_post = None

    missing = [] if await is_owner_or_moderator(database.db, user_id, settings) else await join_status(
        bot, user_id, channels, database.db
    )
    if missing:
        join_buttons = []
        for index, channel in enumerate(missing):
            channel_cfg = await database.db.settings.find_one({"key": "force_join_channel", "channel": channel}) or {}
            target = await resolve_force_join_button_target(
                bot, database.db, channel, channel_cfg
            )
            button = build_force_join_channel_button(channel_cfg, index + 1, target, channel)
            if button:
                join_buttons.append(button)
        rows = [join_buttons[i:i+2] for i in range(0, len(join_buttons), 2)]
        rows.append([InlineKeyboardButton(text=force_cfg.get("verify_text") or "ɪ ᴊᴏɪɴᴇᴅ — ᴄʜᴇᴄᴋ ᴀɢᴀɪɴ", callback_data=f"forcejoin:check:{payload}")])
        me = await bot.get_me()
        raw_text = str(force_cfg.get("message") or "Hey {mention}\nPlease Join My {remaining_count} Channel's To Use Me!")
        rendered = render_common_variables(raw_text, query.from_user, me).replace("{remaining_count}", str(len(missing)))
        markup = InlineKeyboardMarkup(inline_keyboard=rows)

        # Stop the animation, then edit its poster in place with the current
        # remaining-channel buttons and configured Force Join message.
        if animation_mode == "new_message":
            separate_animation_id = await _cancel_gate_animation(
                bot, user_id, payload, delete_separate=False
            )
        else:
            separate_animation_id = await _cancel_gate_animation(bot, user_id, payload)
        if animation_enabled and animation_mode == "force_join_poster" and animation_post:
            if await _edit_force_join_post(animation_post, rendered, markup):
                # Keep the previous verification post until animation has stopped
                # and its replacement contains the latest remaining-channel list.
                if old_post and old_post.message_id != animation_post.message_id:
                    try:
                        await old_post.delete()
                    except TelegramAPIError:
                        pass
                return

        if animation_enabled and animation_mode == "new_message" and (force_cfg.get("poster_file_id") or force_cfg.get("poster_photo_id")):
            # Text animation cannot be converted into a media post. Use the
            # configured Force Join poster for the final verification prompt.
            new_post = await _send_force_join_post(bot, user_id, force_cfg, rendered, markup)
            if separate_animation_id:
                try:
                    await bot.delete_message(chat_id=user_id, message_id=separate_animation_id)
                except TelegramAPIError:
                    pass
            if old_post and old_post.message_id != new_post.message_id:
                try:
                    await old_post.delete()
                except TelegramAPIError:
                    pass
            return
        if animation_enabled and animation_mode == "new_message" and separate_animation_id:
            # The animated message itself becomes the updated Force Join prompt.
            try:
                await bot.edit_message_text(
                    chat_id=user_id,
                    message_id=separate_animation_id,
                    text=rendered,
                    reply_markup=markup,
                    parse_mode="HTML",
                )
                if old_post and old_post.message_id != separate_animation_id:
                    try:
                        await old_post.delete()
                    except TelegramAPIError:
                        pass
                return
            except TelegramAPIError:
                log.warning("Could not convert Verify animation message into Force Join post", exc_info=True)

        new_post = await _send_force_join_post(bot, user_id, force_cfg, rendered, markup)
        if animation_post and animation_post.message_id != new_post.message_id:
            try:
                await animation_post.delete()
            except TelegramAPIError:
                pass
        if separate_animation_id:
            try:
                await bot.delete_message(chat_id=user_id, message_id=separate_animation_id)
            except TelegramAPIError:
                pass
        if old_post and old_post.message_id != new_post.message_id:
            try:
                await old_post.delete()
            except TelegramAPIError:
                pass
        return

    # Verification succeeded: stop animation immediately; never wait for its
    # cycle or message cleanup before delivering the requested file or batch.
    separate_animation_id = await _cancel_gate_animation(
        bot, user_id, payload, delete_separate=False
    )
    if animation_post and animation_post.message_id != (old_post.message_id if old_post else None):
        try:
            await animation_post.delete()
        except TelegramAPIError:
            pass
    repo=FileRepository(database.db)
    if payload.startswith("file_"):record=await repo.get_file(payload[5:])
    elif payload.startswith("batch_"):record=await repo.get_batch(payload[6:])
    else:return
    if not record:
        if old_post:
            try: await old_post.delete()
            except TelegramAPIError: pass
        await bot.send_message(user_id,"This link is invalid or expired.")
        return
    request_anchor = old_post.message_id if old_post else query.id
    request_key = f"forcejoin:{user_id}:{request_anchor}:{payload}"
    if not await claim_delivery_request(database.db, request_key, user_id, payload):
        return
    await _mark_verified(database.db, user_id, channels)
    # Preserve the verification post until at least one requested file is delivered.
    success = False
    try:
        result=await deliver_payload(bot,database,settings,user_id,payload,record,request_key=request_key)
        if payload.startswith("batch_") and result[0]==0:
            await bot.send_message(user_id,"None of the batch files could be delivered.")
        elif payload.startswith("file_") and result[0] == 0:
            await bot.send_message(user_id,"This file is currently unavailable. The source message may have been deleted or become inaccessible.")
        success = result[0] > 0
        if success:
            if old_post:
                try:
                    await old_post.delete()
                except TelegramAPIError:
                    log.info("Could not remove Force Join post after successful delivery")
            if separate_animation_id:
                async def cleanup_finished_animation():
                    try:
                        await bot.delete_message(chat_id=user_id, message_id=separate_animation_id)
                    except TelegramAPIError:
                        pass
                asyncio.create_task(cleanup_finished_animation())
    except TelegramAPIError:
        log.exception("Delivery after Force Join failed")
        await bot.send_message(user_id,"File delivery failed. Contact the owner.")
    except Exception:
        log.exception("Unexpected delivery failure after Force Join")
        await bot.send_message(user_id,"Delivery failed due to a temporary error.")
    finally:
        try: await finish_delivery_request(database.db, request_key, success)
        except Exception: log.exception("Could not update Force Join delivery request status")


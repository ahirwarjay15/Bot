"""Start command, Force Sub gate, and single/batch deeplink delivery."""
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
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter, TelegramNetworkError
from app.config import Settings
from app.database.files import FileRepository
from app.services.delivery import deliver_reference,deliver_batch,claim_delivery_request,finish_delivery_request
from app.services.permissions import is_owner_or_moderator
from app.services.text_format import normalize_telegram_text
router=Router(name="start");log=logging.getLogger(__name__)

def ensure_default_quote_format(raw_text, kind):
    """Repair legacy persisted defaults so old MongoDB values render the intended Telegram quotes."""
    text = str(raw_text or "")
    if kind == "start" and "<blockquote>" not in text:
        hello = "<b><i>✨ ʜᴇʟʟᴏ {mention}</i></b>"
        body = "ɪ'm ᴀ ᴘᴇʀᴍᴀɴᴇɴᴛ ꜰɪʟᴇ ꜱᴛᴏʀᴇ ʙᴏᴛ ʙʏ <b><i>{anime}</i></b>."
        if text.startswith(hello + "\\n\\n" + body):
            text = text.replace(hello, "<blockquote>" + hello + "</blockquote>", 1)
    elif kind == "force_join" and "<blockquote>" not in text:
        first = "⚡ ʜᴇʏ {mention}, ᴘʟᴇᴀꜱᴇ ᴊᴏɪɴ ᴀʟʟ ʀᴇQᴜɪʀᴇᴅ ᴄʜᴀɴɴᴇʟꜱ ᴛᴏ ᴜɴʟᴏᴄᴋ ᴀᴄᴄᴇꜱꜱ!"
        second = "ᴛᴀᴘ <b>『 ʀᴇᴛʀʏ 』</b> ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ ᴛᴏ ᴠᴇʀɪꜰʏ."
        if text == first + "\\n\\n" + second:
            text = "<blockquote>" + first + "</blockquote>\\n\\n<blockquote>" + second + "</blockquote>"
    return text


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

DEFAULT_ANIMATION_FRAMES = ["Checking.", "Checking..", "Checking...", "Checking....", "Checking....."]

def animation_frame_text(frame: str) -> str:
    """Render a saved frame while preserving Telegram HTML formatting."""
    import re
    frame_text = str(frame or "")
    if not re.search(r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|blockquote|tg-spoiler|tg-emoji|a)\b|&(?:amp|lt|gt|quot|#\d+|#x[0-9a-f]+);", frame_text, flags=re.IGNORECASE):
        frame_text = html.escape(frame_text)
    return frame_text

def normalize_animation_config(cfg: dict | None) -> dict:
    """Migrate legacy settings and enforce current frame and speed rules."""
    normalized = dict(cfg or {})
    frames = normalized.get("frames")
    frames = [str(frame) for frame in frames if str(frame).strip()] if isinstance(frames, list) else []
    if not 4 <= len(frames) <= 6: frames = list(DEFAULT_ANIMATION_FRAMES)
    normalized["frames"] = frames[:6]
    mode = normalized.get("mode")
    if mode in ("new_message", "message"): mode = "message"
    elif mode in ("force_join_poster", "poster"): mode = "poster"
    elif mode != "disabled": mode = "poster" if normalized.get("enabled", False) else "disabled"
    if normalized.get("enabled") is False: mode = "disabled"
    normalized["mode"] = mode
    normalized["enabled"] = mode != "disabled"
    try: speed = float(normalized.get("speed", 0.25))
    except (TypeError, ValueError): speed = 0.25
    normalized["speed"] = min(1.0, max(0.20, speed))
    normalized.pop("prefix", None)
    normalized.pop("prefix_enabled", None)
    normalized.pop("frames_enabled", None)
    return normalized

async def load_animation_config(db) -> dict:
    """Load and persist migrated animation defaults once."""
    raw = await db.settings.find_one({"key": "animation"}) or {}
    normalized = normalize_animation_config(raw)
    persisted_keys = ("frames", "speed", "mode", "enabled")
    legacy_keys = ("prefix", "prefix_enabled", "frames_enabled")
    if any(raw.get(key) != normalized.get(key) for key in persisted_keys) or any(key in raw for key in legacy_keys):
        await db.settings.update_one({"key": "animation"}, {"$set": {key: normalized[key] for key in persisted_keys}, "$unset": {key: "" for key in legacy_keys}}, upsert=True)
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


async def _force_join_owner_alert(bot, db, record: dict, operation: str, error: Exception, attempts: int):
    """Send one durable owner alert for a terminal Force Sub task failure."""
    task_id = str(record.get("_id", "unknown"))
    alerts = getattr(db, "force_join_alerts", None)
    if alerts is not None:
        try:
            claim = await alerts.update_one(
                {"_id": f"{operation}:{task_id}", "sent": {"$ne": True}},
                {"$setOnInsert": {"created_at": datetime.now(timezone.utc), "sent": False}},
                upsert=True,
            )
            existing = await alerts.find_one({"_id": f"{operation}:{task_id}", "sent": True})
            if existing:
                return
        except Exception:
            log.exception("Could not claim Force Sub owner alert")
            alerts = None
    settings_doc = await db.settings.find_one({"key": "runtime"}) or {}
    owner_id = settings_doc.get("owner_id")
    # OWNER_ID is supplied by the application settings at worker startup and is
    # passed through the bot's configured owner metadata when available.
    owner_id = owner_id or getattr(bot, "owner_id", None)
    chat_id = record.get("chat_id")
    chat_name = record.get("channel") or record.get("chat_title") or "unknown"
    message = (
        "⚠️ <b>Force Sub task failed</b>\n"
        f"Operation: <code>{html.escape(operation)}</code>\n"
        f"Chat: <code>{html.escape(str(chat_name))}</code>\n"
        f"Chat ID: <code>{html.escape(str(chat_id or 'unknown'))}</code>\n"
        f"Attempts: <code>{attempts}/3</code>\n"
        f"Task/link: <code>{html.escape(str(record.get('invite_link') or task_id))}</code>\n"
        f"Error: <code>{html.escape(str(error)[:2500])}</code>"
    )
    if owner_id:
        try:
            await bot.send_message(int(owner_id), message, parse_mode="HTML")
            if alerts is not None:
                await alerts.update_one({"_id": f"{operation}:{task_id}"}, {"$set": {"sent": True, "sent_at": datetime.now(timezone.utc)}})
        except Exception:
            log.exception("Could not deliver Force Sub owner alert")
    else:
        log.error("Force Sub owner alert cannot be sent: owner ID unavailable; %s", message)


async def _revoke_tracked_force_join_link(bot, db, record: dict) -> None:
    """Revoke an expired invite with durable, bounded retries."""
    link, chat_id = record.get("invite_link"), record.get("chat_id")
    now = datetime.now(timezone.utc)
    attempts = int(record.get("revoke_attempts", 0))
    due = record.get("revoke_retry_at")
    if isinstance(due, datetime) and due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    if due and due > now:
        return
    if link and chat_id is not None:
        try:
            await bot.revoke_chat_invite_link(chat_id=int(chat_id), invite_link=link)
        except TelegramAPIError as exc:
            # Telegram's "already revoked/expired" response is treated as completed.
            if isinstance(exc, (TelegramRetryAfter, TelegramNetworkError)):
                attempts += 1
                if attempts >= 3:
                    await db.force_join_links.update_one(
                        {"_id": record["_id"]},
                        {"$set": {"revoke_attempts": attempts, "revoke_failed": True,
                                  "revoke_last_error": str(exc), "revoke_failed_at": now}},
                    )
                    await _force_join_owner_alert(bot, db, record, "revoke", exc, attempts)
                    return
                await db.force_join_links.update_one(
                    {"_id": record["_id"]},
                    {"$set": {"revoke_attempts": attempts, "revoke_retry_at": now + timedelta(seconds=30),
                              "revoke_last_error": str(exc)}},
                )
                return
            log.info("Force Sub invite already expired/revoked: %s", exc)
        except Exception as exc:
            attempts += 1
            if attempts >= 3:
                await db.force_join_links.update_one(
                    {"_id": record["_id"]},
                    {"$set": {"revoke_attempts": attempts, "revoke_failed": True,
                              "revoke_last_error": str(exc), "revoke_failed_at": now}},
                )
                await _force_join_owner_alert(bot, db, record, "revoke", exc, attempts)
                return
            await db.force_join_links.update_one(
                {"_id": record["_id"]},
                {"$set": {"revoke_attempts": attempts, "revoke_retry_at": now + timedelta(seconds=30),
                          "revoke_last_error": str(exc)}},
            )
            return
    await db.force_join_links.delete_one({"_id": record["_id"]})


async def cleanup_expired_force_join_links(bot, db, limit: int = 200) -> int:
    now = datetime.now(timezone.utc)
    records = [r async for r in db.force_join_links.find({
        "expires_at": {"$lte": now}, "revoke_failed": {"$ne": True},
        "$or": [{"revoke_retry_at": {"$exists": False}}, {"revoke_retry_at": {"$lte": now}}],
    }).limit(limit)]
    semaphore = asyncio.Semaphore(10)

    async def cleanup_one(record):
        async with semaphore:
            # Atomic claim makes simultaneous workers skip the same link.
            owner = f"{id(asyncio.current_task())}:{time.monotonic()}"
            claim_until = now + timedelta(minutes=2)
            try:
                claimed = await db.force_join_links.find_one_and_update(
                    {"_id": record["_id"], "$or": [
                        {"revoke_claim_until": {"$lte": now}},
                        {"revoke_claim_until": {"$exists": False}},
                    ]},
                    {"$set": {"revoke_claim_owner": owner, "revoke_claim_until": claim_until}},
                    return_document=True,
                )
            except Exception:
                log.exception("Could not claim expired Force Sub link for cleanup")
                return
            if not claimed or claimed.get("revoke_claim_owner") != owner:
                return
            try:
                await _revoke_tracked_force_join_link(bot, db, claimed)
            except Exception:
                log.warning("Could not clean up an expired Force Sub link", exc_info=True)
            finally:
                await db.force_join_links.update_one(
                    {"_id": record["_id"], "revoke_claim_owner": owner},
                    {"$set": {"revoke_claim_until": datetime.now(timezone.utc)}},
                )

    await asyncio.gather(*(cleanup_one(record) for record in records))
    return len(records)


async def run_force_join_link_cleanup_worker(bot, db, interval_seconds: int = 5):
    """Recover due revocations promptly after restarts; retry failed API calls every 30s."""
    while True:
        try:
            await cleanup_expired_force_join_links(bot, db)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Force Sub link cleanup failed", exc_info=True)
        await asyncio.sleep(interval_seconds)


async def _revoke_used_force_join_link(bot, db, chat_id: int, invite_link) -> None:
    """Shared invite links are revoked only by their scheduled expiry task."""
    return


def _schedule_used_force_join_link_cleanup(bot, db, chat_id: int, invite_link) -> None:
    """Do not revoke a shared invite when one user uses it."""
    return


async def resolve_force_join_button_target(bot, db, channel: str, channel_cfg: dict, user_id: int | None = None) -> str | None:
    """Read a prepared shared link only; never call Telegram invite creation in a user request."""
    links_collection = getattr(db, "force_join_links", None)
    if links_collection is None:
        log.error("Force Sub invite rotation requires the force_join_links collection")
        return None
    now = datetime.now(timezone.utc)
    mode = channel_cfg.get("mode")
    link_mode = channel_cfg.get("link_mode", "rotation")
    if link_mode == "single":
        single_query = {"channel": channel, "link_mode": "single"}
        if mode in {"normal", "join_request"}:
            single_query["mode"] = mode
        single = await links_collection.find_one(single_query)
        if single and single.get("invite_link"):
            return single["invite_link"]
        log.warning("No saved Single Link is currently available for %s", channel)
        return None
    query = {
        "channel": channel,
        "link_mode": "rotation",
        "is_rotation_current": True,
        "refresh_at": {"$gt": now},
        "expires_at": {"$gt": now},
    }
    if mode in {"normal", "join_request"}:
        query["mode"] = mode
    cached = await links_collection.find_one(query)
    if cached and cached.get("invite_link"):
        return cached["invite_link"]
    # During an API outage, keep using the previous shared link until its real
    # Telegram expiry, even if its 25-minute rotation point has passed.
    fallback_query = {"channel": channel, "link_mode": "rotation", "expires_at": {"$gt": now}}
    if mode in {"normal", "join_request"}:
        fallback_query["mode"] = mode
    fallback = await links_collection.find_one(fallback_query)
    if fallback and fallback.get("invite_link"):
        return fallback["invite_link"]
    log.warning("No pre-generated Force Sub link is currently available for %s", channel)
    return None


async def _prepare_force_join_link(bot, db, channel: str, channel_cfg: dict) -> bool:
    """Create and persist a shared 30-minute link in the background rotation worker."""
    links = db.force_join_links
    try:
        chat_arg = int(channel) if channel.lstrip("-").isdigit() else channel
        chat = await bot.get_chat(chat_arg)
    except (ValueError, TelegramAPIError):
        log.warning("Could not resolve Force Sub chat %r for background link rotation", channel, exc_info=True)
        return False
    chat_id = int(chat.id)
    mode = channel_cfg.get("mode", "normal" if getattr(chat, "username", None) else "join_request")
    if mode not in {"normal", "join_request"}:
        mode = "normal" if getattr(chat, "username", None) else "join_request"
    link_mode = channel_cfg.get("link_mode", "rotation")
    now = datetime.now(timezone.utc)
    if link_mode == "single":
        current = await links.find_one({
            "channel": channel, "chat_id": chat_id, "mode": mode,
            "link_mode": "single", "invite_link": {"$exists": True},
        })
        if current and current.get("invite_link"):
            return True
    else:
        current = await links.find_one({
            "channel": channel, "chat_id": chat_id, "mode": mode,
            "link_mode": "rotation", "is_rotation_current": True, "expires_at": {"$gt": now},
        })
    refresh_at = current.get("refresh_at") if current else None
    # MongoDB datetime decoding may return naive UTC datetimes when tz_aware is disabled.
    # Normalize to UTC-aware before comparing with the aware UTC clock.
    if refresh_at is not None and refresh_at.tzinfo is None:
        refresh_at = refresh_at.replace(tzinfo=timezone.utc)
    if current and refresh_at and refresh_at > now:
        return True
    try:
        invite_args = {
            "chat_id": chat_id,
            "creates_join_request": (mode == "join_request"),
        }
        if link_mode == "rotation":
            invite_args["expire_date"] = int(now.timestamp()) + 1800
        invite = await bot.create_chat_invite_link(**invite_args)
    except TelegramAPIError:
        log.warning("Could not pre-generate Force Sub link for %s; retaining old link until expiry", channel, exc_info=True)
        return False
    # Anchor DB timestamps to the exact expiry timestamp sent to Telegram.
    created = now
    record = {
        "channel": channel,
        "chat_id": chat_id,
        "invite_link": invite.invite_link,
        "mode": mode,
        "link_mode": link_mode,
        "created_at": created,
        "is_rotation_current": True,
    }
    if link_mode == "rotation":
        record.update({
            "refresh_at": created + timedelta(seconds=1500),
            "expires_at": created + timedelta(seconds=1800),
            "cleanup_at": created + timedelta(seconds=1800),
        })
    try:
        await links.insert_one(record)
        if link_mode == "rotation":
            await links.update_many(
                {"channel": channel, "chat_id": chat_id, "mode": mode,
                 "link_mode": "rotation", "is_rotation_current": True, "_id": {"$ne": record.get("_id")}},
                {"$set": {"is_rotation_current": False}},
            )
    except Exception:
        try:
            await bot.revoke_chat_invite_link(chat_id=chat_id, invite_link=invite.invite_link)
        except TelegramAPIError:
            log.warning("Could not revoke untracked Force Sub invite", exc_info=True)
        log.exception("Could not persist pre-generated Force Sub link for %s", channel)
        return False
    return True


async def run_force_join_link_rotation_worker(bot, db, settings, interval_seconds: int = 30):
    """Warm missing links and rotate shared links at 25 minutes, before 30-minute expiry."""
    while True:
        try:
            channels = await effective_force_join_channels(type("DatabaseProxy", (), {"db": db})(), settings)
            cfg = await db.settings.find_one({"key": "force_join"}) or {}
            active_channels = set(channels)
            # Removed channels are excluded from link generation immediately.
            # Keep their existing rotation links valid until their recorded 30-minute
            # expiry; the expiry cleanup worker will revoke and remove them afterward.
            if cfg.get("enabled", False):
                channel_cfgs = await asyncio.gather(*(
                    db.settings.find_one({"key": "force_join_channel", "channel": channel})
                    for channel in channels
                ))
                semaphore = asyncio.Semaphore(5)
                async def prepare(channel, channel_cfg):
                    async with semaphore:
                        # Shared MongoDB lease prevents the two Worker services from
                        # creating/rotating links for the same channel simultaneously.
                        owner = f"{id(asyncio.current_task())}:{time.monotonic()}"
                        locks = db.service_locks
                        now = datetime.now(timezone.utc)
                        lease_until = now + timedelta(minutes=2)
                        try:
                            lock = await locks.find_one_and_update(
                                {"_id": f"force_join_rotation:{channel}",
                                 "$or": [{"lease_until": {"$lte": now}}, {"owner": owner}]},
                                {"$set": {"owner": owner, "lease_until": lease_until}},
                                upsert=True,
                                return_document=True,
                            )
                        except Exception as exc:
                            if getattr(exc, "code", None) == 11000:
                                return
                            raise
                        if not lock or lock.get("owner") != owner:
                            return
                        try:
                            # Re-read effective settings immediately before creation: a channel
                            # removed while this worker was waiting for its lease must not get a link.
                            latest_channels = await effective_force_join_channels(
                                type("DatabaseProxy", (), {"db": db})(), settings
                            )
                            latest_cfg = await db.settings.find_one({"key": "force_join"}) or {}
                            if channel not in latest_channels or not latest_cfg.get("enabled", False):
                                return
                            await _prepare_force_join_link(bot, db, channel, channel_cfg or {})
                        finally:
                            await locks.update_one(
                                {"_id": f"force_join_rotation:{channel}", "owner": owner},
                                {"$set": {"lease_until": datetime.now(timezone.utc)}},
                            )
                await asyncio.gather(*(prepare(channel, channel_cfg) for channel, channel_cfg in zip(channels, channel_cfgs)))
            # Expired links are revoked by the existing cleanup worker.
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Force Sub link rotation worker failed", exc_info=True)
        await asyncio.sleep(interval_seconds)

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
_gate_animation_cycle_events: dict[tuple[int, str], asyncio.Event] = {}
_forcejoin_locks: dict[tuple[int, str], asyncio.Lock] = {}


async def _cancel_gate_animation(bot, user_id: int, payload: str,
                                 delete_separate: bool = True) -> int | None:
    key = (user_id, payload)
    entry = _gate_animation_tasks.get(key)
    stop_event = _gate_animation_stop_events.get(key)
    if stop_event:
        stop_event.set()
    if not entry:
        _gate_animation_stop_events.pop(key, None)
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
            log.debug("Force Sub animation task ended during shutdown", exc_info=True)
    latest = _gate_animation_tasks.get(key)
    if latest and latest[0] is task:
        separate_message_id = latest[1]
        _gate_animation_tasks.pop(key, None)
    _gate_animation_stop_events.pop(key, None)
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
                        stop_event: asyncio.Event | None = None,
                        cycle_event: asyncio.Event | None = None):
    frames = cfg.get("frames")
    if not isinstance(frames, list) or not 4 <= len(frames) <= 6: frames = list(DEFAULT_ANIMATION_FRAMES)
    frames = [str(frame) for frame in frames]
    try: speed = float(cfg.get("speed", 0.25))
    except (TypeError, ValueError): speed = 0.25
    speed = min(1.0, max(0.20, speed))
    mode = cfg.get("mode", "poster")
    target_id = separate_message_id if mode == "message" else gate_message.message_id
    frame_index = 0
    frame_updates = 0
    # A single configured frame is a static display: do not repeatedly edit the same text.
    if len(frames) == 1:
        if cycle_event:
            cycle_event.set()
        return
    key = (user_id, payload)
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
            animated = animation_frame_text(frames[frame_index])
            while True:
                try:
                    content = animated if animation_only else f"{base_text}\n\n{animated}"
                    if mode == "message":
                        # New Message mode also edits the same frame message;
                        # both animation modes must update frames in place.
                        await bot.edit_message_text(
                            chat_id=user_id, message_id=target_id,
                            text=content, parse_mode="HTML", reply_markup=markup,
                        )
                    else:
                        # Force Sub Poster mode must animate the SAME poster.
                        # Telegram's API does not let bots suppress its Edited label.
                        if gate_message.animation or gate_message.photo or gate_message.document:
                            await bot.edit_message_caption(
                                chat_id=user_id, message_id=target_id,
                                caption=content, parse_mode="HTML", reply_markup=markup,
                            )
                        else:
                            await bot.edit_message_text(
                                chat_id=user_id, message_id=target_id,
                                text=content, parse_mode="HTML", reply_markup=markup,
                            )
                    current = _gate_animation_tasks.get(key)
                    running_task = asyncio.current_task()
                    if current and current[0] is running_task:
                        _gate_animation_tasks[key] = (
                            running_task, target_id if mode == "message" else None
                        )
                    frame_updates += 1
                    if cycle_event and frame_updates >= len(frames) - 1:
                        cycle_event.set()
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
                        log.debug("Force Sub animation edit rejected", exc_info=True)
                        return
                except TelegramAPIError:
                    log.debug("Force Sub animation stopped because its message could not be edited", exc_info=True)
                    return
                break
            # Keep a fixed beat when requests are fast; avoid catch-up bursts when
            # Telegram takes longer than one configured interval.
            next_beat += speed
            if next_beat <= time.monotonic():
                next_beat = time.monotonic() + speed
    finally:
        # Never hold Force Join processing until the cycle timeout if animation
        # stopped early because Telegram rejected an edit or the task was stopped.
        if cycle_event:
            cycle_event.set()
        if _gate_animation_stop_events.get(key) is stop_event:
            _gate_animation_stop_events.pop(key, None)


async def _start_gate_animation(bot, user_id: int, payload: str, gate_message: Message,
                                base_text: str, markup: InlineKeyboardMarkup, cfg: dict,
                                animation_only: bool = False, wait_for_cycle: bool = False):
    if not cfg.get("enabled") or cfg.get("mode") == "disabled":
        return
    await _cancel_gate_animation(bot, user_id, payload)
    mode = cfg.get("mode", "poster")
    separate_id = None
    if mode == "message":
        frames = cfg.get("frames")
        if not isinstance(frames, list) or not 1 <= len(frames) <= 6: frames = list(DEFAULT_ANIMATION_FRAMES)
        first = animation_frame_text(frames[0])
        try:
            animation_message = await bot.send_message(user_id, first, parse_mode="HTML")
            separate_id = animation_message.message_id
        except TelegramAPIError:
            log.warning("Could not send separate Force Sub animation", exc_info=True)
            return
    stop_event = asyncio.Event()
    cycle_event = asyncio.Event()
    task = asyncio.create_task(
        _animate_gate(
            bot, user_id, payload, gate_message, base_text, markup, cfg,
            separate_id, animation_only, stop_event, cycle_event,
        )
    )
    key = (user_id, payload)
    _gate_animation_stop_events[key] = stop_event
    _gate_animation_cycle_events[key] = cycle_event
    _gate_animation_tasks[key] = (task, separate_id)
    if wait_for_cycle and mode == "message":
        await cycle_event.wait()


async def _wait_for_gate_animation_minimum_cycle(user_id: int, payload: str, mode: str) -> None:
    """Message mode waits for one cycle; poster mode never has a minimum-cycle delay."""
    if mode != "message":
        return
    cycle_event = _gate_animation_cycle_events.get((user_id, payload))
    if cycle_event:
        await cycle_event.wait()


async def gate_or_prompt(message:Message,payload:str,settings,database,bot, *, verify_button: bool = True)->bool:
    user=message.from_user
    if not user or user.id==settings.owner_id or await is_owner_or_moderator(database.db,user.id,settings):
        return False
    cfg = await database.db.settings.find_one({"key": "force_join"}) or {}
    channels = await effective_force_join_channels(database, settings)
    if not channels:
        return False

    animation_cfg = await load_animation_config(database.db)
    animation_enabled = bool(animation_cfg.get("enabled"))
    animation_mode = animation_cfg.get("mode", "poster")
    animation_post = None

    # Deeplink entry performs a membership check too, so show the configured
    # animation during that check rather than only after the user taps Verify.
    if animation_enabled:
        if animation_mode == "message":
            await _start_gate_animation(bot, user.id, payload, message, "", InlineKeyboardMarkup(inline_keyboard=[]), animation_cfg)
        else:
            frames = animation_cfg.get("frames")
            if not isinstance(frames, list) or not 1 <= len(frames) <= 6: frames = list(DEFAULT_ANIMATION_FRAMES)
            first = animation_frame_text(frames[0])
            try:
                if cfg.get("poster_file_id"):
                    animation_post = await bot.send_animation(
                        user.id, cfg["poster_file_id"],
                        caption=first, parse_mode="HTML",
                    )
                elif cfg.get("poster_photo_id"):
                    animation_post = await bot.send_photo(
                        user.id, cfg["poster_photo_id"],
                        caption=first, parse_mode="HTML",
                    )
                else:
                    animation_post = None
                # Poster mode updates frames in the configured poster caption, never a separate text message.
                if animation_post:
                    await _start_gate_animation(
                        bot, user.id, payload, animation_post, "",
                        InlineKeyboardMarkup(inline_keyboard=[]), animation_cfg,
                        animation_only=True,
                    )
            except TelegramAPIError:
                log.warning("Could not create Force Sub animation poster during deeplink check", exc_info=True)
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
    channel_cfgs = await asyncio.gather(*(
        database.db.settings.find_one({"key": "force_join_channel", "channel": channel})
        for channel in missing
    ))
    channel_cfgs = [item or {} for item in channel_cfgs]
    targets = await asyncio.gather(*(
        resolve_force_join_button_target(bot, database.db, channel, channel_cfg, user.id)
        for channel, channel_cfg in zip(missing, channel_cfgs)
    ))
    for index, (channel, channel_cfg, target) in enumerate(zip(missing, channel_cfgs, targets)):
        button = build_force_join_channel_button(channel_cfg, index + 1, target, channel)
        if button:
            join_buttons.append(button)
    rows = [join_buttons[i:i+2] for i in range(0, len(join_buttons), 2)]
    if verify_button:
        rows.append([InlineKeyboardButton(text=cfg.get("verify_text") or "♻️ ʀᴇᴛʀʏ", callback_data=f"forcejoin:check:{payload}")])
    raw_text = ensure_default_quote_format(cfg.get("message") or "⚡ ʜᴇʏ {mention}, ᴘʟᴇᴀꜱᴇ ᴊᴏɪɴ ᴀʟʟ ʀᴇQᴜɪʀᴇᴅ ᴄʜᴀɴɴᴇʟꜱ ᴛᴏ ᴜɴʟᴏᴄᴋ ᴀᴄᴄᴇꜱꜱ!\\n\\nᴛᴀᴘ <b>『 ʀᴇᴛʀʏ 』</b> ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ ᴛᴏ ᴠᴇʀɪꜰʏ.", "force_join")
    me = await bot.get_me()
    rendered = render_common_variables(raw_text, user, me).replace("{remaining_count}", str(len(missing)))
    markup = InlineKeyboardMarkup(inline_keyboard=rows)

    # Let Force Join checks and invite-link resolution run alongside the animation.
    # Only Message mode has a minimum cycle; Poster mode proceeds as soon as the work is ready.
    if animation_enabled:
        await _wait_for_gate_animation_minimum_cycle(user.id, payload, animation_mode)
    separate_animation_id = None
    if animation_enabled:
        separate_animation_id = await _cancel_gate_animation(
            bot, user.id, payload, delete_separate=(animation_mode != "message")
        )
    if animation_enabled and animation_mode == "poster" and animation_post:
        # Reuse the animated poster as the final Force Join post; only its caption/buttons change.
        await _edit_force_join_post(animation_post, rendered, markup)
        return True
    if animation_enabled and animation_mode == "message":
        # Publish the final Force Sub post and remove the last animation frame concurrently.
        async def delete_message_animation():
            if separate_animation_id:
                try:
                    await bot.delete_message(chat_id=user.id, message_id=separate_animation_id)
                except TelegramAPIError:
                    pass
        await asyncio.gather(
            _send_force_join_post(bot, user.id, cfg, rendered, markup),
            delete_message_animation(),
        )
        return True
    new_post = await _send_force_join_post(bot, user.id, cfg, rendered, markup)
    if separate_animation_id:
        try:
            await bot.delete_message(chat_id=user.id, message_id=separate_animation_id)
        except TelegramAPIError:
            pass
    return True

async def send_file_animation(bot, db, user_id: int) -> None:
    """Show and immediately clear the configured blink before file delivery."""
    msg = None
    try:
        cfg = await load_animation_config(db)
        text = str(cfg.get("file_animation_text") or "").strip()
        if not text:
            return
        msg = await bot.send_message(user_id, text, parse_mode="HTML")
        # Keep the blink brief, then remove it before sending the file.
        await asyncio.sleep(0.15)
    except TelegramAPIError:
        log.warning("Blink send failed; continuing delivery", exc_info=True)
    finally:
        if msg:
            try:
                await msg.delete()
            except TelegramAPIError:
                log.debug("Blink delete failed; continuing delivery", exc_info=True)


async def delete_start_command_after(message: Message, db, *, delay: bool = True) -> None:
    """Let the destination post appear, then delete /start after one configured beat."""
    try:
        if delay:
            cfg = await load_animation_config(db)
            await asyncio.sleep(cfg["speed"])
        await message.delete()
    except Exception:
        log.debug("Could not delay/delete the incoming /start command", exc_info=True)


async def deliver_payload(bot,database,settings,user_id:int,payload:str,record:dict,request_key:str|None=None):
    me=await bot.get_me();link=f"https://t.me/{me.username}?start={payload}"
    if isinstance(record.get("items"), list):
        return await deliver_batch(bot,database.db,user_id=user_id,batch=record,deeplink=link,request_key=request_key)
    return await deliver_reference(bot,database.db,settings,user_id=user_id,file_doc=record,deeplink=link,request_key=request_key)

@router.message(CommandStart())
async def start_command(message:Message,command:CommandObject,settings:Settings,database,bot):
    user=message.from_user
    if not user:return
    repo=FileRepository(database.db);await repo.save_user(user)
    if await repo.is_banned(user.id):return
    payload=command.args or ""
    if payload:
        # Public start links contain only the 20-character token. The first five
        # characters are the Deepcode; the remaining characters keep the URL
        # opaque without adding visible file/batch/deepcode prefixes.
        record = await repo.get_file(payload[:5]) if len(payload) == 20 and payload.isalnum() else None
        if not record:
            await message.answer("This link is invalid or expired.")
            await delete_start_command_after(message, database.db)
            return
        if await gate_or_prompt(message,payload,settings,database,bot):
            await delete_start_command_after(message, database.db)
            return
        auto_delete_cfg = await database.db.settings.find_one({"key": "auto_delete", "enabled": True})
        request_key = f"start:{user.id}:{message.message_id}" if auto_delete_cfg else None
        if request_key and not await claim_delivery_request(database.db, request_key, user.id, payload):
            await delete_start_command_after(message, database.db)
            return
        success = False
        try:
            # Flash and clear the blink before delivering the file/batch.
            await send_file_animation(bot, database.db, message.chat.id)
            result=await deliver_payload(bot,database,settings,message.chat.id,payload,record,request_key=request_key)
            is_batch = isinstance(record.get("items"), list)
            if is_batch and result[0]==0:
                await message.answer("None of the batch files could be delivered. The source messages may be unavailable.")
            elif is_batch and result[1]:
                log.warning("Batch delivery partial: %s delivered, %s failed",*result)
            elif not is_batch and result[0] == 0:
                await message.answer("This file is currently unavailable. The source message may have been deleted or become inaccessible.")
            success = result[0] > 0
        except TelegramAPIError:
            log.exception("Delivery failed for %s",payload);await message.answer("This file is currently unavailable. Please contact the bot owner.")
        except Exception:
            log.exception("Unexpected delivery failure for %s", payload)
            await message.answer("Delivery failed due to a temporary error. Please try the link again.")
        finally:
            # The blink has already appeared before delivery.
            await delete_start_command_after(message, database.db, delay=False)
            if request_key:
                try: await finish_delivery_request(database.db, request_key, success)
                except Exception: log.exception("Could not update delivery request status")
        return
    if await is_owner_or_moderator(database.db,user.id,settings):
        await delete_start_command_after(message, database.db)
        return
    # Plain /start checks Force Sub first; this prompt intentionally has no Verify button.
    if await gate_or_prompt(message, "", settings, database, bot, verify_button=False):
        await delete_start_command_after(message, database.db)
        return
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
        for index in (3, 4):
            button_url = cfg.get(f"button{index}_url", "")
            button_text = cfg.get(f"button{index}_text", "")
            parsed_button = urlparse(button_url)
            if cfg.get(f"button{index}_enabled", False) and button_text and parsed_button.scheme in ("http", "https") and parsed_button.netloc:
                configured_buttons.append(InlineKeyboardButton(text=button_text, url=button_url))
        rows.extend(configured_buttons[i:i+2] for i in range(0, len(configured_buttons), 2))
        if cfg.get("about_enabled", True):
            rows.append([InlineKeyboardButton(text=cfg.get("about_button_text", "ᴀʙᴏᴜᴛ"), callback_data="start:about")])
        markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        default_start_text = (
            "<b>✨ ʜᴇʟʟᴏ</b> <i>{mention}</i>\n\n"
            "ɪ'm {bot_mention} ꜰɪʟᴇ ʙᴏᴛ ʙʏ <b><a href=\"https://t.me/lexianime\">Lexi Anime</a></b>.\n"
            "⚡ ᴛᴀᴘ ᴛʜᴇ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ ᴛᴏ ᴅɪꜱᴄᴏᴠᴇʀ ᴍᴏʀᴇ ᴅᴇᴛᴀɪʟꜱ!"
        )
        raw_text = normalize_telegram_text(ensure_default_quote_format(cfg.get("text") or default_start_text, "start"))
        me = await message.bot.get_me()
        text = render_common_variables(raw_text, user, me)
        if cfg.get("poster_file_id"):
            await message.answer_animation(cfg["poster_file_id"], caption=text, reply_markup=markup, parse_mode="HTML")
        elif cfg.get("poster_photo_id"):
            await message.answer_photo(cfg["poster_photo_id"], caption=text, reply_markup=markup, parse_mode="HTML")
        else:
            await message.answer(text, reply_markup=markup, parse_mode="HTML")
    await delete_start_command_after(message, database.db)

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
    # Send a fresh About post instead of editing the Start post (which can show Edited).
    try:
        if cfg.get("poster_file_id"):
            replacement = await query.bot.send_animation(
                query.from_user.id, cfg["poster_file_id"], caption=rendered,
                parse_mode="HTML", reply_markup=back_markup,
            )
        elif cfg.get("poster_photo_id"):
            replacement = await query.bot.send_photo(
                query.from_user.id, cfg["poster_photo_id"], caption=rendered,
                parse_mode="HTML", reply_markup=back_markup,
            )
        else:
            replacement = await query.bot.send_message(
                query.from_user.id, rendered, parse_mode="HTML", reply_markup=back_markup,
            )
        try:
            await query.message.delete()
        except TelegramAPIError:
            pass
    except TelegramAPIError:
        log.warning("Could not send replacement About post", exc_info=True)

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
    url3 = cfg.get("button3_url", "")
    label3 = cfg.get("button3_text", "")
    parsed3 = urlparse(url3)
    button3 = None
    if cfg.get("button3_enabled", False) and label3 and parsed3.scheme in ("http", "https") and parsed3.netloc:
        button3 = InlineKeyboardButton(text=label3, url=url3)
    if configured_buttons:
        rows.append(configured_buttons)
    if button3:
        rows.append([button3])
    if cfg.get("about_enabled", True):
        rows.append([InlineKeyboardButton(text=cfg.get("about_button_text", "ᴀʙᴏᴜᴛ"), callback_data="start:about")])
    markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    raw = cfg.get("text") or '<b>✨ ʜᴇʟʟᴏ</b> <i>{mention}</i>\n\nɪ\'m {bot_mention} ꜰɪʟᴇ ʙᴏᴛ ʙʏ <b><a href="https://t.me/lexianime">Lexi Anime</a></b>.\n⚡ ᴛᴀᴘ ᴛʜᴇ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ ᴛᴏ ᴅɪꜱᴄᴏᴠᴇʀ ᴍᴏʀᴇ ᴅᴇᴛᴀɪʟꜱ!'
    rendered = render_common_variables(raw, user, me)
    # Re-send the main Start post and remove About instead of editing in place.
    # This avoids Telegram's Edited label on both text and media messages.
    try:
        if cfg.get("poster_file_id"):
            replacement = await query.bot.send_animation(
                user.id, cfg["poster_file_id"], caption=rendered,
                parse_mode="HTML", reply_markup=markup,
            )
        elif cfg.get("poster_photo_id"):
            replacement = await query.bot.send_photo(
                user.id, cfg["poster_photo_id"], caption=rendered,
                parse_mode="HTML", reply_markup=markup,
            )
        else:
            replacement = await query.bot.send_message(
                user.id, rendered, parse_mode="HTML", reply_markup=markup,
            )
        try:
            await msg.delete()
        except TelegramAPIError:
            pass
    except TelegramAPIError:
        log.warning("Could not send replacement Start post", exc_info=True)

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
    bot = getattr(event, "bot", None)
    if bot is not None:
        _schedule_used_force_join_link_cleanup(bot, database.db, event.chat.id, getattr(event, "invite_link", None))
    # Telegram emits this only when the bot has the appropriate channel admin rights.
    await database.db.pending_join_requests.update_one(
        {"chat_id": event.chat.id, "user_id": event.from_user.id},
        {"$set": {"chat_id": event.chat.id, "user_id": event.from_user.id,
                  "requested_at": datetime.now(timezone.utc)}},
        upsert=True,
    )

@router.chat_member()
async def clear_accepted_private_join_request(event, database):
    bot = getattr(event, "bot", None)
    if bot is not None:
        _schedule_used_force_join_link_cleanup(bot, database.db, event.chat.id, getattr(event, "invite_link", None))
    """Drop a saved request only when a private-channel request becomes membership."""
    if not _is_private_chat(event.chat):
        return
    if _is_member_status(event.old_chat_member) or not _is_member_status(event.new_chat_member):
        return
    await database.db.pending_join_requests.delete_one(
        {"chat_id": event.chat.id, "user_id": event.new_chat_member.user.id}
    )


async def _send_force_join_post(bot, user_id: int, cfg: dict, text: str, markup: InlineKeyboardMarkup):
    """Send a replacement Force Sub post before removing the previous one."""
    try:
        if cfg.get("poster_file_id"):
            return await bot.send_animation(user_id, cfg["poster_file_id"], caption=text, reply_markup=markup, parse_mode="HTML")
        if cfg.get("poster_photo_id"):
            return await bot.send_photo(user_id, cfg["poster_photo_id"], caption=text, reply_markup=markup, parse_mode="HTML")
        return await bot.send_message(user_id, text, reply_markup=markup, parse_mode="HTML")
    except TelegramAPIError:
        log.warning("Configured Force Sub poster could not be sent; using text post", exc_info=True)
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
        log.warning("Could not edit animation poster into Force Sub prompt", exc_info=True)
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
    target = await resolve_force_join_button_target(query.bot, database.db, channel, channel_cfg, query.from_user.id)
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
            log.warning("Could not replace repaired Force Sub URL button", exc_info=True)
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
        await query.answer("ᴠᴇʀɪꜰʏɪɴɢ...", show_alert=False)
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
    animation_mode = animation_cfg.get("mode", "poster")
    animation_post = None

    # In poster mode, the configured Force Sub media must remain visible during
    # verification. Editing a text-only old post would otherwise silently skip it.
    if old_post and animation_enabled:
        if animation_mode == "message":
            await _start_gate_animation(
                bot, user_id, payload, old_post, "",
                InlineKeyboardMarkup(inline_keyboard=[]), animation_cfg,
            )
        else:
            frames = animation_cfg.get("frames")
            if not isinstance(frames, list) or not 1 <= len(frames) <= 6: frames = list(DEFAULT_ANIMATION_FRAMES)
            first = animation_frame_text(frames[0])
            empty_markup = InlineKeyboardMarkup(inline_keyboard=[])
            try:
                if force_cfg.get("poster_file_id"):
                    animation_post = await bot.send_animation(
                        user_id, force_cfg["poster_file_id"],
                        caption=first, parse_mode="HTML",
                    )
                elif force_cfg.get("poster_photo_id"):
                    animation_post = await bot.send_photo(
                        user_id, force_cfg["poster_photo_id"],
                        caption=first, parse_mode="HTML",
                    )
                else:
                    animation_post = None
                # Poster mode updates frames in this same media caption, never a separate text message.
                if animation_post:
                    await _start_gate_animation(
                        bot, user_id, payload, animation_post, "", empty_markup,
                        animation_cfg, animation_only=True,
                    )
            except TelegramAPIError:
                log.warning("Could not create Force Sub animation poster", exc_info=True)
                animation_post = None

    missing = [] if await is_owner_or_moderator(database.db, user_id, settings) else await join_status(
        bot, user_id, channels, database.db
    )
    if missing:
        join_buttons = []
        channel_cfgs = await asyncio.gather(*(
            database.db.settings.find_one({"key": "force_join_channel", "channel": channel})
            for channel in missing
        ))
        channel_cfgs = [item or {} for item in channel_cfgs]
        # Verification refreshes expired join links without changing its own callback.
        targets = await asyncio.gather(*(
            resolve_force_join_button_target(bot, database.db, channel, channel_cfg, user_id)
            for channel, channel_cfg in zip(missing, channel_cfgs)
        ))
        for index, (channel, channel_cfg, target) in enumerate(zip(missing, channel_cfgs, targets)):
            button = build_force_join_channel_button(channel_cfg, index + 1, target, channel)
            if button:
                join_buttons.append(button)
        rows = [join_buttons[i:i+2] for i in range(0, len(join_buttons), 2)]
        rows.append([InlineKeyboardButton(text=force_cfg.get("verify_text") or "♻️ ʀᴇᴛʀʏ", callback_data=f"forcejoin:check:{payload}")])
        me = await bot.get_me()
        raw_text = ensure_default_quote_format(force_cfg.get("message") or "⚡ ʜᴇʏ {mention}, ᴘʟᴇᴀꜱᴇ ᴊᴏɪɴ ᴀʟʟ ʀᴇQᴜɪʀᴇᴅ ᴄʜᴀɴɴᴇʟꜱ ᴛᴏ ᴜɴʟᴏᴄᴋ ᴀᴄᴄᴇꜱꜱ!\\n\\nᴛᴀᴘ <b>『 ʀᴇᴛʀʏ 』</b> ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ ᴛᴏ ᴠᴇʀɪꜰʏ.", "force_join")
        rendered = render_common_variables(normalize_telegram_text(raw_text), query.from_user, me).replace("{remaining_count}", str(len(missing)))
        markup = InlineKeyboardMarkup(inline_keyboard=rows)

        # Wait only for Message mode's minimum cycle, after verification work has run.
        # Poster mode has no minimum-cycle limit and is replaced immediately when ready.
        if animation_enabled:
            await _wait_for_gate_animation_minimum_cycle(user_id, payload, animation_mode)
        # Stop the animation, then edit its poster in place with the current
        # remaining-channel buttons and configured Force Sub message.
        if animation_mode == "message":
            separate_animation_id = await _cancel_gate_animation(
                bot, user_id, payload, delete_separate=False
            )
        else:
            separate_animation_id = await _cancel_gate_animation(bot, user_id, payload)
        if animation_enabled and animation_mode == "poster" and animation_post:
            # The same animated poster becomes the final Force Join post.
            await _edit_force_join_post(animation_post, rendered, markup)
            if old_post and old_post.message_id != animation_post.message_id:
                try:
                    await old_post.delete()
                except TelegramAPIError:
                    pass
            return

        if animation_enabled and animation_mode == "message":
            # Publish the final Force Sub post and remove the animation frame concurrently.
            async def delete_message_animation():
                if separate_animation_id:
                    try:
                        await bot.delete_message(chat_id=user_id, message_id=separate_animation_id)
                    except TelegramAPIError:
                        pass
            new_post, _ = await asyncio.gather(
                _send_force_join_post(bot, user_id, force_cfg, rendered, markup),
                delete_message_animation(),
            )
            if old_post and old_post.message_id != new_post.message_id:
                try:
                    await old_post.delete()
                except TelegramAPIError:
                    pass
            return

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

    # Verification succeeded: Message mode completes its minimum cycle; Poster mode does not wait.
    # Then remove the Force Sub prompt before sending the requested file or batch item.
    if animation_enabled:
        await _wait_for_gate_animation_minimum_cycle(user_id, payload, animation_mode)
    separate_animation_id = await _cancel_gate_animation(
        bot, user_id, payload, delete_separate=False
    )
    if animation_post and animation_post.message_id != (old_post.message_id if old_post else None):
        try:
            await animation_post.delete()
        except TelegramAPIError:
            log.info("Could not remove Force Sub verification animation")
    repo = FileRepository(database.db)
    # Files and batches share the same collection and Deepcode lookup.
    record = await repo.get_file(payload[:5]) if len(payload) == 20 and payload.isalnum() else None
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
    # Delete the Force Sub post first. Await this API call before beginning delivery,
    # so the verification prompt cannot appear after the file in normal operation.
    if old_post:
        try:
            await old_post.delete()
        except TelegramAPIError:
            log.warning("Could not remove Force Sub post before delivery", exc_info=True)
    if separate_animation_id:
        try:
            await bot.delete_message(chat_id=user_id, message_id=separate_animation_id)
        except TelegramAPIError:
            pass
    success = False
    try:
        # Show and clear the blink before the file/batch is sent.
        await send_file_animation(bot, database.db, user_id)
        result=await deliver_payload(bot,database,settings,user_id,payload,record,request_key=request_key)
        is_batch = isinstance(record.get("items"), list)
        if is_batch and result[0]==0:
            await bot.send_message(user_id,"None of the batch files could be delivered.")
        elif not is_batch and result[0] == 0:
            await bot.send_message(user_id,"This file is currently unavailable. The source message may have been deleted or become inaccessible.")
        success = result[0] > 0
    except TelegramAPIError:
        log.exception("Delivery after Force Sub failed")
        await bot.send_message(user_id,"File delivery failed. Contact the owner.")
    except Exception:
        log.exception("Unexpected delivery failure after Force Sub")
        await bot.send_message(user_id,"Delivery failed due to a temporary error.")
    finally:
        try: await finish_delivery_request(database.db, request_key, success)
        except Exception: log.exception("Could not update Force Sub delivery request status")


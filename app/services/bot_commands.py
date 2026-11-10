"""Role-based Telegram command menus for private chats."""
import asyncio
import logging

from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

log = logging.getLogger(__name__)

# Everyone else sees only /start. More specific per-chat scopes override this.
USER_COMMANDS = [
    BotCommand(command="start", description="Start the adventure ✨"),
]

# Moderators added with /addmod get the operational menu, never owner-only controls.
MODERATOR_COMMANDS = [
    BotCommand(command="start", description="Start the adventure ✨"),
    BotCommand(command="settings", description="Open bot settings"),
    BotCommand(command="file", description="Generate a permanent file link"),
    BotCommand(command="batch", description="Generate a batch link for multiple files"),
    BotCommand(command="cancel", description="Cancel the current operation"),
    BotCommand(command="shortener", description="Shorten a URL"),
    BotCommand(command="ban", description="Ban a user by Telegram ID"),
    BotCommand(command="unban", description="Unban a user by Telegram ID"),
    BotCommand(command="stats", description="View bot statistics"),
    BotCommand(command="broadcast", description="Broadcast a message to users"),
]

# Owners configured through Render environment variables get every listed command.
OWNER_COMMANDS = [
    *MODERATOR_COMMANDS,
    BotCommand(command="addmod", description="Add a moderator (owner only)"),
    BotCommand(command="delmod", description="Remove a moderator (owner only)"),
]


async def set_user_commands(bot, user_id: int) -> None:
    """Reset a chat to the public command menu (also used after /delmod)."""
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeChat(chat_id=user_id))


async def set_moderator_commands(bot, user_id: int) -> None:
    """Apply the moderator menu immediately after /addmod."""
    await bot.set_my_commands(MODERATOR_COMMANDS, scope=BotCommandScopeChat(chat_id=user_id))


async def configure_bot_commands(bot, settings, db) -> None:
    """Apply the public default and per-chat owner/moderator command menus."""
    try:
        await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        owner_ids = set(settings.owner_ids)
        moderator_ids = set()
        async for moderator in db.moderators.find({}, {"user_id": 1}):
            user_id = moderator.get("user_id")
            if isinstance(user_id, int) and user_id > 0 and user_id not in owner_ids:
                moderator_ids.add(user_id)

        for user_id in sorted(moderator_ids):
            try:
                await set_moderator_commands(bot, user_id)
            except Exception:
                log.warning("Could not register moderator command menu for chat %s", user_id, exc_info=True)
        for user_id in sorted(owner_ids):
            try:
                await bot.set_my_commands(OWNER_COMMANDS, scope=BotCommandScopeChat(chat_id=user_id))
            except Exception:
                log.warning("Could not register owner command menu for chat %s", user_id, exc_info=True)

        log.info(
            "Telegram command menus registered: public=%s, moderators=%s, owners=%s",
            len(USER_COMMANDS), len(moderator_ids), len(owner_ids),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        # A Telegram menu API failure must not prevent bot startup.
        log.exception("Could not register Telegram command menus")

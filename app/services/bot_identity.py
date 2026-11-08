"""Process-local cache for Telegram bot identity."""

async def get_bot_identity(bot):
    """Return aiogram's cached bot identity, fetching it only on first use."""
    try:
        return bot.me
    except (AttributeError, RuntimeError):
        return await bot.get_me()

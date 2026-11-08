"""Process-local cache for Telegram bot identity."""

async def get_bot_identity(bot):
    """Return the bot identity across aiogram versions exposing me as a value or method."""
    try:
        identity = bot.me
    except (AttributeError, RuntimeError):
        identity = None

    # Some aiogram versions expose me as an async method instead of a
    # cached User property. Resolve that method before callers read .username.
    if callable(identity):
        identity = identity()
        if hasattr(identity, "__await__"):
            identity = await identity

    return identity if identity is not None else await bot.get_me()

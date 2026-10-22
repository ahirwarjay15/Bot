"""Mirror generated deeplinks and their formatted captions to an optional channel."""
import logging

from aiogram.exceptions import TelegramAPIError

log = logging.getLogger(__name__)


async def mirror_deeplink(bot, settings, link, reply_markup, caption_items=()):
    """Post the same deeplink and formatted reply captions to the configured channel.

    Failures here are logged but must never invalidate a link already delivered to
    the owner in DM. Caption entities are passed through unchanged, preserving
    bold/italic/links/custom emoji and other Telegram formatting.
    """
    channel_id = getattr(settings, "deeplink_channel_id", None)
    if not channel_id:
        return
    try:
        link_message = await bot.send_message(
            chat_id=channel_id,
            text=link,
            reply_markup=reply_markup,
            link_preview_options={"is_disabled": True},
        )
        for text, entities in caption_items:
            if not text:
                continue
            await bot.send_message(
                chat_id=channel_id,
                text=text,
                entities=entities or None,
                parse_mode=None,
                reply_to_message_id=link_message.message_id,
            )
    except TelegramAPIError:
        log.exception("Could not mirror deeplink to DEEPLINK_CHANNEL=%s", channel_id)

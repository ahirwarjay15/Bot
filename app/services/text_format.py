"""Normalize user-facing start-message text for Telegram HTML parse mode."""
import re


def normalize_telegram_text(value: str | None) -> str:
    """Support common Markdown markers while preserving Telegram HTML templates."""
    text = str(value or "")
    # Repair legacy defaults that stored a literal backslash-n instead of a newline.
    text = text.replace("\\\\n", "\n").replace("\\n", "\n")
    # Convert common Markdown formatting so markers are never shown to users.
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", text)
    return text

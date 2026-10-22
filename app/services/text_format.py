"""Normalize Markdown-like user text into Telegram HTML without exposing markers."""
import re
from html import escape


def normalize_telegram_text(value: str | None) -> str:
    """Convert supported Markdown to Telegram HTML; leave formatting markers invisible."""
    text = str(value or "")
    # Repair legacy values that may contain one or more literal backslashes before n.\n    text = re.sub(r"\\\\+n", "\\n", text)
    code_blocks = []

    def save_code(match):
        code_blocks.append("<pre>" + escape(match.group(1).strip("\n")) + "</pre>")
        return f"\x00CODE{len(code_blocks) - 1}\x00"

    text = re.sub(r"```\s*\n?(.*?)\n?```", save_code, text, flags=re.DOTALL)
    text = re.sub(r"(?m)^> ?", "", text)
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text, flags=re.DOTALL)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", text)
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00CODE{i}\x00", block)
    return text

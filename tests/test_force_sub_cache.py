import pytest
from types import SimpleNamespace

from app.handlers.settings import build_forcejoin_page


@pytest.mark.asyncio
async def test_force_sub_settings_uses_cached_channel_titles_without_telegram_reads():
    class SettingsCollection:
        async def find_one(self, query):
            if query.get("key") == "force_join":
                return {"channels": ["-100123"], "channels_override": True, "enabled": True}
            if query.get("key") == "force_join_channel":
                return {
                    "channel": "-100123",
                    "channel_title": "Cached Private Channel",
                    "button_url": "https://t.me/+stable",
                    "metadata_cached_at": "cached",
                }
            return None

        async def update_one(self, *args, **kwargs):
            return None

    class Database:
        settings = SettingsCollection()

    class Bot:
        async def get_chat(self, chat_id):
            raise AssertionError("Opening Force Sub settings must not call Telegram get_chat")

    text, markup = await build_forcejoin_page(
        Bot(), Database(), SimpleNamespace(force_join_channels=[])
    )
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "Cached Private Channel" in labels
    assert "FORCE SUB\\n\\n" not in text

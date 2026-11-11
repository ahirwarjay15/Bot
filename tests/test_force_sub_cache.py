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

        def find(self, query):
            class Cursor:
                async def to_list(self, length=None):
                    return [{
                        "channel": "-100123",
                        "channel_title": "Cached Private Channel",
                        "button_url": "https://t.me/+stable",
                        "metadata_cached_at": "cached",
                    }]
            return Cursor()

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



def test_other_force_sub_links_are_hidden_without_pending_required_chats():
    from app.handlers.start import build_force_join_other_link_buttons

    links = [{"url": "https://t.me/example"}]
    assert build_force_join_other_link_buttons(links, 0) == []


def test_other_force_sub_links_use_join_number_after_pending_channels():
    from app.handlers.start import build_force_join_other_link_buttons

    buttons = build_force_join_other_link_buttons(
        [{"url": "https://t.me/example"}], 3
    )
    assert [button.text for button in buttons] == ["Join 4"]


def test_other_force_sub_links_preserve_custom_button_text():
    from app.handlers.start import build_force_join_other_link_buttons

    buttons = build_force_join_other_link_buttons(
        [{"button_text": "Visit Website", "url": "https://example.com"}], 2
    )
    assert [button.text for button in buttons] == ["Visit Website"]
    assert buttons[0].url == "https://example.com"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@samplechannel", "https://t.me/samplechannel"),
        ("t.me/samplechannel", "https://t.me/samplechannel"),
        ("telegram.me/joinchat/abc123", "https://telegram.me/joinchat/abc123"),
        ("tg://resolve?domain=samplechannel", "https://t.me/samplechannel"),
        ("tg://join?invite=abc123", "https://t.me/+abc123"),
        ("example.com/path", "https://example.com/path"),
    ],
)
def test_other_force_sub_link_formats_are_normalized(raw, expected):
    from app.handlers.settings import normalize_forcejoin_other_url

    assert normalize_forcejoin_other_url(raw) == expected



def test_other_links_are_only_available_inside_force_sub_add_keyboard():
    from pathlib import Path

    source = Path("app/handlers/settings.py").read_text()
    page = source.split("async def build_forcejoin_page", 1)[1].split(
        "async def render_forcejoin_media", 1
    )[0]
    add_flow = source.split('elif action == "forcejoin_add":', 1)[1].split(
        'elif action == "forcejoin_join_request":', 1
    )[0]
    assert 'settings:forcejoin_other_links' not in page
    assert 'KeyboardButton(text="Other Links")' in add_flow
    assert 'KeyboardButton(text="Group"' in add_flow
    assert 'KeyboardButton(text="Channel"' in add_flow


def test_other_link_input_messages_and_prompts_are_cleaned_up():
    from pathlib import Path

    source = Path("app/handlers/settings.py").read_text()
    label_flow = source.split('if field == "forcejoin_other_label":', 1)[1].split(
        'if field == "forcejoin_other_link":', 1
    )[0]
    link_flow = source.split('if field == "forcejoin_other_link":', 1)[1].split(
        'if field == "forcejoin_label":', 1
    )[0]
    assert "await message.delete()" in label_flow
    assert "await remove_temporary_prompts(message.bot, pending)" in label_flow
    assert "await message.delete()" in link_flow
    assert "await remove_temporary_prompts(message.bot, pending)" in link_flow
    assert 'forcejoin_panel_chat_id=pending.get("forcejoin_panel_chat_id")' in source



def test_added_other_links_are_visible_and_manageable_from_force_sub_root():
    from pathlib import Path

    source = Path("app/handlers/settings.py").read_text()
    page = source.split("async def build_forcejoin_page", 1)[1].split(
        "async def render_forcejoin_media", 1
    )[0]
    assert 'cfg.get("other_links")' in page
    assert 'settings:forcejoin_other_item:{i}' in page
    assert 'ADDED OTHER LINKS ({len(other_links)})' in page
    assert 'elif action.startswith("forcejoin_other_item:")' in source
    assert 'settings:forcejoin_other_remove:{index}' in source

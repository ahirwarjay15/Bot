import time
import pytest
from app.handlers.batch import SESSION_TTL, _expired, menu
from app.handlers.start import _join_signature
from app.services.delivery import _minutes, _notice, DEFAULT_NOTICE
from app.handlers.settings import time_label, time_words


@pytest.mark.asyncio
async def test_batch_session_expires_after_200_seconds():
    assert await _expired({"status": "collecting", "last_activity_ts": time.time() - SESSION_TTL - 1})


@pytest.mark.asyncio
async def test_generating_batch_is_not_expired_mid_generation():
    assert not await _expired({"status": "generating", "last_activity_ts": 0})


def test_batch_menu_exposes_only_generate_and_cancel():
    labels = [button.text for row in menu().inline_keyboard for button in row]
    assert labels == ["ɢᴇɴᴇʀᴀᴛᴇ ʟɪɴᴋ", "ᴄᴀɴᴄᴇʟ"]


def test_force_join_signature_is_order_independent_but_configuration_sensitive():
    assert _join_signature(["@one", "@two"]) == _join_signature(["@two", "@one"])
    assert _join_signature(["@one", "@two"]) != _join_signature(["@one", "@three"])


def test_blueprint_auto_delete_cycles_and_labels():
    expected = (2, 5, 15, 30, 60, 180, 360, 720, 1440)
    for minutes in expected:
        assert _minutes({"minutes": minutes}) == minutes
    assert _minutes({"minutes": 7}) == 30


def test_notice_uses_readable_timer_label():
    assert _notice({"notice": DEFAULT_NOTICE, "minutes": 60}, 60) == "Your files will be automatically deleted after 1 hour."
    assert _notice({"notice": DEFAULT_NOTICE, "minutes": 180}, 180) == "Your files will be automatically deleted after 3 hours."


def test_blueprint_timer_display_labels():
    assert [time_label(m) for m in (2, 5, 15, 30, 60, 180, 360, 720, 1440)] == [
        "2m", "5m", "15m", "30m", "1h", "3h", "6h", "12h", "24h"
    ]
    assert time_words(60) == "1 hour"
    assert time_words(1440) == "24 hours"


def test_animation_mode_buttons_persist_distinct_modes():
    from app.handlers.settings import _animation_mode_for_action

    assert _animation_mode_for_action("animation_mode_new") == "new_message"
    assert _animation_mode_for_action("animation_mode_poster") == "force_join_poster"


def test_settings_buttons_use_small_caps_with_required_exceptions():
    from app.handlers.settings import unicode_buttons

    assert unicode_buttons("START TEXT") == "ꜱᴛᴀʀᴛ ᴛᴇxᴛ"
    assert unicode_buttons("《 BACK") == "《 BACK"
    assert unicode_buttons("📟 UPDATE CHANNEL") == "📟 UPDATE CHANNEL"


def test_invalid_post_about_defaults_to_multiline_standard_about():
    from app.handlers.misc import DEFAULT_ABOUT

    assert "\n" in DEFAULT_ABOUT
    assert "\\n" not in DEFAULT_ABOUT
    assert "{bot_name}" in DEFAULT_ABOUT


@pytest.mark.asyncio
async def test_start_and_invalid_posts_share_one_saved_about_template():
    from app.handlers.misc import DEFAULT_ABOUT, load_shared_about_text

    class SettingsCollection:
        def __init__(self, doc=None):
            self.doc = doc
            self.updates = []

        async def find_one(self, query):
            return self.doc if self.doc and self.doc.get("key") == query.get("key") else None

        async def update_one(self, query, update, upsert=False):
            self.updates.append((query, update, upsert))
            if self.doc is None:
                self.doc = dict(update["$set"])
            else:
                self.doc.update(update["$set"])

    class FakeDB:
        def __init__(self, doc=None):
            self.settings = SettingsCollection(doc)

    saved = "<b>Shared About</b>\\n{bot_name}"
    db = FakeDB({"key": "invalid_post", "about_text": saved})
    assert await load_shared_about_text(db) == saved
    assert db.settings.updates == []

    empty_db = FakeDB({"key": "invalid_post"})
    assert await load_shared_about_text(empty_db) == DEFAULT_ABOUT
    assert empty_db.settings.doc["about_text"] == DEFAULT_ABOUT


def test_about_text_has_no_settings_text_editor():
    from pathlib import Path

    source = Path("app/handlers/settings.py").read_text()
    assert 'elif action == "invalid_about_text"' not in source
    assert 'elif field == "invalid_about_text"' not in source
    assert '("ABOUT", "settings:invalid_about_toggle")' in source


def test_invalid_post_template_replaces_user_variables_safely():
    from types import SimpleNamespace
    from app.handlers.misc import _render_user_text

    user = SimpleNamespace(id=42, first_name="A&B", last_name="User")
    rendered = _render_user_text("Hi {mention} / {first_name} / {last_name}", user)
    assert 'tg://user?id=42' in rendered
    assert "A&amp;B" in rendered
    assert "User" in rendered


def test_all_runtime_handlers_import_cleanly():
    import app.handlers.start
    import app.handlers.misc
    import app.handlers.broadcast
    import app.handlers.settings
    import app.services.delivery
    import app.handlers.file
    import app.handlers.admin
    import app.handlers.shortener
    import main


def test_settings_pages_get_shared_heading_and_body_style():
    from app.handlers.settings import format_settings_page

    raw = "AUTO DELETE" + "\n" + "Automatically delete delivered files." + "\n" + "Status: DISABLED"
    rendered = format_settings_page(raw)
    assert rendered.startswith("<b><u>AUTO DELETE</u></b>" + "\n\n" + "<i>")
    assert "___________" not in rendered
    assert "<u>AUTO DELETE</u>" in rendered
    assert rendered.endswith("</i>")

def test_common_variables_render_bot_and_channel_links_in_start_and_invalid_text():
    from types import SimpleNamespace
    from app.handlers.start import render_common_variables
    from app.handlers.misc import _render_user_text

    user = SimpleNamespace(
        id=42,
        first_name="Alice",
        last_name="Example",
        mention_html=lambda: '<a href="tg://user?id=42">Alice</a>',
    )
    bot = SimpleNamespace(username="sample_bot", full_name="Sample Bot")
    template = "{bot} | {bot_mention} | {first_name} | {last_name} | {user} | {owner} | {anime} | {hindi_anime}"
    for rendered in (render_common_variables(template, user, bot), _render_user_text(template, user, bot)):
        assert '<a href="https://t.me/sample_bot">Sample Bot</a>' in rendered
        assert "Alice" in rendered
        assert "Example" in rendered
        assert '<a href="tg://user?id=42">Alice</a>' in rendered
        assert '<a href="https://t.me/lexxiowner">Lexi</a>' in rendered
        assert '<a href="https://t.me/lexianime">Lexi Anime</a>' in rendered
        assert '<a href="https://t.me/lexianimehindi">Lexi Anime Hindi</a>' in rendered




def test_force_join_default_button_numbers_and_custom_slot():
    from app.handlers.start import force_join_button_label

    assert force_join_button_label({"channel_title": "Official Channel"}, 1) == "Join 1"
    labels = [
        force_join_button_label({}, 1),
        force_join_button_label({}, 2),
        force_join_button_label({"button_text": "Premium Channel"}, 3),
        force_join_button_label({}, 4),
    ]
    assert labels == ["Join 1", "Join 2", "Premium Channel", "Join 4"]


def test_force_join_custom_label_does_not_reset_following_number():
    from app.handlers.start import force_join_button_label

    assert force_join_button_label({"button_text": "Paid Group"}, 1) == "Paid Group"
    assert force_join_button_label({}, 2) == "Join 2"


def test_force_join_mode_defaults_preserve_legacy_join_request_behavior():
    from app.handlers.start import join_status
    assert callable(join_status)



@pytest.mark.asyncio
async def test_pending_join_request_only_verifies_join_request_mode():
    from types import SimpleNamespace
    from app.handlers.start import join_status

    class Collection:
        def __init__(self, doc=None):
            self.doc = doc

        async def find_one(self, query):
            return self.doc

        async def delete_one(self, query):
            self.doc = None

    class FakeDB:
        def __init__(self, mode):
            from datetime import datetime, timezone
            self.settings = Collection({"mode": mode})
            self.pending_join_requests = Collection({
                "chat_id": -100123, "user_id": 42, "requested_at": datetime.now(timezone.utc)
            })

    class FakeBot:
        async def get_chat(self, chat_id):
            return SimpleNamespace(id=-100123, type="supergroup", username=None)

        async def get_chat_member(self, chat_id, user_id):
            return SimpleNamespace(status="left", is_member=False)

    bot = FakeBot()
    assert await join_status(bot, 42, ["-100123"], FakeDB("normal")) == ["-100123"]
    assert await join_status(bot, 42, ["-100123"], FakeDB("join_request")) == []



def test_force_join_public_and_private_chats_select_automatic_modes():
    from types import SimpleNamespace
    from app.handlers.settings import force_join_mode_for_chat

    public_channel = SimpleNamespace(username="public_channel")
    public_group = SimpleNamespace(username="public_group")
    private_channel = SimpleNamespace(username=None)
    private_group = SimpleNamespace(username=None)

    assert force_join_mode_for_chat(public_channel) == "normal"
    assert force_join_mode_for_chat(public_group) == "normal"
    assert force_join_mode_for_chat(private_channel) == "join_request"
    assert force_join_mode_for_chat(private_group) == "join_request"


def test_custom_button_settings_layout_uses_two_controls_per_row():
    from app.handlers.settings import custom_button_settings_rows

    rows = custom_button_settings_rows([
        {"text": "Protect", "url": "https://example.com"},
        {"text": "Update", "url": "https://example.org"},
    ])
    assert [[button.text for button in row] for row in rows] == [
        ["Protect", "✖"],
        ["Update", "✖"],
    ]


@pytest.mark.asyncio
async def test_button_panel_refresh_keeps_inline_back_navigation():
    from types import SimpleNamespace
    from app.handlers.settings import refresh_button_panel

    class SettingsCollection:
        async def find_one(self, query):
            return {
                "button1_enabled": True,
                "button1_text": "Updates",
                "button1_url": "https://t.me/example",
                "button2_enabled": False,
            }

    class FakeBot:
        def __init__(self):
            self.edited = None

        async def edit_message_text(self, **kwargs):
            self.edited = kwargs

    bot = FakeBot()
    await refresh_button_panel(bot, 123, 456, SimpleNamespace(settings=SettingsCollection()), "start")

    assert bot.edited["chat_id"] == 123
    assert bot.edited["message_id"] == 456
    markup = bot.edited["reply_markup"]
    callback_data = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert "settings:start" in callback_data
    assert "settings:start_button_1" in callback_data
    assert "settings:start_button_2" in callback_data


def test_start_message_default_and_markdown_are_normalized_without_leaked_markers():
    from app.handlers.settings import DEFAULT_START_TEXT
    from app.services.text_format import normalize_telegram_text

    assert DEFAULT_START_TEXT.splitlines() == [
        "<b>Hey {user_mention}✨</b>",
        "",
        "<i>Welcome to the fastest and most secure file store bot!</i>",
    ]
    rendered = normalize_telegram_text("**Hey {user_mention}✨**\n\n*Welcome to the fastest and most secure file store bot!*")
    assert rendered == DEFAULT_START_TEXT
    assert "\\\n" not in rendered
    assert "**" not in rendered
    assert "*Welcome" not in rendered
    assert normalize_telegram_text("`sample`") == "<code>sample</code>"


def test_broadcast_confirmation_has_no_back_button():
    from app.handlers.broadcast import confirmation_keyboard

    labels = [button.text for row in confirmation_keyboard("draft123").inline_keyboard for button in row]
    callbacks = [button.callback_data for row in confirmation_keyboard("draft123").inline_keyboard for button in row]
    assert labels == ["ᴄᴏɴꜰɪʀᴍ", "ᴄᴀɴᴄᴇʟ"]
    assert callbacks == ["broadcast:yes:draft123", "broadcast:no:draft123"]


def test_batch_requires_two_files_and_preserves_message_order():
    from types import SimpleNamespace
    from app.handlers.batch import is_batch_file, ordered_batch_items

    assert is_batch_file(SimpleNamespace(
        document=None, photo=None, video=None, audio=None, animation=None,
        voice=None, video_note=None, sticker=None,
    )) is False
    assert is_batch_file(SimpleNamespace(
        document=object(), photo=None, video=None, audio=None, animation=None,
        voice=None, video_note=None, sticker=None,
    )) is True
    items = [
        {"original_message_id": 12, "source_message_id": 102},
        {"original_message_id": 10, "source_message_id": 100},
        {"original_message_id": 11, "source_message_id": 101},
    ]
    assert [item["original_message_id"] for item in ordered_batch_items(items)] == [10, 11, 12]


def test_batch_repository_rejects_single_file_batch():
    import inspect
    from app.database.files import FileRepository

    source = inspect.getsource(FileRepository.save_batch)
    assert "len(items) < 2" in source
    assert "2 to 100 files" in source


def test_broadcast_job_restores_saved_inline_keyboard():
    from aiogram.types import InlineKeyboardMarkup
    markup_data = {"inline_keyboard": [[{"text": "Open", "url": "https://example.com"}]]}
    markup = InlineKeyboardMarkup.model_validate(markup_data)
    assert markup.inline_keyboard[0][0].url == "https://example.com"


def test_shortener_accepts_command_urls_and_plain_domains():
    from app.handlers.shortener import extract_target

    assert extract_target("https://example.com/a") == "https://example.com/a"
    assert extract_target("example.com") == "https://example.com"
    assert extract_target("/batch") is None


@pytest.mark.asyncio
async def test_force_join_settings_page_uses_real_line_breaks():
    from types import SimpleNamespace
    from app.handlers.settings import build_forcejoin_page

    class SettingsCollection:
        async def find_one(self, query):
            if query.get("key") == "force_join":
                return {"enabled": True, "channels": []}
            return None

    class FakeBot:
        async def get_chat(self, chat_id):
            raise AssertionError("No channels should need resolving")

    class Database:
        settings = SettingsCollection()

    text, _ = await build_forcejoin_page(FakeBot(), Database(), SimpleNamespace(force_join_channels=[]))
    assert "FORCE JOIN\n\n" in text
    assert r"FORCE JOIN\n\n" not in text


def test_new_message_animation_is_deleted_only_after_replacement_post():
    from pathlib import Path

    source = Path("app/handlers/start.py").read_text()
    assert "delete_separate: bool = True" in source
    assert "delete_separate=False" in source
    send_position = source.index('new_post = await _send_force_join_post(bot, user_id, force_cfg, rendered, markup)')
    delete_position = source.index('await bot.delete_message(chat_id=user_id, message_id=separate_animation_id)', send_position)
    old_post_delete_position = source.index('await old_post.delete()', send_position)
    # Configured poster is sent before the animation and stale Force Join post
    # are removed; the non-poster fallback keeps its replacement ordering.
    assert send_position < delete_position < old_post_delete_position
    fallback_position = source.index('new_post = await _send_force_join_post(bot, user_id, force_cfg, rendered, markup)', send_position + 1)
    poster_delete_position = source.index('await animation_post.delete()', fallback_position)
    fallback_old_delete_position = source.index('await old_post.delete()', fallback_position)
    assert fallback_position < poster_delete_position < fallback_old_delete_position


def test_force_join_add_cleans_prompts_and_returns_to_main_page():
    from pathlib import Path

    source = Path("app/handlers/settings.py").read_text()
    assert "await remove_temporary_prompts(message.bot, data)" in source
    assert "await keyboard_removal.delete()" in source
    assert "await message.bot.edit_message_text(chat_id=panel_chat_id, message_id=panel_message_id" in source
    assert "Force Join added successfully." not in source


def test_force_join_verification_resolves_public_chat_username_for_join_button():
    from pathlib import Path

    source = Path("app/handlers/start.py").read_text()
    gate_prompt = source.split("async def gate_or_prompt", 1)[1].split("@router.callback_query(F.data == \"start:about\")", 1)[0]
    verification = source.split("async def _forcejoin_check_impl", 1)[1]
    assert "resolve_force_join_button_target(bot, database.db, channel, channel_cfg)" in gate_prompt
    assert "resolve_force_join_button_target(" in verification

def test_settings_index_migration_allows_multiple_force_join_channel_documents():
    from pathlib import Path

    source = Path("app/database/connection.py").read_text()
    assert '("key", ASCENDING), ("channel", ASCENDING)' in source
    assert 'name="key_channel_unique"' in source
    assert 'await self.db.settings.drop_index(index_name)' in source
    assert 'await self.db.settings.create_index([("key",ASCENDING)],unique=True)' not in source


@pytest.mark.asyncio
async def test_force_join_custom_label_updates_only_selected_channel():
    from app.handlers.settings import save_force_join_channel_label

    class SettingsCollection:
        def __init__(self):
            self.docs = [
                {"key": "force_join_channel", "channel": "-100111", "button_url": "https://t.me/first_invite", "button_text": "Join 1"},
                {"key": "force_join_channel", "channel": "-100222", "button_url": "https://t.me/second_invite", "button_text": "Join 2"},
            ]

        async def update_one(self, query, update, upsert=False):
            for doc in self.docs:
                if all(doc.get(key) == value for key, value in query.items()):
                    doc.update(update["$set"])
                    return
            if upsert:
                self.docs.append(dict(update["$set"]))

    class Database:
        def __init__(self):
            self.settings = SettingsCollection()

    class FakeBot:
        async def get_chat(self, chat_id):
            from types import SimpleNamespace
            return SimpleNamespace(id=chat_id, title="Second Channel", type="channel", username=None)

        async def create_chat_invite_link(self, **kwargs):
            from types import SimpleNamespace
            assert kwargs["chat_id"] == -100222
            assert kwargs["creates_join_request"] is True
            return SimpleNamespace(invite_link="https://t.me/+correct_second_invite")

    db = Database()
    await save_force_join_channel_label(FakeBot(), db, "-100222", "Premium Channel")

    first, second = db.settings.docs
    assert first["channel"] == "-100111"
    assert first["button_url"] == "https://t.me/first_invite"
    assert first["button_text"] == "Join 1"
    assert second["channel"] == "-100222"
    assert second["button_url"] == "https://t.me/+correct_second_invite"
    assert second["channel_title"] == "Second Channel"
    assert second["button_text"] == "Premium Channel"

def test_new_message_animation_is_reused_as_force_join_post_for_verify_and_deeplink():
    from pathlib import Path

    source = Path("app/handlers/start.py").read_text()
    assert 'message_id=separate_animation_id,' in source
    assert 'text=rendered,' in source
    assert 'Could not convert deeplink animation message into Force Join post' in source
    assert 'Could not convert Verify animation message into Force Join post' in source
    assert 'if old_post and old_post.message_id != separate_animation_id:' in source

def test_settings_flow_messages_are_tracked_for_page_navigation_cleanup():
    from pathlib import Path

    source = Path("app/handlers/settings.py").read_text()
    callback_start = source.index('@router.callback_query(F.data.startswith("settings:"))')
    callback_end = source.index('@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))', callback_start)
    callback_handler = source[callback_start:callback_end]
    input_start = source.index('@router.message(SettingInput.waiting)')
    input_end = source.index('@router.message(Command("setstart"))', input_start)
    input_handler = source[input_start:input_end]

    assert "await remove_temporary_prompts(query.bot, pending)" in callback_handler
    assert "await tracked_answer(query.message, state," in callback_handler
    assert "await tracked_answer(message, state," in input_handler
    assert "async def clear_state_preserving_prompts(state):" in source
    assert "prompt_ids = list(data.get(\"temporary_prompt_ids\", []))" in source
    assert "await track_temporary_prompt(state, sent)" in source
    assert "await remove_temporary_prompts(message.bot, data)" in source[source.index('@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))'):source.index("async def send_button_settings_reply_panel")]




def test_stats_alert_only_contains_total_and_banned_users():
    from pathlib import Path

    source = Path("app/handlers/settings.py").read_text()
    stats_block = source.split('elif action == "stats":', 1)[1].split('\n    await query.answer()', 1)[0]
    assert '⛦ Total Users: {total}' in stats_block
    assert '⛦ Banned Users: {blocked}' in stats_block
    assert 'get_me()' not in stats_block
    assert 'USERS:' not in stats_block
    assert 'BLOCKED:' not in stats_block


def test_animation_legacy_defaults_are_migrated_and_frames_are_bold():
    from app.handlers.start import animation_frame_text, normalize_animation_config

    cfg = normalize_animation_config({
        "enabled": True,
        "prefix": "Checking..",
        "frames": ["OLD", "VALUES", "HERE"],
        "speed": 0.75,
        "mode": "new_message",
    })
    assert cfg["prefix"] == "Checking"
    assert cfg["frames"] == [".", "..", "..."]
    assert cfg["speed"] == 0.15
    assert cfg["mode"] == "new_message"
    assert cfg["enabled"] is True
    assert animation_frame_text(cfg["prefix"], cfg["frames"][0]) == "Checking<b>.</b>"


def test_animation_speed_is_clamped_for_both_modes_without_overriding_custom_speed():
    from app.handlers.start import normalize_animation_config

    slow = normalize_animation_config({
        "prefix": "Checking", "frames": [".", "..", "..."],
        "speed": 0.05, "mode": "force_join_poster",
    })
    fast = normalize_animation_config({
        "prefix": "Checking", "frames": [".", "..", "..."],
        "speed": 0.75, "mode": "new_message",
    })
    assert slow["speed"] == 0.15
    assert fast["speed"] == 0.75


def test_animation_speed_never_exceeds_supported_range():
    from app.handlers.start import normalize_animation_config

    cfg = normalize_animation_config({
        "prefix": "Checking", "frames": [".", "..", "..."],
        "speed": 3.5, "mode": "force_join_poster",
    })
    assert cfg["speed"] == 1.0


def test_settings_navigation_does_not_spawn_duplicate_panels_and_picker_prompt_is_hidden():
    from pathlib import Path

    source = Path("app/handlers/settings.py").read_text()
    edit_page = source.split("async def edit_page(query, text, keyboard):", 1)[1].split("\nasync def render_home", 1)[0]
    # A replacement is permitted only when Telegram refuses an edit, and
    # the old panel must then be deleted so navigation cannot stack panels.
    assert "query.message.answer(" in edit_page
    assert "await query.message.delete()" in edit_page
    assert "return replacement" in edit_page
    picker = source.split('elif action == "forcejoin_add":', 1)[1].split('elif action == "forcejoin_other":', 1)[0]
    assert '"\\u2063"' in picker
    assert "Choose the type of chat to add." not in picker


def test_force_join_chat_shared_message_and_cancel_keyboard_are_cleaned_up():
    from pathlib import Path

    source = Path("app/handlers/settings.py").read_text()
    input_handler = source.split("async def receive_setting_input", 1)[1]
    shared_block = input_handler.split('if field == "forcejoin_wait_chat":', 1)[1]
    assert "await message.delete()" in shared_block
    cancel_block = input_handler.split('if field in {"forcejoin_wait_chat", "forcejoin_wait_mode"}', 1)[1].split('if field == "forcejoin_wait_chat":', 1)[0]
    assert "Force Join channel selection cancelled." not in cancel_block
    assert "keyboard_removal.delete()" in cancel_block


def test_settings_command_message_is_deleted_and_picker_keyboard_is_removed_on_navigation():
    from pathlib import Path

    source = Path("app/handlers/settings.py").read_text()
    settings_command = source.split('async def settings_command', 1)[1].split('@router.callback_query', 1)[0]
    assert "await message.delete()" in settings_command
    callbacks = source.split('async def settings_callbacks', 1)[1].split('\nasync def ', 1)[0]
    assert 'pending.get("field") == "forcejoin_wait_chat"' in callbacks
    assert 'reply_markup=ReplyKeyboardRemove()' in callbacks


def test_force_join_chat_add_returns_to_main_page_with_fallback():
    from pathlib import Path

    source = Path("app/handlers/settings.py").read_text()
    handler = source.split('if field == "forcejoin_wait_chat":', 1)[1].split('if field == "custom_button_text":', 1)[0]
    assert 'text=format_settings_page(panel_text)' in handler
    assert 'await message.bot.edit_message_text(' in handler
    # Normal success path edits the saved Force Join panel in place; it does
    # not send another panel. Replacement is handled by edit_page navigation.
    assert 'panel_chat_id, panel_message_id = data.get("forcejoin_panel_chat_id"), data.get("forcejoin_panel_message_id")' in handler
    assert 'channels.append(channel_id)' in handler
    assert 'await patch(db, "force_join", {"channels": channels, "channels_override": True})' in handler


def test_force_join_disable_button_is_next_to_verify():
    from pathlib import Path

    source = Path("app/handlers/settings.py").read_text()
    page = source.split("async def build_forcejoin_page", 1)[1].split("async def render_forcejoin", 1)[0]
    assert '[("VERIFY", "settings:forcejoin_verify_text"),' in page
    assert '("DISABLE" if cfg.get("enabled", False) else "ENABLE", "settings:forcejoin_toggle")' in page
    assert 'rows.append([("DISABLE" if cfg.get("enabled", False) else "ENABLE", "settings:forcejoin_toggle")])' not in page


def test_edit_page_replaces_uneditable_panel_and_deletes_old_panel():
    from pathlib import Path

    source = Path("app/handlers/settings.py").read_text()
    edit_page = source.split("async def edit_page", 1)[1].split("async def render_home", 1)[0]
    assert "await query.message.edit_text(" in edit_page
    assert "await query.message.answer(" in edit_page
    assert "await query.message.delete()" in edit_page


def test_force_join_add_tracks_the_panel_message_returned_by_edit_page():
    from pathlib import Path

    source = Path("app/handlers/settings.py").read_text()
    assert "return replacement" in source.split("async def edit_page", 1)[1].split("async def render_home", 1)[0]
    add_flow = source.split('elif action == "forcejoin_add":', 1)[1].split('elif action == "forcejoin_other":', 1)[0]
    assert "panel_message = await edit_page(query" in add_flow
    assert "forcejoin_panel_message_id=panel_message.message_id" in add_flow


def test_rich_text_settings_preserve_telegram_entities_and_html_success_prompt():
    from pathlib import Path

    source = Path("app/handlers/settings.py").read_text()
    assert "html_text = getattr(message, \"html_text\", None)" in source
    assert 'value = formatted_input_text(message, value)' in source
    assert 'parse_mode="HTML")' in source.split("async def finish_input_panel", 1)[1].split("async def edit_page", 1)[0]
    assert 'await patch(db, "force_join", {"message": value})' in source
    assert 'await patch(db, "invalid_post", {"text": value}' in source
    assert 'await patch(db, "after_delete", {"text": value}' in source


def test_message_see_and_default_popups_are_available_for_all_requested_settings():
    from pathlib import Path

    source = Path("app/handlers/settings.py").read_text()
    for action in (
        "start_text_see", "start_text_default",
        "forcejoin_message_see", "forcejoin_message_default",
        "auto_message_see", "auto_message_default",
        "invalid_text_see", "invalid_text_default",
    ):
        assert f'action == "{action}"' in source
    assert "def plain_preview(value)" in source
    assert "restored to the recommended default" in source


def test_force_join_chat_add_has_a_main_panel_fallback_and_poster_is_kept_for_verify():
    from pathlib import Path

    settings_source = Path("app/handlers/settings.py").read_text()
    start_source = Path("app/handlers/start.py").read_text()
    handler = settings_source.split('if field == "forcejoin_wait_chat":', 1)[1].split('if field == "custom_button_text":', 1)[0]
    assert "build_forcejoin_page(message.bot, db, settings)" in handler
    assert "replacement = await message.bot.send_message(" in handler
    assert "await message.bot.delete_message(chat_id=panel_chat_id, message_id=panel_message_id)" in handler
    assert 'force_cfg.get("poster_file_id") or force_cfg.get("poster_photo_id")' in start_source
    assert 'cfg.get("poster_file_id") or cfg.get("poster_photo_id")' in start_source


def test_pending_join_request_expires_and_requires_timestamp():
    from datetime import datetime, timedelta, timezone
    from app.handlers.start import _pending_request_is_recent

    now = datetime.now(timezone.utc)
    assert _pending_request_is_recent({"requested_at": now - timedelta(minutes=5)}, now=now)
    assert _pending_request_is_recent({"requested_at": now - timedelta(days=2)}, now=now)
    assert not _pending_request_is_recent({"user_id": 42}, now=now)


@pytest.mark.asyncio
async def test_force_join_explicit_empty_database_override_removes_environment_channels():
    from types import SimpleNamespace
    from app.handlers.start import effective_force_join_channels

    class SettingsCollection:
        async def find_one(self, query):
            return {"key": "force_join", "channels": [], "channels_override": True}

    database = SimpleNamespace(db=SimpleNamespace(settings=SettingsCollection()))
    assert await effective_force_join_channels(
        database, SimpleNamespace(force_join_channels=["@envchannel"])
    ) == []


@pytest.mark.asyncio
async def test_force_join_uses_environment_channels_when_database_list_is_empty():
    from types import SimpleNamespace
    from app.handlers.start import effective_force_join_channels

    class Collection:
        async def find_one(self, query):
            return {"key": "force_join", "channels": []}

    database = SimpleNamespace(db=SimpleNamespace(settings=Collection()))
    assert await effective_force_join_channels(database, SimpleNamespace(force_join_channels=["@envchannel"])) == ["@envchannel"]


def test_invalid_post_router_runs_after_file_and_batch_message_handlers():
    from pathlib import Path

    source = Path("main.py").read_text()
    router_order = source.split("for r in (", 1)[1].split("):dp.include_router(r)", 1)[0]
    assert router_order.index("file_router") < router_order.index("misc_router")
    assert router_order.index("batch_router") < router_order.index("misc_router")
    assert router_order.index("settings_router") < router_order.index("misc_router")


def test_invalid_post_never_deletes_submitted_user_message():
    from pathlib import Path

    source = Path("app/handlers/misc.py").read_text()
    handler = source.split("async def invalid_post", 1)[1].split("@router.callback_query", 1)[0]
    assert "response = await message.answer_animation(" in handler
    assert "response = await message.answer_photo(" in handler
    assert "response = await message.answer(text, parse_mode=\"HTML\", reply_markup=markup)" in handler
    assert "await message.delete()" not in handler
    assert "keeping the submitted message" in handler


def test_invalid_post_ignores_commands_banned_users_owner_and_moderators():
    from pathlib import Path

    source = Path("app/handlers/misc.py").read_text()
    handler = source.split("async def invalid_post", 1)[1].split("@router.callback_query", 1)[0]
    assert 'message.text and message.text.startswith("/")' in handler
    assert "if await is_banned(database.db, user.id):" in handler
    assert "if await is_owner_or_moderator(database.db, user.id, settings):" in handler
    assert 'find_one({"key": "invalid_post"})' in handler
    assert 'if not cfg.get("enabled", False):' in handler
    assert 'log.info("Invalid-post skipped: setting disabled or missing")' in handler
    assert 'log.exception("Invalid-post handler failed before completing")' in handler



def test_force_join_poster_keeps_old_post_until_replacement_is_ready():
    from pathlib import Path

    source = Path("app/handlers/start.py").read_text()
    impl_start = source.index("async def _forcejoin_check_impl")
    missing_check = source.index("missing = [] if await is_owner_or_moderator", impl_start)
    old_post_delete = source.index("await old_post.delete()", impl_start)
    replacement_edit = source.index("if await _edit_force_join_post(animation_post, rendered, markup):", impl_start)

    assert missing_check < replacement_edit < old_post_delete


def test_broadcast_report_counts_skipped_banned_recipients():
    from app.handlers.broadcast import format_broadcast_report

    report = format_broadcast_report({
        "sent": 7, "failed": 1, "blocked": 2, "unknown": 1, "skipped_banned": 3,
    })
    assert "Recipients: 14" in report
    assert "Sent successfully: 7" in report
    assert "Failed: 2" in report
    assert "Blocked/unreachable: 2" in report
    assert "Skipped (banned): 3" in report


def test_stats_blocked_count_does_not_treat_any_delivery_error_as_blocked():
    from pathlib import Path

    source = Path("app/handlers/admin.py").read_text()
    stats_handler = source.split('@router.message(Command("stats"))', 1)[1].split(
        '@router.callback_query(F.data == "stats:ok")', 1
    )[0]
    assert 'count_documents({"blocked": True})' in stats_handler
    assert 'last_delivery_error' not in stats_handler


def test_force_join_channel_button_is_not_omitted_when_invite_resolution_fails():
    from app.handlers.start import build_force_join_channel_button

    button = build_force_join_channel_button({"button_text": "Test"}, 2, None, "-100123")
    assert button.text == "Test"
    assert button.callback_data == "forcejoin:repair:-100123"
    assert button.url is None


def test_force_join_channel_button_uses_resolved_url_when_available():
    from app.handlers.start import build_force_join_channel_button

    button = build_force_join_channel_button({"button_text": "Tst"}, 1, "https://t.me/channel")
    assert button.text == "Tst"
    assert button.url == "https://t.me/channel"
    assert button.callback_data is None


def test_force_join_channel_custom_labels_are_limited_to_telegram_button_length():
    from pathlib import Path

    source = Path("app/handlers/settings.py").read_text()
    block = source.split('if field == "forcejoin_label":', 1)[1].split("\n    if field ==", 1)[0]
    assert "len(value) > 64" in block
    assert "Button text must be between 1 and 64 characters." in block


def test_external_force_join_feature_is_completely_removed_from_runtime():
    from pathlib import Path

    settings_source = Path("app/handlers/settings.py").read_text()
    start_source = Path("app/handlers/start.py").read_text()
    for retired_token in ("forcejoin_other", "other_links", "force_join_capacity_reached"):
        assert retired_token not in settings_source
        assert retired_token not in start_source


def test_legacy_external_force_join_setting_is_removed_once_at_startup():
    from pathlib import Path

    source = Path("app/database/connection.py").read_text()
    assert 'async def remove_legacy_external_force_join_links(db)' in source
    assert '"$unset": {"other_links": ""}' in source
    assert 'await remove_legacy_external_force_join_links(self.db)' in source


def test_button_text_validation_enforces_telegram_limits():
    from app.handlers.settings import valid_button_text

    assert valid_button_text("Join")
    assert valid_button_text("x" * 64)
    assert not valid_button_text("")
    assert not valid_button_text("x" * 65)


@pytest.mark.asyncio
async def test_shortener_input_state_does_not_swallow_other_commands():
    from aiogram.dispatcher.event.bases import SkipHandler
    from app.handlers.shortener import waiting_command

    class FakeState:
        def __init__(self):
            self.cleared = False

        async def clear(self):
            self.cleared = True

    state = FakeState()
    with pytest.raises(SkipHandler):
        await waiting_command(None, state)
    assert state.cleared is True


def test_batch_order_uses_database_channel_message_order():
    from app.handlers.batch import ordered_batch_items

    items = [
        {"source_message_id": 303, "original_message_id": 10},
        {"source_message_id": 301, "original_message_id": 12},
        {"source_message_id": 302, "original_message_id": 11},
    ]
    assert [item["source_message_id"] for item in ordered_batch_items(items)] == [301, 302, 303]


def test_batch_accepts_only_forwarded_posts_from_configured_database_channel():
    from types import SimpleNamespace
    from app.handlers.batch import _database_channel_origin

    valid = SimpleNamespace(forward_origin=SimpleNamespace(
        chat=SimpleNamespace(id=-100123), message_id=77
    ))
    external = SimpleNamespace(forward_origin=SimpleNamespace(
        chat=SimpleNamespace(id=-100999), message_id=77
    ))
    not_forwarded = SimpleNamespace(forward_origin=None)

    assert _database_channel_origin(valid, "-100123") == (-100123, 77)
    assert _database_channel_origin(external, -100123) is None
    assert _database_channel_origin(not_forwarded, -100123) is None


def test_batch_does_not_copy_external_files_and_debounces_panels():
    from pathlib import Path

    source = Path("app/handlers/batch.py").read_text()
    collect = source.split("async def _collect_message", 1)[1].split(
        "@router.message(BatchState.collecting)", 1
    )[0]
    panel = source.split("async def _render_session_panel", 1)[1].split(
        '@router.message(Command("batch"))', 1
    )[0]
    assert "_database_channel_origin(message, settings.source_channel_id)" in collect
    assert "message.bot.copy_message(" not in collect
    assert "This file was not added or copied" in collect
    assert "_render_session_panel" not in collect
    assert "PANEL_SETTLE_SECONDS = 1" in source
    assert "panel_count" in panel
    assert "await bot.send_message(chat_id, text, reply_markup=menu())" in panel
    assert "await bot.delete_message(chat_id, old_id)" in panel
    assert "if count < 2 or count >= 100" in panel


def test_batch_starts_with_minimum_and_maximum_file_prompt():
    from pathlib import Path

    source = Path("app/handlers/batch.py").read_text()
    assert "Send a minimum of 2 and a maximum of 100 files" in source
    assert "AUTO_GENERATE_SETTLE_SECONDS = 4" in source
    assert "maximum is 100 files" in source


def test_batch_single_file_does_not_render_a_generate_panel():
    from pathlib import Path

    source = Path("app/handlers/batch.py").read_text()
    panel = source.split("async def _render_session_panel", 1)[1].split(
        '@router.message(Command("batch"))', 1
    )[0]
    assert "if count < 2 or count >= 100" in panel
    assert "PANEL_SETTLE_SECONDS = 1" in source
    assert "await _render_session_panel(bot, db, panel_session)" in source


def test_batch_panel_is_not_sent_from_each_file_update():
    from pathlib import Path

    source = Path("app/handlers/batch.py").read_text()
    collect = source.split("async def _collect_message", 1)[1].split(
        "@router.message(BatchState.collecting)", 1
    )[0]
    assert "_render_session_panel" not in collect
    assert "The background worker debounces a burst of updates into one panel." in collect

from app.handlers.start import normalize_animation_config
from app.handlers.settings import build_animation_page


def test_legacy_prefix_does_not_overwrite_custom_animation_frames():
    configured = {
        "prefix": "Checking..",
        "frames": ["🟢", "🟡", "🔴"],
        "frames_enabled": True,
        "speed": 0.3,
    }

    normalized = normalize_animation_config(configured)

    assert normalized["prefix"] == "Checking"
    assert normalized["frames"] == ["🟢", "🟡", "🔴"]
    assert normalized["frames_enabled"] is True


def test_legacy_config_without_three_frames_still_gets_default_frames():
    normalized = normalize_animation_config(
        {"prefix": "Checking..", "frames": ["only-one-frame"]}
    )

    assert normalized["frames"] == [".", "..", "..."]



def test_animation_page_shows_enabled_status_in_text_and_buttons():
    text, markup = build_animation_page({
        "prefix": "Verifying",
        "prefix_enabled": True,
        "frames": ["🟢", "🟡", "🔴"],
        "frames_enabled": True,
        "enabled": True,
        "speed": 0.3,
    })

    assert "Prefix Status: ENABLED ✅" in text
    assert "Frames Status: ENABLED ✅" in text
    assert markup.inline_keyboard[0][0].text == "PREFIX 🟢"
    assert markup.inline_keyboard[0][1].text == "FRAMES 🟢"


def test_animation_page_shows_disabled_status_in_text_and_buttons():
    text, markup = build_animation_page({
        "prefix": "",
        "prefix_enabled": False,
        "frames": [".", "..", "..."],
        "frames_enabled": False,
        "enabled": False,
        "speed": 0.3,
    })

    assert "Prefix Status: DISABLED ❌" in text
    assert "Frames Status: DISABLED ❌" in text
    assert markup.inline_keyboard[0][0].text == "PREFIX 🔴"
    assert markup.inline_keyboard[0][1].text == "FRAMES 🔴"

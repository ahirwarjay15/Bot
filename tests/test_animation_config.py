from app.handlers.start import normalize_animation_config
from app.handlers.settings import build_animation_page


def test_legacy_prefix_is_removed_without_overwriting_valid_frames():
    configured = {
        "prefix": "Checking..",
        "frames": ["🟢", "🟡", "🔴", "🔵"],
        "frames_enabled": True,
        "speed": 0.3,
        "mode": "message",
    }
    normalized = normalize_animation_config(configured)
    assert normalized["frames"] == ["🟢", "🟡", "🔴", "🔵"]
    assert normalized["speed"] == 0.3
    assert "prefix" not in normalized
    assert "frames_enabled" not in normalized
def test_legacy_config_without_four_frames_gets_five_default_frames():
    normalized = normalize_animation_config({"prefix": "Checking..", "frames": ["only-one-frame"]})
    assert normalized["frames"] == ["Checking.", "Checking..", "Checking...", "Checking....", "Checking....."]
def test_animation_page_shows_canonical_status_and_only_remaining_controls():
    text, markup = build_animation_page({
        "prefix": "Verifying", "prefix_enabled": True,
        "frames": ["1", "2", "3", "4"], "frames_enabled": True,
        "enabled": True, "mode": "message", "speed": 0.3,
    })
    assert "Status: Message" in text
    assert "Frames: 1, 2, 3, 4" in text
    assert "Speed: 0.30s per frame" in text
    assert "Prefix" not in text and "prefix" not in text.lower()
    assert len(markup.inline_keyboard) == 3
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert labels == ["FRAMES", "SPEED", "MODE", "BLINK", "《 BACK"]
def test_animation_page_shows_disabled_mode_and_minimum_speed():
    text, markup = build_animation_page({
        "prefix": "", "prefix_enabled": False, "frames": [".", "..", "..."],
        "frames_enabled": False, "enabled": False, "mode": "disabled", "speed": 0.1,
    })
    assert "Status: Disabled" in text
    assert "Speed: 0.20s per frame" in text
    assert "Prefix" not in text
    assert all(button.text not in {"PREFIX", "ENABLE/DISABLE"} for row in markup.inline_keyboard for button in row)

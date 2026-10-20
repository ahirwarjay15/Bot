from app.handlers.start import normalize_animation_config


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

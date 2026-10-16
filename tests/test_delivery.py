from app.services.delivery import _minutes, _notice, DEFAULT_NOTICE, format_notice

def test_auto_delete_timer_accepts_blueprint_cycle():
    for minutes in (2, 5, 15, 30, 60, 180, 360, 720, 1440):
        assert _minutes({"minutes": minutes}) == minutes

def test_invalid_auto_delete_timer_falls_back_to_default():
    assert _minutes({"minutes": 7}) == 30
    assert _minutes({}) == 30

def test_auto_delete_notice_interpolates_timer():
    assert _notice({"notice": "Deleted in {auto_delete_time}", "minutes": 30}, 30) == "Deleted in 30 minutes"
    assert _notice({"notice": DEFAULT_NOTICE}, 60) == "Your files will be automatically deleted after 1 hour."

def test_auto_delete_notice_preserves_bold_entity_and_adjusts_length():
    source = "Bold {auto_delete_time}"
    cfg = {
        "notice": source,
        "notice_entities": [{"type": "bold", "offset": 0, "length": len(source)}],
    }
    text, entities, custom = format_notice(cfg, 30)
    assert text == "Bold 30 minutes"
    assert custom is True
    assert entities[0].offset == 0
    assert entities[0].length == len(text)


def test_auto_delete_notice_shifts_entities_after_timer_placeholder():
    marker = "{auto_delete_time}"
    cfg = {
        "notice": marker + " bold",
        "notice_entities": [{"type": "bold", "offset": len(marker) + 1, "length": 4}],
    }
    text, entities, custom = format_notice(cfg, 30)
    assert text == "30 minutes bold"
    assert custom is True
    assert entities[0].offset == len("30 minutes") + 1


import pytest
import app.services.delivery as delivery


@pytest.mark.asyncio
async def test_single_file_uses_durable_delivery_queue(monkeypatch):
    captured = {}

    async def fake_deliver_batch(bot, db, *, user_id, batch, deeplink, request_key=None):
        captured.update(user_id=user_id, batch=batch, deeplink=deeplink, request_key=request_key)
        return (1, 0)

    monkeypatch.setattr(delivery, "deliver_batch", fake_deliver_batch)
    result = await delivery.deliver_reference(
        bot=None, db=None, settings=None, user_id=42,
        file_doc={"source_chat_id": -100123, "source_message_id": 77, "file_name": "x.mp4"},
        deeplink="https://t.me/example?start=file_abc", request_key="start:42:10",
    )
    assert result == (1, 0)
    assert captured["request_key"] == "start:42:10"
    assert captured["batch"]["items"] == [{
        "source_chat_id": -100123, "source_message_id": 77,
        "file_name": "x.mp4", "reply_markup": None,
    }]


def test_after_delete_replacement_uses_html_parse_mode():
    from pathlib import Path

    source = Path("app/services/delivery.py").read_text()
    assert 'text=text, reply_markup=markup, parse_mode="HTML"' in source
    assert 'bot.send_message(task["user_id"], text, reply_markup=markup, parse_mode="HTML")' in source

from app.handlers.anime import _ordered_collected_items


def test_anime_items_are_ordered_by_original_telegram_message_id():
    items = [
        {"original_message_id": 103, "file_name": "third"},
        {"original_message_id": 101, "file_name": "first"},
        {"original_message_id": 102, "file_name": "second"},
    ]

    ordered = _ordered_collected_items(items)

    assert [item["file_name"] for item in ordered] == [
        "first",
        "second",
        "third",
    ]

from types import SimpleNamespace

import pytest

from app.database.files import FileRepository
from app.services.delivery import _compact_buttons_markup, _stored_message_ids, deliver_batch


def _button(message_id, row, column, text, url, color=""):
    return "|".join(map(str, (message_id, row, column, text, url, color)))


def test_stored_message_ids_preserve_repeated_selections():
    assert _stored_message_ids("372,372,372,373") == [372, 372, 372, 373]


def test_compact_button_parser_keeps_two_distinct_buttons_and_deduplicates_repeated_entries():
    entries = [
        _button(372, 1, 0, "480p", "https://example.org/480", "S"),
        _button(372, 1, 1, "720p", "https://example.org/720", "P"),
        _button(372, 1, 0, "480p", "https://example.org/480", "S"),
        _button(372, 1, 1, "720p", "https://example.org/720", "P"),
    ]
    markup = _compact_buttons_markup(entries)
    assert markup is not None
    assert len(markup["inline_keyboard"]) == 1
    assert len(markup["inline_keyboard"][0]) == 2
    assert [button["text"] for button in markup["inline_keyboard"][0]] == ["480p", "720p"]


class FakeFilesCollection:
    def __init__(self):
        self.document = None

    async def find_one(self, query, projection=None):
        if self.document and self.document.get("key") == query.get("key"):
            return self.document
        return None

    async def insert_one(self, document):
        self.document = dict(document)
        return SimpleNamespace(inserted_id="fake-id")


class FakeDatabase:
    def __init__(self):
        self.files = FakeFilesCollection()


@pytest.mark.asyncio
async def test_save_batch_preserves_repeated_ids_but_stores_each_message_buttons_once(monkeypatch):
    database = FakeDatabase()
    repository = FileRepository(database)
    monkeypatch.setattr(repository, "reserve_code", _reserve_code)

    record = await repository.save_batch(
        [372, 372, 372],
        buttons={
            "372": [
                [1, 0, "480p", "https://example.org/480", "S"],
                [1, 1, "720p", "https://example.org/720", "P"],
            ]
        },
    )

    assert record["id"] == "372,372,372"
    assert record["buttons"] == [
        "372|1|0|480p|https://example.org/480|S",
        "372|1|1|720p|https://example.org/720|P",
    ]


async def _reserve_code(length):
    assert length == 7
    return "Test123"


@pytest.mark.asyncio
async def test_delivery_sends_each_repeated_id_with_only_its_two_buttons():
    sent = []

    class FakeBot:
        async def copy_message(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(message_id=1000 + len(sent))

    class FakeSettingsCollection:
        async def find_one(self, query):
            return None

    class FakeTasksCollection:
        pass

    db = SimpleNamespace(settings=FakeSettingsCollection(), tasks=FakeTasksCollection())
    buttons = [
        _button(372, 1, 0, "480p", "https://example.org/480", "S"),
        _button(372, 1, 1, "720p", "https://example.org/720", "P"),
    ]

    result = await deliver_batch(
        FakeBot(), db, user_id=42,
        batch={"id": "372,372,372", "_source_chat_id": -100123, "buttons": buttons},
        deeplink="https://t.me/example?start=abc",
    )

    assert result == (3, 0)
    assert len(sent) == 3
    assert all(call["message_id"] == 372 for call in sent)
    assert all(call["reply_markup"] is not None for call in sent)
    assert all(len(call["reply_markup"].inline_keyboard[0]) == 2 for call in sent)

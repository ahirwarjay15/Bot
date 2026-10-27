import pytest

from app.database.files import FileRepository
from app.handlers.file import MAX_FILES
from app.handlers.batch import same_source_channel


class FakeFiles:
    def __init__(self):
        self.documents = []

    async def find_one(self, query, projection=None):
        for document in self.documents:
            if document.get("key") == query.get("key"):
                return dict(document)
        return None

    async def insert_one(self, document):
        self.documents.append(dict(document))

    async def update_one(self, query, update, upsert=False):
        existing = await self.find_one(query)
        if existing:
            for document in self.documents:
                if document.get("key") == query.get("key"):
                    document.update(update.get("$set", {}))
                    break
        elif upsert:
            self.documents.append(dict(update.get("$set", {})))


class FakeDatabase:
    def __init__(self):
        self.files = FakeFiles()


@pytest.mark.asyncio
async def test_single_file_stores_id_as_array_and_omits_empty_fields():
    db = FakeDatabase()
    saved = await FileRepository(db).save_file(source_message_id=381)

    assert saved["id"] == [381]
    assert "buttons" not in saved
    assert "reply_markup" not in saved
    assert "message_id" not in saved


@pytest.mark.asyncio
async def test_batch_stores_ordered_ids_without_buttons_or_chat_by_default():
    db = FakeDatabase()
    saved = await FileRepository(db).save_batch([381, 379, 382])

    assert saved["id"] == [381, 379, 382]
    assert "buttons" not in saved
    assert "chat" not in saved
    assert "message_id" not in saved


@pytest.mark.asyncio
async def test_external_channel_batch_saves_numeric_chat_id_but_no_buttons():
    db = FakeDatabase()
    saved = await FileRepository(db).save_batch([381, 379], chat_id=-1001234567890)

    assert saved == {"key": saved["key"], "id": [381, 379], "chat": -1001234567890}
    assert "buttons" not in saved
    assert "message_id" not in saved


@pytest.mark.asyncio
async def test_batch_accepts_handler_item_references_without_storing_chat_or_buttons():
    db = FakeDatabase()
    saved = await FileRepository(db).save_batch([
        {"source_chat_id": -100123, "source_message_id": 3},
        {"source_chat_id": -100123, "source_message_id": 4},
    ])

    assert saved["id"] == [3, 4]
    assert "source_chat_id" not in saved
    assert "chat" not in saved
    assert "buttons" not in saved


@pytest.mark.asyncio
async def test_only_multi_id_array_is_a_batch_record():
    repo = FileRepository(FakeDatabase())

    assert await repo.is_batch_record({"key": "abc1234", "id": [11, 12]}) is True
    assert await repo.is_batch_record({"key": "abc1234", "id": [11]}) is False
    assert await repo.is_batch_record({"key": "abc1234", "id": 11}) is False


@pytest.mark.asyncio
async def test_batch_requires_at_least_two_ids():
    with pytest.raises(ValueError, match="at least 2"):
        await FileRepository(FakeDatabase()).save_batch([381])


def test_file_collection_limit_is_50():
    assert MAX_FILES == 50


def test_batch_rejects_endpoints_from_different_chats():
    assert same_source_channel(-1001234567890, -1001234567890) is True
    assert same_source_channel(-1001234567890, -1009876543210) is False
    assert same_source_channel(None, -1009876543210) is False

import pytest

from app.database.files import FileRepository
from app.handlers.file import MAX_FILES


class FakeFiles:
    def __init__(self):
        self.documents = []

    async def find_one(self, query, projection=None):
        return None

    async def insert_one(self, document):
        self.documents.append(dict(document))


class FakeDatabase:
    def __init__(self):
        self.files = FakeFiles()


@pytest.mark.asyncio
async def test_single_file_uses_one_id_array_and_omits_empty_fields():
    db = FakeDatabase()
    repo = FileRepository(db)

    saved = await repo.save_file(source_message_id=381)

    assert saved == {"key": saved["key"], "id": [381]}
    assert db.files.documents[0]["id"] == [381]
    assert "buttons" not in saved
    assert "reply_markup" not in saved


@pytest.mark.asyncio
async def test_single_file_preserves_button_rows_without_extra_fields():
    db = FakeDatabase()
    repo = FileRepository(db)
    buttons = [[{"text": "Open", "url": "https://example.org", "style": "primary"}]]

    saved = await repo.save_file(source_message_id=381, buttons=buttons)

    assert saved["id"] == [381]
    assert saved["buttons"] == buttons
    assert "reply_markup" not in saved


@pytest.mark.asyncio
async def test_batch_ids_are_stored_as_compact_ordered_array():
    db = FakeDatabase()
    repo = FileRepository(db)

    saved = await repo.save_batch(
        [381, 379, 382],
        buttons={"01": [[{"text": "Open", "url": "https://example.org"}]]},
    )

    assert saved["id"] == [381, 379, 382]
    assert saved["buttons"]["01"] == [[{"text": "Open", "url": "https://example.org"}]]
    assert db.files.documents[0]["id"] == [381, 379, 382]


@pytest.mark.asyncio
async def test_batch_accepts_current_handler_item_references_without_changing_batch_flow():
    db = FakeDatabase()
    repo = FileRepository(db)

    saved = await repo.save_batch([
        {"source_chat_id": -100123, "source_message_id": 3},
        {"source_chat_id": -100123, "source_message_id": 4},
        {"source_chat_id": -100123, "source_message_id": 5},
    ])

    assert saved["id"] == [3, 4, 5]
    assert "source_chat_id" not in saved
    assert "buttons" not in saved


@pytest.mark.asyncio
async def test_only_multi_id_array_is_a_batch_record():
    repo = FileRepository(FakeDatabase())

    assert await repo.is_batch_record({"key": "abc1234", "id": [11, 12]}) is True
    assert await repo.is_batch_record({"key": "abc1234", "id": [11]}) is False
    assert await repo.is_batch_record({"key": "abc1234", "id": 11}) is False


@pytest.mark.asyncio
async def test_batch_requires_at_least_two_ids():
    repo = FileRepository(FakeDatabase())

    with pytest.raises(ValueError, match="at least 2"):
        await repo.save_batch([381])


def test_file_collection_limit_is_50():
    assert MAX_FILES == 50

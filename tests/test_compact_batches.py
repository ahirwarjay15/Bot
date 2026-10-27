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
async def test_new_batch_ids_are_stored_as_compact_ordered_array():
    db = FakeDatabase()
    repo = FileRepository(db)

    saved = await repo.save_batch(
        [381, 379, 382],
        buttons={"01": [[{"text": "Open", "url": "https://example.org"}]]},
    )

    assert saved["id"] == [381, 379, 382]
    assert list(saved["id"]) == [381, 379, 382]
    assert saved["buttons"]["01"] == [[{"text": "Open", "url": "https://example.org"}]]
    assert db.files.documents[0]["id"] == [381, 379, 382]


@pytest.mark.asyncio
async def test_compact_batch_is_recognized_as_batch_record():
    repo = FileRepository(FakeDatabase())

    assert await repo.is_batch_record({"key": "abc1234", "id": [11, 12]}) is True
    assert await repo.is_batch_record({"key": "abc1234", "id": {"01": 11, "02": 12}}) is True
    assert await repo.is_batch_record({"key": "abc1234", "id": 11}) is False


def test_file_collection_limit_is_50():
    assert MAX_FILES == 50

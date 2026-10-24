from types import SimpleNamespace

import pytest

from app.database.connection import unify_file_batch_records


class AsyncCursor:
    def __init__(self, records):
        self.records = records

    def __aiter__(self):
        self.iterator = iter(self.records)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration:
            raise StopAsyncIteration


class FilesCollection:
    def __init__(self):
        self.records = [{
            "file_key": "file0",
            "file_name": "unused.mp4",
            "created_at": "old",
            "created_by": 123,
            "kind": "file",
            "source_message_id": 1,
        }]
        self.unset_fields = set()

    async def find_one(self, query, projection=None):
        return next((record.copy() for record in self.records
                     if record.get("file_key") == query.get("file_key")), None)

    async def insert_one(self, document):
        self.records.append(document.copy())

    async def update_many(self, query, update):
        fields = update.get("$unset", {})
        self.unset_fields.update(fields)
        for record in self.records:
            for field in fields:
                record.pop(field, None)


class BatchesCollection:
    def __init__(self):
        self.records = [{"_id": 1, "batch_id": "batch0", "items": [
            {"source_chat_id": -100123, "source_message_id": 10}
        ]}]

    def find(self, query):
        return AsyncCursor([record.copy() for record in self.records])

    async def delete_one(self, query):
        self.records = [record for record in self.records if record.get("_id") != query.get("_id")]


@pytest.mark.asyncio
async def test_batches_move_into_shared_files_collection_and_unused_metadata_is_removed():
    files = FilesCollection()
    batches = BatchesCollection()
    db = SimpleNamespace(files=files, batches=batches)

    assert await unify_file_batch_records(db) == 1

    batch = await files.find_one({"file_key": "batch0"})
    assert batch["items"][0]["source_message_id"] == 10
    assert batches.records == []
    assert not ({"file_name", "created_at", "created_by", "kind"} & set(files.records[0]))
    assert {"file_name", "created_at", "created_by", "kind"} <= files.unset_fields

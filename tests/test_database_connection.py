from types import SimpleNamespace

import pytest

from app.database.connection import backfill_deeplink_codes


class AsyncCursor:
    def __init__(self, records):
        self.records = records

    def __aiter__(self):
        self._iterator = iter(self.records)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration:
            raise StopAsyncIteration


class SourceCollection:
    def __init__(self, records):
        self.records = records

    def find(self, query, projection):
        field = next(iter(projection))
        return AsyncCursor([{field: record[field]} for record in self.records])


class CodeCollection:
    def __init__(self):
        self.batches = []

    async def bulk_write(self, operations, ordered=False):
        assert ordered is False
        self.batches.append(list(operations))


class MigrationCollection:
    def __init__(self):
        self.completed = False

    async def find_one(self, query):
        return {"_id": query["_id"]} if self.completed else None

    async def update_one(self, query, update, upsert=False):
        assert upsert is True
        self.completed = True


@pytest.mark.asyncio
async def test_legacy_deeplink_backfill_uses_bulk_writes_and_runs_once():
    codes = CodeCollection()
    migrations = MigrationCollection()
    db = SimpleNamespace(
        files=SourceCollection([{"file_key": f"file{i}"} for i in range(5)]),
        batches=SourceCollection([{"batch_id": "batch0"}]),
        deeplink_codes=codes,
        system_migrations=migrations,
    )

    assert await backfill_deeplink_codes(db, batch_size=2) is True
    assert [len(batch) for batch in codes.batches] == [2, 2, 1, 1]
    assert migrations.completed is True

    assert await backfill_deeplink_codes(db, batch_size=2) is False
    assert [len(batch) for batch in codes.batches] == [2, 2, 1, 1]

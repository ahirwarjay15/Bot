from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pymongo.errors import DuplicateKeyError

from app.services.polling_lease import LEASE_ID, _claim


class FakeLocks:
    def __init__(self):
        self.row = None

    async def find_one_and_update(self, query, update, return_document=None):
        now = query["$or"][0]["lease_until"]["$lte"] if "$or" in query else query["lease_until"]["$lte"]
        if self.row and self.row["_id"] == query["_id"] and self.row.get("lease_until", datetime.min.replace(tzinfo=timezone.utc)) <= now:
            self.row.update(update["$set"])
            return dict(self.row)
        return None

    async def insert_one(self, doc):
        if self.row:
            raise DuplicateKeyError("duplicate lease")
        self.row = dict(doc)

    async def update_one(self, query, update):
        return SimpleNamespace(modified_count=0)


@pytest.mark.asyncio
async def test_polling_lease_allows_only_one_owner():
    locks = FakeLocks()
    db = SimpleNamespace(service_locks=locks)

    assert await _claim(db, "owner-a") is True
    assert locks.row["_id"] == LEASE_ID
    assert await _claim(db, "owner-b") is False


@pytest.mark.asyncio
async def test_polling_lease_can_be_reclaimed_after_expiry():
    locks = FakeLocks()
    db = SimpleNamespace(service_locks=locks)

    assert await _claim(db, "owner-a") is True
    locks.row["lease_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    assert await _claim(db, "owner-b") is True
    assert locks.row["owner"] == "owner-b"

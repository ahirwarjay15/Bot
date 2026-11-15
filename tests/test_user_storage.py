from types import SimpleNamespace

import pytest

from app.database import users as user_storage


@pytest.mark.asyncio
async def test_registration_appends_to_comma_separated_active_bucket(monkeypatch):
    class Users:
        def __init__(self):
            self.query = None
            self.update = None

        async def find_one(self, query, projection, session=None):
            assert "id" in query
            return None

        async def update_one(self, query, update, session=None):
            self.query = query
            self.update = update
            return SimpleNamespace(matched_count=1, modified_count=1)

    collection = Users()
    monkeypatch.setattr(user_storage, "_BANNED_USER_IDS", set())
    assert await user_storage.register_active_user(
        SimpleNamespace(users=collection), 123456
    )
    assert collection.query["$expr"]["$lt"][1] == 100_000
    assert collection.update[0]["$set"]["id"]["$concat"] == ["$id", ",", "123456"]


@pytest.mark.asyncio
async def test_banned_registration_is_rejected_without_database_write(monkeypatch):
    class Users:
        async def find_one(self, *args, **kwargs):
            raise AssertionError("Banned users must not be written to active storage")

    monkeypatch.setattr(user_storage, "_BANNED_USER_IDS", {123456})
    assert not await user_storage.register_active_user(
        SimpleNamespace(users=Users()), 123456
    )


@pytest.mark.asyncio
async def test_startup_loads_banned_comma_separated_string_into_cache(monkeypatch):
    class Banned:
        async def find_one(self, query, projection):
            assert query == {"_id": user_storage.BANNED_DOCUMENT_ID}
            assert projection == {"id": 1}
            return {"id": "123456,789012"}

    monkeypatch.setattr(user_storage, "_BANNED_USER_IDS", set())
    count = await user_storage.load_banned_user_cache(
        SimpleNamespace(banned_users=Banned())
    )

    assert count == 2
    assert user_storage.is_banned_cached(123456)
    assert user_storage.is_banned_cached(789012)
    assert not user_storage.is_banned_cached(999999)


def test_split_ids_supports_legacy_arrays_and_current_string():
    assert user_storage._split_ids("12,34,56") == ["12", "34", "56"]
    assert user_storage._split_ids([12, "34"]) == ["12", "34"]
    assert user_storage._split_ids("") == []

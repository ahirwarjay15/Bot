from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.handlers.start import (
    _is_private_chat,
    _pending_request_is_recent,
    cleanup_expired_pending_join_requests,
    cleanup_pending_join_requests_for_channel,
    clear_accepted_private_join_request,
    join_status,
)


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return list(self.rows)

    def __aiter__(self):
        self.iterator = iter(self.rows)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration:
            raise StopAsyncIteration


@pytest.mark.parametrize(
    ("chat_type", "username", "expected"),
    [
        ("channel", None, True),
        ("supergroup", None, True),
        ("group", None, True),
        ("channel", "public_channel", False),
        ("supergroup", "public_group", False),
        ("private", None, False),
    ],
)
def test_is_private_chat(chat_type, username, expected):
    assert _is_private_chat(SimpleNamespace(type=chat_type, username=username)) is expected


def test_pending_request_expires_after_15_days_and_uses_compact_timestamp():
    now = datetime.now(timezone.utc)
    assert _pending_request_is_recent({"t": now - timedelta(days=14, hours=23)}, now=now)
    assert not _pending_request_is_recent({"t": now - timedelta(days=15, seconds=1)}, now=now)
    assert not _pending_request_is_recent({"requested_at": now}, now=now)


@pytest.mark.asyncio
async def test_accepted_private_join_request_pulls_only_that_user_and_removes_empty_chat():
    class Pending:
        def __init__(self):
            self.updates = []
            self.deletes = []

        async def update_one(self, query, update):
            self.updates.append((query, update))

        async def delete_one(self, query):
            self.deletes.append(query)

    pending = Pending()
    database = SimpleNamespace(db=SimpleNamespace(pending_join_requests=pending))
    event = SimpleNamespace(
        chat=SimpleNamespace(id=-100123, type="channel", username=None),
        old_chat_member=SimpleNamespace(status="left"),
        new_chat_member=SimpleNamespace(status="member", user=SimpleNamespace(id=42)),
    )

    await clear_accepted_private_join_request(event, database)

    assert pending.updates == [
        ({"_id": -100123}, {"$pull": {"users": {"id": 42}}})
    ]
    assert pending.deletes == [{"_id": -100123, "users": {"$size": 0}}]


@pytest.mark.asyncio
async def test_cleanup_worker_only_filters_compact_requests_older_than_15_days():
    class Pending:
        def __init__(self):
            self.update_query = None
            self.pipeline = None
            self.delete_query = None

        async def update_many(self, query, pipeline):
            self.update_query = query
            self.pipeline = pipeline
            return SimpleNamespace(modified_count=2)

        async def delete_many(self, query):
            self.delete_query = query
            return SimpleNamespace(deleted_count=1)

    pending = Pending()
    now = datetime.now(timezone.utc)
    deleted = await cleanup_expired_pending_join_requests(
        SimpleNamespace(pending_join_requests=pending), now=now
    )
    cutoff = now - timedelta(days=15)

    assert deleted == 2
    assert pending.update_query == {"users.t": {"$lte": cutoff}}
    assert pending.pipeline[0]["$set"]["users"]["$filter"]["cond"] == {
        "$gt": ["$$entry.t", cutoff]
    }
    assert pending.delete_query == {"users": {"$size": 0}}


@pytest.mark.asyncio
async def test_join_status_batches_private_pending_requests_into_one_database_query():
    class Settings:
        def find(self, query):
            assert query["key"] == "force_join_channel"
            return Cursor([
                {"channel": "-100123", "mode": "join_request"},
                {"channel": "-100456", "mode": "join_request"},
            ])

    class Pending:
        def __init__(self):
            self.queries = []

        def find(self, query, projection):
            self.queries.append((query, projection))
            return Cursor([
                {"_id": -100123, "users": [{"id": 42, "t": datetime.now(timezone.utc)}]},
            ])

    class Bot:
        async def get_chat_member(self, chat_id, user_id):
            return SimpleNamespace(status="left", is_member=False)

        async def get_chat(self, chat_id):
            return SimpleNamespace(id=int(chat_id), type="channel", username=None)

    pending = Pending()
    db = SimpleNamespace(settings=Settings(), pending_join_requests=pending)

    missing = await join_status(Bot(), 42, ["-100123", "-100456"], db)

    assert missing == ["-100456"]
    assert len(pending.queries) == 1
    assert pending.queries[0][0] == {
        "_id": {"$in": [-100123, -100456]},
        "users.id": 42,
    }


@pytest.mark.asyncio
async def test_public_chat_never_queries_pending_request_collection():
    class Settings:
        def find(self, query):
            return Cursor([{"channel": "@public", "mode": "join_request"}])

    class Pending:
        def find(self, *args, **kwargs):
            raise AssertionError("Public chats must not query pending requests")

    class Bot:
        async def get_chat_member(self, chat_id, user_id):
            return SimpleNamespace(status="left", is_member=False)

        async def get_chat(self, chat_id):
            return SimpleNamespace(id=-100123, type="channel", username="public")

    db = SimpleNamespace(settings=Settings(), pending_join_requests=Pending())
    assert await join_status(Bot(), 42, ["@public"], db) == ["@public"]


@pytest.mark.asyncio
async def test_removing_force_sub_chat_revokes_tracked_link_and_deletes_its_object():
    class Links:
        def __init__(self):
            self.deleted = []

        def find(self, query):
            return Cursor([{
                "channel": "-100123",
                "chat_id": -100123,
                "invite_link": "https://t.me/+private",
            }])

        async def delete_many(self, query):
            self.deleted.append(query)

    class Settings:
        async def find_one(self, query):
            return {"button_url": "https://t.me/+private"}

        async def delete_one(self, query):
            self.deleted = query

    class Pending:
        async def delete_many(self, query):
            self.deleted = query

    class Bot:
        def __init__(self):
            self.revoked = []

        async def revoke_chat_invite_link(self, **kwargs):
            self.revoked.append(kwargs)

    links, settings, pending, bot = Links(), Settings(), Pending(), Bot()
    db = SimpleNamespace(
        force_join_links=links, settings=settings, pending_join_requests=pending
    )

    assert await cleanup_pending_join_requests_for_channel(bot, db, "-100123") == 1
    assert bot.revoked == [{
        "chat_id": -100123, "invite_link": "https://t.me/+private"
    }]
    assert pending.deleted == {"_id": {"$in": [-100123]}}
    assert links.deleted == {"channel": "-100123"}
    assert settings.deleted == {"key": "force_join_channel", "channel": "-100123"}

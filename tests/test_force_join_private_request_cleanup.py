from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.handlers.start import (
    _is_private_chat,
    _pending_request_is_recent,
    force_join_button_label,
    cleanup_expired_pending_join_requests,
    clear_accepted_private_join_request,
    join_status,
)


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
    chat = SimpleNamespace(type=chat_type, username=username)
    assert _is_private_chat(chat) is expected


class FakePendingRequests:
    def __init__(self):
        self.deleted = []

    async def delete_one(self, query):
        self.deleted.append(query)


@pytest.mark.asyncio
async def test_accepted_private_supergroup_request_is_removed():
    pending = FakePendingRequests()
    database = SimpleNamespace(db=SimpleNamespace(pending_join_requests=pending))
    event = SimpleNamespace(
        chat=SimpleNamespace(id=-100123, type="supergroup", username=None),
        old_chat_member=SimpleNamespace(status="left", user=SimpleNamespace(id=42)),
        new_chat_member=SimpleNamespace(status="member", user=SimpleNamespace(id=42)),
    )

    await clear_accepted_private_join_request(event, database)

    assert pending.deleted == [{"chat_id": -100123, "user_id": 42}]


@pytest.mark.asyncio
async def test_public_group_membership_does_not_clear_private_request_record():
    pending = FakePendingRequests()
    database = SimpleNamespace(db=SimpleNamespace(pending_join_requests=pending))
    event = SimpleNamespace(
        chat=SimpleNamespace(id=-100123, type="supergroup", username="public_group"),
        old_chat_member=SimpleNamespace(status="left", user=SimpleNamespace(id=42)),
        new_chat_member=SimpleNamespace(status="member", user=SimpleNamespace(id=42)),
    )

    await clear_accepted_private_join_request(event, database)

    assert pending.deleted == []


@pytest.mark.asyncio
async def test_membership_loss_does_not_count_as_request_acceptance():
    pending = FakePendingRequests()
    database = SimpleNamespace(db=SimpleNamespace(pending_join_requests=pending))
    event = SimpleNamespace(
        chat=SimpleNamespace(id=-100123, type="supergroup", username=None),
        old_chat_member=SimpleNamespace(status="member", user=SimpleNamespace(id=42)),
        new_chat_member=SimpleNamespace(status="left", user=SimpleNamespace(id=42)),
    )

    await clear_accepted_private_join_request(event, database)

    assert pending.deleted == []



def test_pending_request_expires_after_15_days():
    now = datetime.now(timezone.utc)
    assert _pending_request_is_recent({"requested_at": now - timedelta(days=14, hours=23)}, now=now)
    assert not _pending_request_is_recent({"requested_at": now - timedelta(days=15, seconds=1)}, now=now)


class FakePendingRequestCollection:
    def __init__(self):
        self.query = None

    async def delete_many(self, query):
        self.query = query
        return SimpleNamespace(deleted_count=3)


@pytest.mark.asyncio
async def test_cleanup_worker_query_only_targets_requests_older_than_15_days():
    pending = FakePendingRequestCollection()
    db = SimpleNamespace(pending_join_requests=pending)
    now = datetime.now(timezone.utc)

    deleted = await cleanup_expired_pending_join_requests(db, now=now)

    assert deleted == 3
    assert pending.query == {"requested_at": {"$lte": now - timedelta(days=15)}}


@pytest.mark.asyncio
async def test_public_chat_never_passes_from_pending_request_record():
    class FakeBot:
        async def get_chat(self, channel):
            return SimpleNamespace(id=-100123, type="channel", username="public_channel")

        async def get_chat_member(self, chat_id, user_id):
            return SimpleNamespace(status="left", is_member=False)

    class FakeSettings:
        async def find_one(self, query):
            return {"mode": "join_request"}

    class FakePending:
        async def find_one(self, query):
            return {"chat_id": -100123, "user_id": 42, "requested_at": datetime.now(timezone.utc)}

        async def delete_one(self, query):
            raise AssertionError("Public chat verification must not inspect or delete pending records")

    db = SimpleNamespace(settings=FakeSettings(), pending_join_requests=FakePending())

    assert await join_status(FakeBot(), 42, ["@public_channel"], db) == ["@public_channel"]



def test_pending_request_is_expired_at_exactly_15_days():
    now = datetime.now(timezone.utc)
    assert not _pending_request_is_recent({"requested_at": now - timedelta(days=15)}, now=now)


def test_force_join_default_button_label_uses_join_number_not_channel_title():
    assert force_join_button_label({"channel_title": "Actual Private Channel"}, 3) == "Join 3"


def test_force_join_custom_button_label_overrides_join_number():
    assert force_join_button_label({"channel_title": "Actual Channel", "button_text": "Premium Group"}, 3) == "Premium Group"


@pytest.mark.asyncio
async def test_private_channel_membership_passes_even_if_request_record_exists():
    class FakeBot:
        async def get_chat(self, channel):
            return SimpleNamespace(id=-100123, type="channel", username=None)

        async def get_chat_member(self, chat_id, user_id):
            return SimpleNamespace(status="member", is_member=True)

    class FakeSettings:
        async def find_one(self, query):
            return {"mode": "join_request"}

    class FakePending:
        async def find_one(self, query):
            raise AssertionError("Live membership should pass without consulting pending requests")

    db = SimpleNamespace(settings=FakeSettings(), pending_join_requests=FakePending())

    assert await join_status(FakeBot(), 42, ["-100123"], db) == []


@pytest.mark.asyncio
async def test_private_channel_recent_pending_request_passes_when_not_a_member():
    class FakeBot:
        async def get_chat(self, channel):
            return SimpleNamespace(id=-100123, type="channel", username=None)

        async def get_chat_member(self, chat_id, user_id):
            return SimpleNamespace(status="left", is_member=False)

    class FakeSettings:
        async def find_one(self, query):
            return {"mode": "join_request"}

    class FakePending:
        async def find_one(self, query):
            return {"chat_id": -100123, "user_id": 42, "requested_at": datetime.now(timezone.utc)}

    db = SimpleNamespace(settings=FakeSettings(), pending_join_requests=FakePending())

    assert await join_status(FakeBot(), 42, ["-100123"], db) == []


@pytest.mark.asyncio
async def test_private_channel_expired_pending_request_does_not_pass():
    class FakeBot:
        async def get_chat(self, channel):
            return SimpleNamespace(id=-100123, type="channel", username=None)

        async def get_chat_member(self, chat_id, user_id):
            return SimpleNamespace(status="left", is_member=False)

    class FakeSettings:
        async def find_one(self, query):
            return {"mode": "join_request"}

    class FakePending:
        async def find_one(self, query):
            return {"chat_id": -100123, "user_id": 42, "requested_at": datetime.now(timezone.utc) - timedelta(days=15)}

    db = SimpleNamespace(settings=FakeSettings(), pending_join_requests=FakePending())

    assert await join_status(FakeBot(), 42, ["-100123"], db) == ["-100123"]


@pytest.mark.asyncio
async def test_missing_private_force_join_url_is_repaired_for_existing_channel():
    from app.handlers.start import resolve_force_join_button_target

    class SettingsCollection:
        def __init__(self):
            self.updates = []

        async def update_one(self, query, update, upsert=False):
            self.updates.append((query, update, upsert))

    class FakeBot:
        async def get_chat(self, chat_id):
            assert chat_id == -100123
            return SimpleNamespace(id=-100123, title="Private Channel", type="channel", username=None)

        async def create_chat_invite_link(self, **kwargs):
            assert kwargs["chat_id"] == -100123
            assert kwargs["creates_join_request"] is True
            return SimpleNamespace(invite_link="https://t.me/+repaired_invite")

    settings = SettingsCollection()
    db = SimpleNamespace(settings=settings)
    target = await resolve_force_join_button_target(FakeBot(), db, "-100123", {})

    assert target == "https://t.me/+repaired_invite"
    assert settings.updates == [(
        {"key": "force_join_channel", "channel": "-100123"},
        {"$set": {
            "key": "force_join_channel", "channel": "-100123",
            "button_url": "https://t.me/+repaired_invite",
            "channel_title": "Private Channel", "chat_type": "channel",
            "mode": "join_request",
        }},
        True,
    )]


@pytest.mark.asyncio
async def test_stale_private_force_join_invite_is_recreated_when_channel_metadata_differs():
    from app.handlers.start import resolve_force_join_button_target

    class SettingsCollection:
        def __init__(self):
            self.updates = []

        async def update_one(self, query, update, upsert=False):
            self.updates.append((query, update, upsert))

    class FakeBot:
        async def get_chat(self, chat_id):
            return SimpleNamespace(id=-100123, title="Correct Channel", type="channel", username=None)

        async def create_chat_invite_link(self, **kwargs):
            return SimpleNamespace(invite_link="https://t.me/+new_correct_invite")

    settings = SettingsCollection()
    target = await resolve_force_join_button_target(
        FakeBot(),
        SimpleNamespace(settings=settings),
        "-100123",
        {
            "button_url": "https://t.me/+wrong_channel_invite",
            "channel_title": "Another Channel",
            "chat_type": "channel",
            "mode": "join_request",
        },
    )

    assert target == "https://t.me/+new_correct_invite"
    assert settings.updates[0][1]["$set"]["button_url"] == target


@pytest.mark.asyncio
async def test_public_force_join_url_is_canonical_without_unneeded_database_write():
    from app.handlers.start import resolve_force_join_button_target

    class SettingsCollection:
        def __init__(self):
            self.updates = []

        async def update_one(self, query, update, upsert=False):
            self.updates.append((query, update, upsert))

    class FakeBot:
        async def get_chat(self, chat_id):
            return SimpleNamespace(id=-100123, title="Public Channel", type="channel", username="public_channel")

    settings = SettingsCollection()
    target = await resolve_force_join_button_target(
        FakeBot(),
        SimpleNamespace(settings=settings),
        "-100123",
        {
            "button_url": "https://t.me/public_channel",
            "channel_title": "Public Channel",
            "chat_type": "channel",
            "mode": "normal",
        },
    )

    assert target == "https://t.me/public_channel"
    assert settings.updates == []

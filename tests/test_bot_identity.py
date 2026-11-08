import pytest
from types import SimpleNamespace

from app.services.bot_identity import get_bot_identity


@pytest.mark.asyncio
async def test_cached_bot_identity_avoids_telegram_request():
    identity = SimpleNamespace(id=123, username="cached_bot")

    class FakeBot:
        me = identity

        async def get_me(self):
            raise AssertionError("Cached bot identity should be reused")

    assert await get_bot_identity(FakeBot()) is identity


@pytest.mark.asyncio
async def test_bot_identity_is_fetched_when_not_cached():
    identity = SimpleNamespace(id=456, username="first_fetch")

    class FakeBot:
        @property
        def me(self):
            raise RuntimeError("Bot identity has not been fetched")

        async def get_me(self):
            return identity

    assert await get_bot_identity(FakeBot()) is identity

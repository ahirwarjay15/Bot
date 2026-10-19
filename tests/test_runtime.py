import asyncio
import json

import pytest

import main


@pytest.mark.asyncio
async def test_health_reports_starting_until_service_is_ready(monkeypatch):
    monkeypatch.setattr(main, "SERVICE_READY", False)
    starting = await main.health(None)
    assert starting.status == 503
    assert json.loads(starting.text) == {"status": "starting", "service": "Titania"}

    monkeypatch.setattr(main, "SERVICE_READY", True)
    ready = await main.health(None)
    assert ready.status == 200
    assert json.loads(ready.text) == {"status": "ok", "service": "Titania"}


@pytest.mark.asyncio
async def test_database_startup_retries_with_bounded_backoff(monkeypatch):
    class FakeDatabase:
        def __init__(self):
            self.attempts = 0

        async def connect(self):
            self.attempts += 1
            if self.attempts < 3:
                raise ConnectionError("temporary database outage")

    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    database = FakeDatabase()
    await main.connect_database_with_retry(database, initial_delay=5, max_delay=10)

    assert database.attempts == 3
    assert delays == [5, 10]


@pytest.mark.asyncio
async def test_database_startup_retry_does_not_swallow_cancellation(monkeypatch):
    class FakeDatabase:
        async def connect(self):
            raise ConnectionError("temporary database outage")

    async def cancel_sleep(_delay):
        raise asyncio.CancelledError()

    monkeypatch.setattr(main.asyncio, "sleep", cancel_sleep)
    with pytest.raises(asyncio.CancelledError):
        await main.connect_database_with_retry(FakeDatabase(), initial_delay=1, max_delay=2)

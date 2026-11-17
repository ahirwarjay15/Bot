import pytest
from aiogram.exceptions import TelegramRetryAfter

from app.handlers.anime import _retry_step


def _retry_after(seconds):
    error = TelegramRetryAfter.__new__(TelegramRetryAfter)
    Exception.__init__(error, "Telegram flood control")
    error.retry_after = seconds
    return error


@pytest.mark.asyncio
async def test_anime_retries_flood_control_until_telegram_allows_send(monkeypatch):
    attempts = 0
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts <= 6:
            raise _retry_after(2)
        return "sent"

    monkeypatch.setattr("app.handlers.anime.asyncio.sleep", fake_sleep)

    result = await _retry_step(operation, label="test send")

    assert result == "sent"
    assert attempts == 7
    assert waits == [3.0] * 6

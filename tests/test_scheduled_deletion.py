import pytest

from app.services.delivery import delete_scheduled_messages


@pytest.mark.asyncio
async def test_scheduled_message_deletion_batches_up_to_100_ids_per_request():
    class FakeBot:
        def __init__(self):
            self.calls = []

        async def delete_messages(self, *, chat_id, message_ids):
            self.calls.append((chat_id, list(message_ids)))
            return True

    bot = FakeBot()
    ids = list(range(1, 206))
    ids.extend([1, None])

    await delete_scheduled_messages(bot, 42, ids)

    assert [len(chunk) for _, chunk in bot.calls] == [100, 100, 5]
    assert all(chat_id == 42 for chat_id, _ in bot.calls)
    assert bot.calls[0][1][0] == 1
    assert bot.calls[-1][1] == [201, 202, 203, 204, 205]


@pytest.mark.asyncio
async def test_scheduled_message_deletion_skips_empty_id_list():
    class FakeBot:
        async def delete_messages(self, **kwargs):
            raise AssertionError("No Telegram request should be made for an empty task")

    await delete_scheduled_messages(FakeBot(), 42, [])


@pytest.mark.asyncio
async def test_idle_delivery_worker_waits_for_scheduler_wake(monkeypatch):
    import asyncio
    import app.services.delivery as delivery

    class EmptyCursor:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class Tasks:
        def find(self, *args, **kwargs):
            return EmptyCursor()

        async def find_one(self, *args, **kwargs):
            return None

    class DB:
        tasks = Tasks()

    async def stop_after_sleep(seconds):
        assert seconds == 60
        raise asyncio.CancelledError

    monkeypatch.setattr(delivery.asyncio, "sleep", stop_after_sleep)
    with pytest.raises(asyncio.CancelledError):
        await delivery.run_task_worker(None, DB())

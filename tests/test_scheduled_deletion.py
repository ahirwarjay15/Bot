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
async def test_idle_delivery_worker_uses_change_stream_and_bulk_startup_recovery(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import app.services.delivery as delivery

    class Tasks:
        def __init__(self):
            self.updated = None
            self.deleted = None

        async def update_many(self, query, update):
            self.updated = (query, update)
            return SimpleNamespace(modified_count=0)

        async def delete_many(self, query):
            self.deleted = query
            return SimpleNamespace(deleted_count=0)

    tasks = Tasks()

    class DB:
        pass

    db = DB()
    db.tasks = tasks

    async def stop_watcher(_db, _wake_event, ready_event):
        ready_event.set()
        raise asyncio.CancelledError

    monkeypatch.setattr(delivery, "_watch_pending_delivery_tasks", stop_watcher)
    with pytest.raises(asyncio.CancelledError):
        await delivery.run_task_worker(None, db)

    assert tasks.updated[0]["status"] == "delivering"
    assert tasks.updated[0]["message_ids"] == {"$exists": True, "$ne": []}
    assert tasks.deleted["$and"][0]["status"] == "delivering'

from datetime import datetime, timedelta, timezone

from app.handlers.broadcast import (
    _eligible_broadcast_recipient_filter,
    _retry_wait_seconds,
)


def test_broadcast_claim_skips_recipients_until_retry_deadline():
    now = datetime(2026, 11, 8, 4, 0, tzinfo=timezone.utc)
    query = _eligible_broadcast_recipient_filter("job-1", now)

    assert query == {
        "job_id": "job-1",
        "status": "pending",
        "$or": [
            {"retry_at": {"$exists": False}},
            {"retry_at": {"$lte": now}},
        ],
    }


def test_retry_wait_seconds_supports_naive_mongodb_utc_dates():
    now = datetime(2026, 11, 8, 4, 0, tzinfo=timezone.utc)
    retry_at = (now + timedelta(seconds=12)).replace(tzinfo=None)

    assert _retry_wait_seconds(retry_at, now) == 12


def test_retry_wait_seconds_never_returns_zero():
    now = datetime(2026, 11, 8, 4, 0, tzinfo=timezone.utc)

    assert _retry_wait_seconds(now - timedelta(seconds=5), now) == 0.1



def test_broadcast_change_stream_subscribes_to_recipient_progress():
    import asyncio
    import pytest
    from app.handlers.broadcast import _watch_broadcast_changes

    @pytest.mark.asyncio
    async def run():
        class Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def __aiter__(self):
                async def changes():
                    yield {"operationType": "update", "ns": {"coll": "broadcast_recipients"}}
                return changes()

        class DB:
            async def watch(self, *, pipeline, full_document, max_await_time_ms):
                assert full_document == "updateLookup"
                assert max_await_time_ms == 600_000
                match = pipeline[0]["$match"]["$or"]
                assert any(item.get("ns.coll") == "broadcast_recipients" for item in match)
                return Stream()

        wake_event = asyncio.Event()
        ready_event = asyncio.Event()
        await _watch_broadcast_changes(DB(), wake_event, ready_event)
        assert ready_event.is_set()
        assert wake_event.is_set()

    asyncio.run(run())

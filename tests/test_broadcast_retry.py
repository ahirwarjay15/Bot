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

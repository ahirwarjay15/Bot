from app.database.users import BANNED_DOCUMENT_ID, MAX_USERS_PER_DOCUMENT


def test_active_user_bucket_limit_is_one_lakh():
    assert MAX_USERS_PER_DOCUMENT == 100_000


def test_banned_user_storage_uses_one_stable_document():
    assert str(BANNED_DOCUMENT_ID) == "000000000000000000000001"

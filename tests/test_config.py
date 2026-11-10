import pytest
from pydantic import ValidationError

from app.config import Settings


def _set_required_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:fake-token")
    monkeypatch.setenv("OWNER_ID", "12345")
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    monkeypatch.setenv("MONGODB_DB_NAME", "test_bot")


def test_force_join_channels_accept_comma_separated_values(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("FORCE_JOIN_CHANNELS", "@one, -100123, @two ")
    settings = Settings(_env_file=None)
    assert settings.force_join_channels == ["@one", "-100123", "@two"]


def test_owner_id_must_be_positive(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("OWNER_ID", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_optional_source_channel_is_empty_by_default(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("SOURCE_CHANNEL_ID", raising=False)
    monkeypatch.delenv("DB_CHANNEL_ID", raising=False)
    settings = Settings(_env_file=None)
    assert settings.source_channel_id is None


def test_blank_optional_source_channel_is_treated_as_unset(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("SOURCE_CHANNEL_ID", "")
    settings = Settings(_env_file=None)
    assert settings.source_channel_id is None


def test_legacy_db_channel_id_alias_is_supported(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("SOURCE_CHANNEL_ID", raising=False)
    monkeypatch.setenv("DB_CHANNEL_ID", "-100123456789")
    settings = Settings(_env_file=None)
    assert settings.source_channel_id == -100123456789



def test_owner_id_accepts_comma_separated_ids(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("OWNER_ID", "12345,23456,34567")
    monkeypatch.delenv("OWNER_IDS", raising=False)
    settings = Settings(_env_file=None)
    assert settings.owner_ids == [12345, 23456, 34567]
    assert settings.owner_id == 12345


def test_owner_ids_alias_accepts_comma_separated_ids(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("OWNER_ID", raising=False)
    monkeypatch.setenv("OWNER_IDS", "12345,23456")
    settings = Settings(_env_file=None)
    assert settings.owner_ids == [12345, 23456]


def test_numbered_owner_environment_variables_are_supported(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("OWNER_ID", "12345")
    monkeypatch.setenv("OWNER_ID_2", "23456")
    monkeypatch.setenv("OWNER_ID_3", "34567")
    settings = Settings(_env_file=None)
    assert settings.owner_ids == [12345, 23456, 34567]


def test_owner_ids_are_deduplicated_and_must_be_positive(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("OWNER_ID", "12345,23456,12345")
    monkeypatch.setenv("OWNER_ID_2", "23456")
    settings = Settings(_env_file=None)
    assert settings.owner_ids == [12345, 23456]

    monkeypatch.setenv("OWNER_IDS", "12345,0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)

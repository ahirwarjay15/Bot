import pytest
from pydantic import ValidationError
from app.config import Settings

def test_force_join_channels_accept_comma_separated_values(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN","123456:fake-token")
    monkeypatch.setenv("OWNER_ID","12345")
    monkeypatch.setenv("MONGODB_URI","mongodb://localhost:27017")
    monkeypatch.setenv("FORCE_JOIN_CHANNELS","@one, -100123, @two ")
    settings=Settings(_env_file=None)
    assert settings.force_join_channels==["@one","-100123","@two"]

def test_owner_id_must_be_positive(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN","123456:fake-token")
    monkeypatch.setenv("OWNER_ID","0")
    monkeypatch.setenv("MONGODB_URI","mongodb://localhost:27017")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)

def test_optional_source_channel_is_empty_by_default(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN","123456:fake-token")
    monkeypatch.setenv("OWNER_ID","12345")
    monkeypatch.setenv("MONGODB_URI","mongodb://localhost:27017")
    monkeypatch.delenv("SOURCE_CHANNEL_ID",raising=False)
    settings=Settings(_env_file=None)
    assert settings.source_channel_id is None


def test_blank_optional_source_channel_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:fake-token")
    monkeypatch.setenv("OWNER_ID", "12345")
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    monkeypatch.setenv("SOURCE_CHANNEL_ID", "")
    settings = Settings(_env_file=None)
    assert settings.source_channel_id is None

"""Validated environment configuration; secrets belong in .env, never Git."""
from functools import lru_cache
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False, enable_decoding=False)
    bot_token: SecretStr = Field(validation_alias="BOT_TOKEN")
    owner_id: int = Field(validation_alias="OWNER_ID", gt=0)
    mongodb_uri: SecretStr = Field(validation_alias="MONGODB_URI")
    mongodb_db_name: str = Field(default="file_bot", validation_alias="MONGODB_DB_NAME")
    source_channel_id: int | None = Field(default=None, validation_alias="SOURCE_CHANNEL_ID")
    force_join_channels: list[str] = Field(default_factory=list, validation_alias="FORCE_JOIN_CHANNELS")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    @field_validator("source_channel_id", mode="before")
    @classmethod
    def parse_optional_source_channel(cls, value):
        if value is None or value == "":
            return None
        return value

    @field_validator("force_join_channels", mode="before")
    @classmethod
    def parse_force_join_channels(cls, value):
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

"""Validated environment configuration; secrets belong in .env, never Git."""
import os
import re
from functools import lru_cache

from dotenv import dotenv_values
from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        enable_decoding=False,
    )

    bot_token: SecretStr = Field(validation_alias="BOT_TOKEN")
    owner_ids: list[int] = Field(validation_alias=AliasChoices("OWNER_IDS", "OWNER_ID"))
    mongodb_uri: SecretStr = Field(validation_alias="MONGODB_URI")
    mongodb_db_name: str = Field(validation_alias="MONGODB_DB_NAME", min_length=1)
    source_channel_id: int | None = Field(
        default=None,
        validation_alias=AliasChoices("SOURCE_CHANNEL_ID", "DB_CHANNEL_ID"),
    )
    force_join_channels: list[str] = Field(default_factory=list, validation_alias="FORCE_JOIN_CHANNELS")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    @field_validator("owner_ids", mode="before")
    @classmethod
    def parse_owner_ids(cls, value):
        if isinstance(value, str):
            value = value.split(",")
        if value is None:
            return []
        if not isinstance(value, (list, tuple, set)):
            value = [value]
        parsed = []
        for item in value:
            if item is None or str(item).strip() == "":
                continue
            owner_id = int(str(item).strip())
            if owner_id <= 0:
                raise ValueError("Owner IDs must be positive integers")
            if owner_id not in parsed:
                parsed.append(owner_id)
        return parsed

    @model_validator(mode="after")
    def collect_numbered_owner_ids(self):
        """Merge OWNER_ID/OWNER_IDS and any OWNER_ID_N variables, preserving order."""
        values = list(self.owner_ids)
        sources = [os.environ]
        env_file = self.model_config.get("env_file")
        if env_file:
            paths = (env_file,) if isinstance(env_file, (str, os.PathLike)) else env_file
            for path in paths:
                file_values = dotenv_values(path)
                sources.append({str(key).upper(): value for key, value in file_values.items() if value is not None})

        for source in sources:
            for key in ("OWNER_IDS", "OWNER_ID"):
                raw = source.get(key)
                if raw is not None:
                    values.extend(self.parse_owner_ids(raw))
            numbered = []
            for key, raw in source.items():
                match = re.fullmatch(r"OWNER_ID_(\d+)", str(key).upper())
                if match and raw is not None:
                    numbered.append((int(match.group(1)), raw))
            for _index, raw in sorted(numbered):
                values.extend(self.parse_owner_ids(raw))

        self.owner_ids = list(dict.fromkeys(values))
        if not self.owner_ids:
            raise ValueError("Configure at least one owner using OWNER_ID or OWNER_IDS")
        return self

    @property
    def owner_id(self) -> int:
        """Backward-compatible primary owner for older call sites."""
        return self.owner_ids[0]

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

"""MongoDB persistence for file references, users, batches, and never-reused codes."""
import asyncio
from datetime import datetime, timezone
from secrets import choice
from string import ascii_letters, digits

_CODE_ALPHABET = ascii_letters + digits


def build_deeplink_token(code: str) -> str:
    """Build public token: stored five-character code + random 15-character suffix."""
    if len(code) != 5 or any(char not in _CODE_ALPHABET for char in code):
        raise ValueError("Deepcode must be exactly 5 alphanumeric characters")
    return code + "".join(choice(_CODE_ALPHABET) for _ in range(15))


def _now():
    return datetime.now(timezone.utc)


class FileRepository:
    def __init__(self, db):
        self.db = db

    async def reserve_code(self, length: int, kind: str) -> str:
        """Reserve a random alphanumeric code permanently, even if a session is abandoned."""
        if length != 5:
            raise ValueError("Deepcodes must be exactly 5 characters")
        for _ in range(100):
            code = "".join(choice(_CODE_ALPHABET) for _ in range(length))
            # Check legacy records concurrently; this avoids three serial DB round trips
            # on every new deeplink while preserving the never-reuse guarantee.
            legacy_matches = await asyncio.gather(
                self.db.files.find_one({"file_key": code}, {"_id": 1}),
                self.db.batches.find_one({"batch_id": code}, {"_id": 1}),
                self.db.batch_sessions.find_one({"batch_id": code}, {"_id": 1}),
            )
            if any(legacy_matches):
                continue
            try:
                await self.db.deeplink_codes.insert_one({
                    "code": code, "length": length, "kind": kind, "reserved_at": _now()
                })
                return code
            except Exception as exc:
                if getattr(exc, "code", None) == 11000:
                    continue
                raise
        raise RuntimeError("Could not allocate a never-used deeplink code")

    async def save_file(
        self, *, source_chat_id: int, source_message_id: int,
        reply_markup: dict | None = None,
    ) -> dict:
        for _ in range(8):
            key = await self.reserve_code(5, "file")
            doc = {
                "file_key": key,
                "source_chat_id": source_chat_id,
                "source_message_id": source_message_id,
                **({"reply_markup": reply_markup} if reply_markup else {}),
            }
            try:
                await self.db.files.insert_one(doc)
                return doc
            except Exception as exc:
                if getattr(exc, "code", None) != 11000:
                    raise
        raise RuntimeError("Could not allocate a unique file key")

    async def get_file(self, key: str) -> dict | None:
        """Find either a single file or a batch in the unified files collection."""
        record = await self.db.files.find_one({"file_key": key})
        if record:
            return record
        # Read old batch links during migration; new batches use files.
        legacy = await self.db.batches.find_one({"batch_id": key})
        if legacy:
            legacy["file_key"] = legacy.get("batch_id")
            return legacy
        return None

    async def is_batch_record(self, record: dict) -> bool:
        return bool(record and isinstance(record.get("items"), list))

    async def save_batch(
        self, items: list[dict], batch_id: str | None = None
    ) -> dict:
        if len(items) < 2:
            raise ValueError("A batch must contain at least 2 messages")
        if batch_id:
            doc = {"file_key": batch_id, "items": items}
            await self.db.files.update_one(
                {"file_key": batch_id}, {"$set": doc}, upsert=True
            )
            return await self.db.files.find_one({"file_key": batch_id})
        for _ in range(8):
            key = await self.reserve_code(5, "batch")
            doc = {"file_key": key, "items": items}
            try:
                await self.db.files.insert_one(doc)
                return doc
            except Exception as exc:
                if getattr(exc, "code", None) != 11000:
                    raise
        raise RuntimeError("Could not allocate a unique batch key")

    async def get_batch(self, batch_id: str) -> dict | None:
        record = await self.db.files.find_one({"file_key": batch_id, "items": {"$exists": True}})
        if record:
            return record
        return await self.db.batches.find_one({"batch_id": batch_id})

    async def save_user(self, user) -> None:
        if user is None:
            return
        now = _now()
        await self.db.users.update_one(
            {"user_id": user.id},
            {"$set": {"first_name": user.first_name, "last_name": user.last_name,
                      "username": user.username, "last_seen": now, "blocked": False},
             "$setOnInsert": {"created_at": now, "banned": False}},
            upsert=True,
        )

    async def is_banned(self, user_id: int) -> bool:
        user = await self.db.users.find_one({"user_id": user_id}, {"banned": 1})
        return bool(user and user.get("banned", False))

    async def get_setting(self, key: str, default=None):
        item = await self.db.settings.find_one({"key": key, "channel": {"$exists": False}}, {"value": 1})
        return item.get("value", default) if item else default

    async def set_setting(self, key: str, value) -> None:
        await self.db.settings.update_one(
            {"key": key, "channel": {"$exists": False}},
            {"$set": {"key": key, "value": value}},
            upsert=True
        )

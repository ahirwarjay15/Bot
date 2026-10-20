"""MongoDB persistence for file references, users, batches, and never-reused codes."""
from datetime import datetime, timezone
from secrets import choice
from string import ascii_letters, digits

_CODE_ALPHABET = ascii_letters + digits


def _now():
    return datetime.now(timezone.utc)


class FileRepository:
    def __init__(self, db):
        self.db = db

    async def reserve_code(self, length: int, kind: str) -> str:
        """Reserve a random alphanumeric code permanently, even if a session is abandoned."""
        if length not in (10, 15, 20):
            raise ValueError("Deeplink codes must be 10, 15, or 20 characters")
        for _ in range(100):
            code = "".join(choice(_CODE_ALPHABET) for _ in range(length))
            # Prevent collisions with links created before the permanent registry existed.
            if await self.db.files.find_one({"file_key": code}, {"_id": 1}):
                continue
            if await self.db.batches.find_one({"batch_id": code}, {"_id": 1}):
                continue
            if await self.db.batch_sessions.find_one({"batch_id": code}, {"_id": 1}):
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
            key = await self.reserve_code(20, "file")
            doc = {
                "file_key": key,
                "source_chat_id": source_chat_id,
                "source_message_id": source_message_id,
                **({"reply_markup": reply_markup} if reply_markup else {}),
                "kind": "file",
            }
            try:
                await self.db.files.insert_one(doc)
                return doc
            except Exception as exc:
                if getattr(exc, "code", None) != 11000:
                    raise
        raise RuntimeError("Could not allocate a unique file key")

    async def get_file(self, key: str) -> dict | None:
        return await self.db.files.find_one({"file_key": key, "kind": "file"})

    async def save_batch(
        self, items: list[dict], batch_id: str | None = None
    ) -> dict:
        if len(items) < 2 or len(items) > 100:
            raise ValueError("A batch must contain 2 to 100 files")
        if batch_id:
            doc = {
                "batch_id": batch_id, "items": items, "kind": "batch",
            }
            await self.db.batches.update_one(
                {"batch_id": batch_id, "kind": "batch"},
                {"$set": doc},
                upsert=True,
            )
            return await self.db.batches.find_one({"batch_id": batch_id, "kind": "batch"})
        for _ in range(8):
            key = await self.reserve_code(20, "batch")
            doc = {
                "batch_id": key, "items": items, "kind": "batch",
            }
            try:
                await self.db.batches.insert_one(doc)
                return doc
            except Exception as exc:
                if getattr(exc, "code", None) != 11000:
                    raise
        raise RuntimeError("Could not allocate a unique batch key")

    async def get_batch(self, batch_id: str) -> dict | None:
        return await self.db.batches.find_one({"batch_id": batch_id, "kind": "batch"})

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
        item = await self.db.settings.find_one({"key": key}, {"value": 1})
        return item.get("value", default) if item else default

    async def set_setting(self, key: str, value) -> None:
        await self.db.settings.update_one(
            {"key": key}, {"$set": {"key": key, "value": value}}, upsert=True
        )

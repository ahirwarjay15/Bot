"""MongoDB persistence for compact single-file and batch deeplinks."""
from datetime import datetime, timezone
from secrets import choice
from string import ascii_letters, digits
import logging

log = logging.getLogger(__name__)

_CODE_ALPHABET = ascii_letters + digits

def build_deeplink_token(code: str) -> str:
    if len(code) != 7 or any(char not in _CODE_ALPHABET for char in code):
        raise ValueError("Deepcode must be exactly 7 alphanumeric characters")
    return code + "".join(choice(_CODE_ALPHABET) for _ in range(15))

def _now():
    return datetime.now(timezone.utc)

class FileRepository:
    def __init__(self, db):
        self.db = db

    async def reserve_code(self, length: int) -> str:
        if length != 7:
            raise ValueError("Deepcodes must be exactly 7 characters")
        for _ in range(100):
            code = "".join(choice(_CODE_ALPHABET) for _ in range(length))
            if not await self.db.files.find_one({"$or": [{"key": code}, {"file_key": code}]}, {"_id": 1}):
                return code
        raise RuntimeError("Could not allocate a unique Deepcode")

    async def _insert_unique(self, doc: dict) -> dict:
        for _ in range(8):
            try:
                await self.db.files.insert_one(doc)
                return doc
            except Exception as exc:
                if getattr(exc, "code", None) != 11000:
                    raise
                doc["key"] = await self.reserve_code(7)
        raise RuntimeError("Could not allocate a unique file key")

    async def save_file(self, *, source_chat_id: int | None = None,
                        source_message_id: int, reply_markup: dict | None = None,
                        buttons: list | None = None) -> dict:
        # source_chat_id is accepted for backwards compatibility but isn't stored.
        for attempt in range(8):
            key = await self.reserve_code(7)
            doc = {"key": key, "id": int(source_message_id)}
            compact = buttons if buttons else None
            if compact:
                doc["buttons"] = compact
            elif reply_markup:
                doc["reply_markup"] = reply_markup
            try:
                await self.db.files.insert_one(doc)
                return doc
            except Exception as exc:
                if getattr(exc, "code", None) != 11000:
                    log.exception("File record insert failed (attempt %s)", attempt + 1)
                    raise
                log.warning("Duplicate file key collision on insert (attempt %s/8)", attempt + 1)
        raise RuntimeError("Could not allocate a unique file key after duplicate-key retries")

    async def get_file(self, key: str) -> dict | None:
        return await self.db.files.find_one({"$or": [{"key": key}, {"file_key": key}]})

    async def is_batch_record(self, record: dict) -> bool:
        return bool(record and (isinstance(record.get("items"), list)
                                or isinstance(record.get("source_message_id"), list)
                                or isinstance(record.get("id"), dict)))

    async def save_batch(self, items: list, batch_id: str | None = None,
                         buttons: dict | None = None) -> dict:
        # New compact API accepts a list of message IDs. Legacy dict items are normalized.
        ids = []
        for item in items:
            ids.append(int(item.get("source_message_id")) if isinstance(item, dict) else int(item))
        if len(ids) < 2:
            raise ValueError("A batch must contain at least 2 messages")
        key = batch_id or await self.reserve_code(7)
        doc = {"key": key, "id": {f"{i:02d}": mid for i, mid in enumerate(ids, 1)}}
        if buttons:
            doc["buttons"] = {str(k): v for k, v in buttons.items() if v}
        if batch_id:
            await self.db.files.update_one({"$or": [{"key": batch_id}, {"file_key": batch_id}]}, {"$set": doc}, upsert=True)
            return await self.db.files.find_one({"key": batch_id})
        return await self._insert_unique(doc)

    async def get_batch(self, batch_id: str) -> dict | None:
        return await self.db.files.find_one({"$and": [{"$or": [{"key": batch_id}, {"file_key": batch_id}]}, {"$or": [{"id": {"$type": "object"}}, {"source_message_id": {"$type": "array"}}, {"items": {"$exists": True}}]}]})

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
            {"$set": {"key": key, "value": value}}, upsert=True
        )

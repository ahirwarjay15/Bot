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
            if not await self.db.files.find_one({"key": code}, {"_id": 1}):
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
                        buttons: dict | list | None = None) -> dict:
        # source_chat_id is accepted for backwards compatibility but isn't stored.
        for attempt in range(8):
            key = await self.reserve_code(7)
            doc = {"key": key, "id": str(int(source_message_id))}
            if buttons:
                doc["buttons"] = [
                    "|".join([str(int(source_message_id)), *[str(part) for part in entry]])
                    for entry in buttons
                    if isinstance(entry, (list, tuple)) and len(entry) >= 4
                ]
            elif reply_markup:
                doc["reply_markup"] = reply_markup
            try:
                await self.db.files.insert_one(doc)
                return doc
            except Exception as exc:
                if getattr(exc, "code", None) != 11000:
                    log.exception("File record insert failed (attempt %s)", attempt + 1)
                    raise
                log.warning("Duplicate-key insert conflict (attempt %s/8): %s", attempt + 1, exc)
        raise RuntimeError("Could not allocate a unique file key after duplicate-key retries")

    async def get_file(self, key: str) -> dict | None:
        return await self.db.files.find_one({"key": key})

    async def is_batch_record(self, record: dict) -> bool:
        ids = record.get("id") if record else None
        return bool(record and (
            isinstance(record.get("items"), list)
            or isinstance(record.get("source_message_id"), list)
            or isinstance(ids, dict)
            or (isinstance(ids, list) and len(ids) > 1)
            or (isinstance(ids, str) and "," in ids)
        ))

    async def save_batch(self, items: list, batch_id: str | None = None,
                         chat_id: int | None = None,
                         buttons: dict | None = None) -> dict:
        # Store per-message compact button maps only when supplied.
        ids = [
            int(item.get("source_message_id")) if isinstance(item, dict) else int(item)
            for item in items
        ]
        if len(ids) < 2:
            raise ValueError("A batch must contain at least 2 messages")
        key = batch_id or await self.reserve_code(7)
        doc = {"key": key, "id": ",".join(str(mid) for mid in ids)}
        if chat_id is not None:
            doc["chat"] = int(chat_id)
        if buttons:
            doc["buttons"] = [
                "|".join([str(mid), *[str(part) for part in entry]])
                for mid in ids
                for entry in buttons.get(str(mid), [])
                if isinstance(entry, (list, tuple)) and len(entry) >= 4
            ] or None
            if doc.get("buttons") is None:
                doc.pop("buttons", None)
        if batch_id:
            unset_fields = {}
            if not doc.get("buttons"):
                unset_fields["buttons"] = ""
            update = {"$set": doc, "$unset": unset_fields}
            if chat_id is None:
                update["$unset"]["chat"] = ""
            await self.db.files.update_one({"key": batch_id}, update, upsert=True)
            return await self.db.files.find_one({"key": batch_id})
        return await self._insert_unique(doc)

    async def get_batch(self, batch_id: str) -> dict | None:
        return await self.db.files.find_one({"key": batch_id, "$or": [
            {"id": {"$type": "object"}},
            {"id.1": {"$exists": True}},
            {"id": {"$regex": ","}},
            {"source_message_id": {"$type": "array"}},
            {"items": {"$exists": True}},
        ]})

    async def save_user(self, user) -> None:
        if user is None:
            return
        await self.db.users.update_one(
            {"user_id": user.id},
            {"$setOnInsert": {"user_id": user.id, "banned": False}},
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

from app.config import Settings

async def is_owner_or_moderator(db, user_id: int, settings: Settings) -> bool:
    if user_id == settings.owner_id:
        return True
    return await db.moderators.find_one({"user_id": user_id}, {"_id": 1}) is not None

async def is_banned(db, user_id: int) -> bool:
    doc = await db.users.find_one({"user_id": user_id}, {"banned": 1})
    return bool(doc and doc.get("banned"))

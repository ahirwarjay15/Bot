"""Owner/moderator administration, ban controls, and stats popup."""
from datetime import datetime, timezone
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from app.config import Settings
from app.services.permissions import is_owner_or_moderator

router = Router(name="admin")

@router.message(Command("ban"))
async def ban_user(message: Message, settings: Settings, database):
    if not message.from_user or not await is_owner_or_moderator(database.db, message.from_user.id, settings):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) <= 0:
        await message.answer("Usage: /ban TELEGRAM_USER_ID")
        return
    uid = int(parts[1])
    if uid == settings.owner_id or await database.db.moderators.find_one({"user_id": uid}):
        await message.answer("Owner/moderators cannot be banned.")
        return
    await database.db.users.update_one(
        {"user_id": uid}, {"$set": {"banned": True}, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}}, upsert=True
    )
    await message.answer(f"User {uid} banned.")

@router.message(Command("unban"))
async def unban_user(message: Message, settings: Settings, database):
    if not message.from_user or not await is_owner_or_moderator(database.db, message.from_user.id, settings):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) <= 0:
        await message.answer("Usage: /unban TELEGRAM_USER_ID")
        return
    uid = int(parts[1])
    result = await database.db.users.update_one({"user_id": uid, "banned": True}, {"$set": {"banned": False}})
    if not result.modified_count:
        await message.answer("That user is not currently banned.")
        return
    await message.answer(f"User {uid} unbanned.")

@router.message(Command("addmod"))
async def add_mod(message: Message, settings: Settings, database):
    if not message.from_user or message.from_user.id != settings.owner_id:
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) <= 0:
        await message.answer("Usage: /addmod TELEGRAM_USER_ID")
        return
    uid = int(parts[1])
    if uid == settings.owner_id:
        await message.answer("Owner is not stored as a moderator.")
        return
    banned_user = await database.db.users.find_one({"user_id": uid, "banned": True})
    if banned_user:
        await message.answer("Unban this user before adding them as a moderator.")
        return
    existing = await database.db.moderators.find_one({"user_id": uid})
    if existing:
        await message.answer("This user is already a moderator.")
        return
    if await database.db.moderators.count_documents({}) >= 3:
        await message.answer("Maximum 3 moderators allowed.")
        return
    user = await database.db.users.find_one({"user_id": uid})
    await database.db.moderators.insert_one({
        "user_id": uid,
        "first_name": (user or {}).get("first_name", f"User {uid}"),
        "username": (user or {}).get("username"),
    })
    await message.answer(f"Successfully added {(user or {}).get('first_name', f'User {uid}')} as moderator.")

@router.message(Command("delmod"))
async def del_mod(message: Message, settings: Settings, database):
    if not message.from_user or message.from_user.id != settings.owner_id:
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Usage: /delmod TELEGRAM_USER_ID")
        return
    result = await database.db.moderators.delete_one({"user_id": int(parts[1])})
    await message.answer("Moderator removed." if result.deleted_count else "That user is not a moderator.")

@router.message(Command("stats"))
async def stats(message: Message, settings: Settings, database):
    if not message.from_user or not await is_owner_or_moderator(database.db, message.from_user.id, settings):
        return
    total = await database.db.users.count_documents({})
    blocked = await database.db.users.count_documents({"blocked": True})
    me = await message.bot.get_me()
    text = f"{me.full_name}\n\nUSERS: {total}\n\nBLOCKED: {blocked}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="OK", callback_data="stats:ok")]])
    await message.answer(text, reply_markup=keyboard)

@router.callback_query(F.data == "stats:ok")
async def stats_ok(query: CallbackQuery, settings: Settings, database):
    if not query.from_user or not await is_owner_or_moderator(database.db, query.from_user.id, settings):
        await query.answer("Not authorized.", show_alert=True)
        return
    total = await database.db.users.count_documents({})
    blocked = await database.db.users.count_documents({"$or": [{"blocked": True}, {"last_delivery_error": {"$exists": True}}]})
    me = await query.bot.get_me()
    await query.answer(f"{me.full_name}\n\nUSERS: {total}\n\nBLOCKED: {blocked}", show_alert=True)

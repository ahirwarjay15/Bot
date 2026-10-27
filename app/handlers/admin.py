"""Owner/moderator administration, ban controls, and stats popup."""
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
        {"user_id": uid}, {"$set": {"banned": True}, "$setOnInsert": {"user_id": uid}}, upsert=True
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
    await database.db.moderators.insert_one({"user_id": uid, "first_name": f"User {uid}"})
    await message.answer(f"Successfully added User {uid} as moderator.")

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

async def build_stats_text(bot, db):
    """Build the same compact stats alert for /stats and the Settings > Stats button."""
    total = await db.users.count_documents({})
    banned = await db.users.count_documents({"banned": True})
    # Keep blocked delivery state on the user record so Stats remains correct
    # after temporary broadcast recipient rows are deleted.
    blocked = await db.users.count_documents({"blocked": True})
    me = await bot.get_me()
    return f"{me.full_name}\n\n⛦ Total Users: {total}\n⛦ Banned Users: {banned}\n⛦ Blocked / Unreachable: {blocked}"


@router.message(Command("stats"))
async def stats(message: Message, settings: Settings, database):
    if not message.from_user or not await is_owner_or_moderator(database.db, message.from_user.id, settings):
        return
    # Telegram alert popups can only be opened from a callback button.
    # Keep this command response minimal; the button shows the stats in an alert.
    await message.answer(
        "📊 View bot statistics",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="STATS", callback_data="stats:show")]
        ]),
    )


@router.callback_query(F.data == "stats:show")
async def stats_show(query: CallbackQuery, settings: Settings, database):
    if not query.from_user or not await is_owner_or_moderator(database.db, query.from_user.id, settings):
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer(await build_stats_text(query.bot, database.db), show_alert=True)


@router.callback_query(F.data == "stats:ok")
async def stats_ok(query: CallbackQuery, settings: Settings, database):
    if not query.from_user or not await is_owner_or_moderator(database.db, query.from_user.id, settings):
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer(await build_stats_text(query.bot, database.db), show_alert=True)

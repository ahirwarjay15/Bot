"""Owner/moderator administration, database export, ban controls, and stats popup."""
import asyncio
import json
from datetime import datetime, timezone
from bson import json_util
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
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
    """Build the same stats content for /stats and Settings > Stats.

    Telegram delivery failures are broadcast outcomes, not a user statistic.
    """
    total, banned = await asyncio.gather(
        db.users.count_documents({}),
        db.users.count_documents({"banned": True}),
    )
    return (
        f"⛦ Total Users: {total}\n"
        f"⛦ Banned Users: {banned}"
    )


@router.message(Command("stats"))
async def stats(message: Message, settings: Settings, database):
    if not message.from_user or not await is_owner_or_moderator(database.db, message.from_user.id, settings):
        return
    # Telegram popup alerts are only available for callback queries, not commands.
    # Keep /stats content identical to Settings > Stats.
    await message.answer(await build_stats_text(message.bot, database.db))
    try:
        await message.delete()
    except Exception:
        pass


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


@router.message(Command("export"))
async def export_database(message: Message, settings: Settings, database):
    """Export every MongoDB collection as Extended JSON in one owner-only TXT file."""
    if not message.from_user or message.from_user.id != settings.owner_id:
        return

    # Telegram's cloud Bot API accepts uploads up to 50 MB; keep a safety margin.
    max_bytes = 48 * 1024 * 1024
    output = bytearray()
    document_count = 0
    collection_count = 0
    try:
        collection_names = sorted(await database.db.list_collection_names())
        output.extend(b'{\n  "database": ')
        output.extend(json.dumps(database.db.name, ensure_ascii=False).encode("utf-8"))
        output.extend(b',\n  "exported_at": ')
        output.extend(json.dumps(datetime.now(timezone.utc).isoformat()).encode("utf-8"))
        output.extend(b',\n  "collections": {')
        for collection_name in collection_names:
            collection = database.db[collection_name]
            if collection_count:
                output.extend(b',')
            output.extend(b'\n    ')
            output.extend(json.dumps(collection_name, ensure_ascii=False).encode("utf-8"))
            output.extend(b': [')
            first_document = True
            async for record in collection.find({}):
                encoded = json_util.dumps(
                    record, json_options=json_util.RELAXED_JSON_OPTIONS, ensure_ascii=False
                ).encode("utf-8")
                extra = (b'' if first_document else b',') + b'\n      ' + encoded.replace(b'\n', b'\n      ')
                if len(output) + len(extra) + 8 > max_bytes:
                    await message.answer(
                        "Database export is larger than Telegram's safe upload limit (48 MB). "
                        "No partial export was sent."
                    )
                    return
                output.extend(extra)
                first_document = False
                document_count += 1
            output.extend(b'\n    ]')
            collection_count += 1
        output.extend(b'\n  }\n}\n')
        if len(output) > max_bytes:
            await message.answer(
                "Database export is larger than Telegram's safe upload limit (48 MB). "
                "No partial export was sent."
            )
            return
        export_file = BufferedInputFile(bytes(output), filename="database_export.txt")
        await message.answer_document(
            export_file,
            caption=(
                "MongoDB database export completed.\\n"
                f"Collections: {collection_count} | Documents: {document_count}"
            ),
        )
    except Exception:
        # Do not expose database errors or credentials in chat.
        await message.answer("Database export failed. Please check the bot logs and try again.")

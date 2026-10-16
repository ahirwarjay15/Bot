# File-Bot

Async Telegram file/batch delivery bot built with Python 3.12, aiogram 3 and MongoDB Atlas via PyMongo Async.

## Implemented behavior
- /file creates one permanent deeplink for one copied source-channel message.
- /batch supports up to 100 messages, one deeplink per batch, persistent collection state, original order, 200-second inactivity timeout, and per-item delivery progress recovery after restart.
- Single-file deliveries use the same durable per-item recovery path.
- /start handles file and batch links, ban checks, and live Force Join membership checks on each deeplink. Pending Join Requests count as verified when recorded by the bot.
- Inline /settings pages cover Start Message/About, Force Join channels and text-animation modes, Auto Delete/time/message, After Delete, Invalid Post/About, Link Shortener, Moderators, and Stats.
- Auto Delete schedules only delivered file messages and their auto-delete notice. Batch files share a deadline starting at the first successful delivery.
- Expiry tasks, single/batch delivery item progress, and broadcast queues persist in MongoDB. Broadcast tracks per-recipient outcomes and resumes after restart.
- Owner-only moderator management (maximum three), moderator/owner ban protection, /ban, /unban, and /stats.
- Manual /shortener URL or reply-to-message shortening; file and batch links are not automatically shortened.
- Shortener setup includes a GPlinks adapter plus a generic JSON API adapter for compatible providers.

## Required Telegram setup
1. Create a private source channel for stored messages.
2. Add the bot as an administrator with permission to post/copy messages.
3. Set SOURCE_CHANNEL_ID to the channel's numeric ID.
4. Set FORCE_JOIN_CHANNELS as comma-separated channel usernames/IDs; the bot must be able to check membership.
5. Set BOT_TOKEN, OWNER_ID, and MONGODB_URI in a private server-side .env. Never commit real secrets.

## Install and run
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

Run tests with pytest -q. GitHub Actions compiles Python and runs the test suite on pushes and pull requests.

## Test status and limitations
GitHub Actions compile checks and the current automated unit/regression suite pass on the implementation branch. End-to-end tests against a real Telegram bot, MongoDB Atlas, and the Oracle VM are still pending. Do not treat it as production-ready until those checks pass.
- MongoDB stores references; the source channel contains actual posts. Deleted or inaccessible source posts cannot be reconstructed.
- Telegram cannot guarantee exactly-once delivery across a crash between a successful send and recording the result. An interrupted in-flight broadcast recipient is marked unknown rather than blindly resent.
- GPlinks uses its documented API endpoint; other shortener providers need to match the generic JSON url/token contract or have a dedicated adapter.
- Telegram does not expose a universal exact count of users who blocked the bot.
- Telegram Bot API cannot delete user-authored messages in ordinary private chats.
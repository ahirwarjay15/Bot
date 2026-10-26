# Telegram File Bot

Async Telegram file and batch-delivery bot built with Python 3.12, aiogram 3 and MongoDB Atlas via PyMongo Async.

## Features

- Single-file and batch deeplinks with durable delivery progress.
- Force Join membership checks, configurable start/about and invalid-post messages.
- Auto Delete, After Delete, shortener integration, moderator controls and statistics.
- Persistent expiry, delivery and broadcast queues with restart recovery.
- Background-worker supervision and readiness-aware HTTP health checks.
- MongoDB startup retry with bounded exponential backoff.
- Docker image support and CI checks on pushes and pull requests.

## Configure

Set these environment variables in your hosting provider or a private `.env` file:

- `BOT_TOKEN`: token for this bot instance.
- `OWNER_ID`: numeric Telegram owner ID.
- `MONGODB_URI`: MongoDB connection string.
- `MONGODB_DB_NAME`: required database name for this bot instance. Choose a dedicated name (for example, `my_file_bot`) to keep this bot's data isolated. The code has no implicit database-name default.
- `SOURCE_CHANNEL_ID`: private source channel ID (legacy `DB_CHANNEL_ID` is also accepted).
- `FORCE_JOIN_CHANNELS`: optional comma-separated usernames or channel IDs.
- `LOG_LEVEL`: optional logging level, defaults to `INFO`.
- `PORT`: optional HTTP health-check port, defaults to `10000`.

**Running multiple bots from this one repository:** deploy a separate process/service per bot and set a distinct `BOT_TOKEN` for each. Set `MONGODB_DB_NAME` per instance if the bots should keep data isolated. Do not share one database unless sharing users, settings, queues and stored references is intentional. Never commit real tokens or database credentials.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

Run the automated test suite with `pytest -q`.

## Docker

```bash
docker build -t titania .
docker run --env-file .env -p 10000:10000 titania
```

The `/` and `/health` endpoints return HTTP 503 until configuration is valid and MongoDB is connected; they return HTTP 200 after the bot is ready. MongoDB connection failures are retried with bounded exponential backoff (5 seconds up to 60 seconds).

## Operational notes

- The source channel stores the actual Telegram posts; MongoDB stores references. Deleted or inaccessible source posts cannot be reconstructed.
- Telegram cannot guarantee exactly-once delivery across a crash between Telegram accepting a send and MongoDB recording it.
- Real Telegram, MongoDB Atlas and production-host smoke tests must be run in the target environment; CI alone does not prove live readiness.

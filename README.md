# Loungefly Monitor

A lightweight, asynchronous foundation for continuously monitoring Loungefly Mini Backpack
availability. It provides lifecycle management, normalized models, SQLite persistence,
configuration, HTTP transport, logging, scheduling, and Discord webhook notifications.
**No retailer scraping is implemented.**

## Requirements

- Python 3.12+
- Linux (including Raspberry Pi 5 / ARM64)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m app.main
```

Press `Ctrl+C` or send `SIGTERM` to stop cleanly. Runtime defaults are in
`config/retailers.yaml`; secrets and optional path/level overrides belong in `.env`.

## Architecture

- `app/models.py`: normalized product, availability, and alert types
- `app/database.py`: SQLite schema and asynchronous connection lifecycle
- `app/http.py`: pooled HTTP client with bounded concurrency and finite retries
- `app/scheduler.py`: independent asynchronous interval jobs
- `app/monitors/base.py`: contract for future retailer adapters
- `app/notifications/base.py`: contract for notification destinations
- `app/notifications/discord.py`: Discord embeds, webhook routing, and persistent deduplication
- `app/services/`: persistence operations for products, stock, and alert audits
- `app/application.py`: startup and graceful shutdown orchestration

The default database is `data/loungefly.db`, and rotating logs are written to
`logs/loungefly-monitor.log`. Both runtime artifacts are ignored by Git.

## Development checks

```bash
pytest
python -m compileall -q app tests
```

## Discord notifications

Set product and administrator webhook URLs in `.env` (never in committed YAML):

```dotenv
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_ADMIN_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Product alerts use the first URL; monitor error and recovery alerts use the administrator URL.
Successful deliveries are persistently deduplicated in SQLite by alert occurrence. Reuse an
alert's `occurrence_id` when retrying or restoring that event; create a new `Alert` for a later
episode, even when its product state and price match an earlier episode. Failed deliveries remain
eligible for retry and return `False` rather than crashing the monitor.

To safely generate a standalone sample event (with no retailer adapter involved), configure
`DISCORD_WEBHOOK_URL` and run:

```bash
python -m app.tools.test_notification
```

The command prints an error and sends nothing when the applicable webhook is not configured.
Use `--admin` to test routing to `DISCORD_ADMIN_WEBHOOK_URL`.

Retailer discovery/check implementations, alert transition rules, SMS delivery, and historical
product states are intentionally reserved for later stages.

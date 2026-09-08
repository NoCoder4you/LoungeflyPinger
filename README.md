# Loungefly Monitor

A lightweight, asynchronous foundation for continuously monitoring Loungefly Mini Backpack
availability. It provides lifecycle management, normalized models, SQLite persistence,
configuration, HTTP transport, logging, scheduling, and Discord webhook notifications.
GeekCore UK is monitored through Shopify's public structured collection feeds, and
TruffleShuffle UK and Loungefly UK are monitored through public product JSON-LD.

## Retailer status

| Retailer | Status | Data source |
| --- | --- | --- |
| GeekCore | Working | Public Shopify product feeds |
| TruffleShuffle | Working | Public category and product JSON-LD |
| HMV | Requires browser | Normal HTTP requests receive a Cloudflare managed challenge |
| Loungefly UK | Working | Public schema.org ItemList and Product JSON-LD |

HMV is present in the retailer configuration with its own interval but is disabled. Its home,
search, and sitemap responses do not expose product IDs, prices, availability, preorder state, or
exclusive markers to reasonable HTTP requests. The monitor deliberately does not solve or bypass
that challenge, and no selectors or stock rules are guessed from it. Consequently there is no HMV
adapter until HMV makes a stable public product representation available to normal HTTP clients.

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
- `app/monitors/base.py`: contract for retailer adapters
- `app/monitors/geekcore.py`: GeekCore UK discovery and stock normalization
- `app/monitors/truffleshuffle.py`: TruffleShuffle UK JSON-LD discovery and normalization
- `app/monitors/loungefly_uk.py`: official Loungefly UK JSON-LD discovery and normalization
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
Deliveries are persisted for deduplication only after Discord accepts them. Reuse an alert's
`occurrence_id` when retrying or restoring that event; create a new `Alert` for a later episode,
even when its product state and price match an earlier episode. Failed or cancelled requests remain
eligible for retry, and delivery failures return `False` rather than crashing the monitor.

To safely generate a standalone sample event (with no retailer adapter involved), configure
`DISCORD_WEBHOOK_URL` and run:

```bash
python -m app.tools.test_notification
```

The command prints an error and sends nothing when the applicable webhook is not configured.
Use `--admin` to test routing to `DISCORD_ADMIN_WEBHOOK_URL`.

The first successful retailer run is a silent baseline synchronization: products and stock are
persisted without flooding Discord. Later discoveries emit `NEW_PRODUCT`, and an
`OUT_OF_STOCK` to `IN_STOCK` transition emits `RESTOCK`. SMS delivery and historical product
states are reserved for later stages.

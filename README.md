# Loungefly Monitor

A lightweight, asynchronous foundation for continuously monitoring Loungefly Mini Backpack
availability. It provides lifecycle management, normalized models, SQLite persistence,
configuration, HTTP transport, optional browser transport, logging, scheduling, and Discord webhook notifications.
GeekCore UK is monitored through Shopify's public structured collection feeds, and
TruffleShuffle UK and Loungefly UK are monitored through public product JSON-LD.

## Retailer status

| Retailer | Status | Data source |
| --- | --- | --- |
| GeekCore | Working | Public Shopify product feeds |
| TruffleShuffle | Working | Public category and product JSON-LD |
| HMV | Requires browser challenge (disabled) | Ordinary Playwright receives a Cloudflare managed challenge |
| Loungefly UK | Working | Public schema.org ItemList and Product JSON-LD |

HMV has an isolated Playwright adapter and a conservative 15-minute interval, but remains disabled.
A controlled test on 2026-09-08 found that both search and Loungefly category pages returned HTTP
403 Cloudflare verification challenges in ordinary headless Chromium. Product discovery and
individual stock monitoring therefore could not be verified reliably. Challenge, denial,
navigation, and parser failures are explicitly distinguished and never treated as out of stock.
The implementation deliberately uses no stealth, CAPTCHA, fingerprint, proxy, or challenge-bypass
techniques.

## Requirements

- Python 3.12+
- Linux (including Raspberry Pi 5 / ARM64)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
python -m app.main
```

Press `Ctrl+C` or send `SIGTERM` to stop cleanly. Runtime defaults are in
`config/retailers.yaml`; secrets and optional path/level overrides belong in `.env`.

## Architecture

- `app/models.py`: normalized product, availability, and alert types
- `app/database.py`: SQLite schema and asynchronous connection lifecycle
- `app/http.py`: pooled HTTP client with bounded concurrency and finite retries
- `app/browser.py`: optional shared Chromium lifecycle with isolated contexts and bounded pages
- `app/scheduler.py`: independent asynchronous interval jobs
- `app/monitors/base.py`: contract for retailer adapters
- `app/monitors/geekcore.py`: GeekCore UK discovery and stock normalization
- `app/monitors/truffleshuffle.py`: TruffleShuffle UK JSON-LD discovery and normalization
- `app/monitors/loungefly_uk.py`: official Loungefly UK JSON-LD discovery and normalization
- `app/monitors/hmv.py`: disabled-by-default browser JSON-LD adapter with challenge safeguards
- `app/notifications/base.py`: contract for notification destinations
- `app/notifications/discord.py`: Discord embeds, webhook routing, and persistent deduplication
- `app/services/`: persistence operations for products, stock, and alert audits
- `app/application.py`: startup and graceful shutdown orchestration

The default database is `data/loungefly.db`, and rotating logs are written to
`logs/loungefly-monitor.log`. Both runtime artifacts are ignored by Git.

Playwright is imported and Chromium is launched only when an enabled retailer selects browser
transport. HTTP retailers do not depend on a running browser and continue if Chromium startup
fails. Browser concurrency defaults to one page; each page uses a fresh context which is closed
after navigation, while one browser process is reused and shut down with the application. On a
Raspberry Pi, install the Chromium system dependencies documented by Playwright and retain the
15-minute HMV interval to limit CPU and memory pressure.

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

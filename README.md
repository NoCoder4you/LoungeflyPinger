# Loungefly Monitor

A lightweight, asynchronous foundation for continuously monitoring Loungefly Mini Backpack
availability. It provides lifecycle management, normalized models, SQLite persistence,
configuration, HTTP transport, logging, scheduling, and Discord webhook notifications.
GeekCore UK is monitored through Shopify's public structured collection feeds, and
TruffleShuffle UK and the Loungefly UK/US/Canada storefronts are monitored through public product JSON-LD. Disney Store UK
uses its public Loungefly ItemList and structured storefront product telemetry.

## Retailer status

| Retailer | Status | Data source |
| --- | --- | --- |
| GeekCore | Working | Public Shopify product feeds |
| TruffleShuffle | Working | Public category and product JSON-LD |
| Loungefly UK | Working | Public schema.org ItemList and Product JSON-LD |
| Loungefly US | Working | Public schema.org ItemList/Product JSON-LD and product flags |
| Loungefly Canada | Working | Public schema.org ItemList/Product JSON-LD and product flags |
| Disney Store UK | Working | Public ItemList and product telemetry |
| Disney Store US | Working | Public schema.org ListItem microdata and product telemetry |
| BoxLunch US | Working | Public schema.org CollectionPage and Product JSON-LD |

Release metadata is parsed conservatively from retailer-published structured descriptions or
dedicated telemetry fields. Current capability is:

| Retailer | Release metadata |
| --- | --- |
| GeekCore | Partial (explicit Shopify tags/descriptions) |
| TruffleShuffle | Partial (explicit Product JSON-LD descriptions) |
| Loungefly UK | Partial (explicit Product JSON-LD descriptions) |
| Loungefly US | Partial (explicit Product JSON-LD descriptions; no date is inferred from publication or shipping) |
| Loungefly Canada | Partial (explicit Product JSON-LD descriptions; times require an explicit timezone) |
| Disney Store UK | Partial (dedicated product telemetry messages when published) |
| Disney Store US | Partial (dedicated product telemetry messages when explicitly published) |
| BoxLunch US | Partial (explicit Product JSON-LD descriptions only) |

Missing or malformed release text never clears a previously known release. Exact times use the
configured retailer-local IANA timezone (`Europe/London` for UK and `America/Los_Angeles` for
Loungefly US), record when that zone was inferred, and apply the offset in effect on the release
date.
Canada spans multiple timezones, so the Canada adapter accepts an exact release time only when
the retailer supplies an explicit timezone; it never guesses one from the storefront market.

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
Product alert selection is configured separately in `config/watchlist.yaml`.

## Watchlist matching

Each entry under `products` is an independent watch. Properties within one watch are combined
with AND, while a product may match several watches. Matching is case- and punctuation-insensitive
for names, keywords, retailer names, franchises, characters, IDs, SKUs, and product types.
Supported constraints are `exact_url` (or `url`), `retailer_product_id`, `sku`, `product_name`,
`keywords` (or `required_keywords`), `excluded_keywords`, `franchise`, `character`, `retailers`,
`product_types`, `exclusivity` (or `exclusive`), `preorder`, and `max_price`.

```yaml
products:
  - name: Stitch Backpacks
    priority: high
    keywords: [stitch]
    franchise: Disney
    character: Stitch
    product_types: [mini_backpack]
    retailers: [geekcore, truffleshuffle, disney store UK]
    excluded_keywords: [wallet]
    max_price: 90
```

Priorities are `low`, `normal`, or `high`. Alerts expose their highest matched priority as
notification metadata for future routing (such as SMS), and Discord embeds show all matched watch
names plus that priority. An empty `products` list disables product alerts without disabling
collection and persistence.

## Architecture

- `app/models.py`: normalized product, availability, and alert types
- `app/database.py`: SQLite schema and asynchronous connection lifecycle
- `app/http.py`: pooled HTTP client with bounded concurrency and finite retries
- `app/scheduler.py`: independent asynchronous interval jobs
- `app/monitors/base.py`: contract for retailer adapters
- `app/monitors/geekcore.py`: GeekCore UK discovery and stock normalization
- `app/monitors/truffleshuffle.py`: TruffleShuffle UK JSON-LD discovery and normalization
- `app/monitors/loungefly_uk.py`: shared regional Loungefly UK/US/Canada JSON-LD discovery and normalization
- `app/monitors/disney_store_uk.py`: shared Disney Store UK/US structured storefront adapter
- `app/monitors/boxlunch.py`: BoxLunch US structured collection adapter
- `app/notifications/base.py`: contract for notification destinations
- `app/notifications/discord.py`: Discord embeds, webhook routing, and persistent deduplication
- `app/services/`: persistence operations for products, stock, and alert audits
- `app/watchlist.py`: watchlist validation, deterministic classification, and product matching
- `app/application.py`: startup and graceful shutdown orchestration

The default database is `data/loungefly.db`, and rotating logs are written to
`logs/loungefly-monitor.log`. Both runtime artifacts are ignored by Git.

## Resilience and retailer health

Each retailer has an independently timed, bounded scheduler task. HTTP requests use a pooled
session, a global concurrency cap, configurable request rate, timeout, retry limit, exponential
backoff, and `Retry-After` handling for throttling. Tune `failure_alert_threshold`,
`retry_backoff_seconds`, `rate_limit_requests_per_second`, and
`retailer_job_timeout_seconds` in `config/retailers.yaml`.

SQLite retains `last_success`, `last_failure`, `consecutive_failures`, `last_error`, the most
recent HTTP response status, request duration, and a `HEALTHY`, `DEGRADED`, `FAILED`, or
`DISABLED` state. A failure episode issues one administrator alert after the configured threshold
and one recovery alert after the next successful scan; these episode flags survive application
restarts. Failed parses and `ERROR` observations are not inventory evidence: successful product
history is preserved, and a failed scan is rolled back rather than partially updating products.

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
even when its product state and price match an earlier episode. Failed product notification
requests remain eligible for retry, and delivery failures return `False` rather than crashing the
monitor. A retailer-health failure alert is issued only once per failure episode even if Discord
is unavailable, preventing an unattended alert storm.

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

Release reminders and upgrade synchronization are configured in `config/retailers.yaml`.
`DATE_ONLY`, `MONTH_ONLY`, and `COMING_SOON` releases never receive an invented midnight
countdown. Existing installations silently enrich known products on the first release-aware scan;
set `notify_existing_on_upgrade: true` only when those discovery notifications are desired.

# Loungefly Monitor

A lightweight, asynchronous foundation for continuously monitoring Loungefly Mini Backpack
availability. This stage provides lifecycle management, normalized models, SQLite persistence,
configuration, HTTP transport, logging, and scheduling. **No retailer scraping is implemented.**

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
- `app/services/`: persistence operations for products, stock, and alert audits
- `app/application.py`: startup and graceful shutdown orchestration

The default database is `data/loungefly.db`, and rotating logs are written to
`logs/loungefly-monitor.log`. Both runtime artifacts are ignored by Git.

## Development checks

```bash
pytest
python -m compileall -q app tests
```

Retailer discovery/check implementations, notification providers, alert transition rules,
and historical product states are intentionally reserved for later stages.

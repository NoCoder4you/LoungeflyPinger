"""Async SQLite connection lifecycle and schema management."""

from pathlib import Path

import aiosqlite

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS retailers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    last_success TEXT,
    last_failure TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0)
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    retailer TEXT NOT NULL,
    retailer_product_id TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    image_url TEXT,
    sku TEXT,
    franchise TEXT,
    character TEXT,
    product_type TEXT NOT NULL,
    exclusive INTEGER NOT NULL DEFAULT 0 CHECK (exclusive IN (0, 1)),
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    missing_scans INTEGER NOT NULL DEFAULT 0 CHECK (missing_scans >= 0),
    removed_at TEXT,
    UNIQUE (retailer, retailer_product_id),
    FOREIGN KEY (retailer) REFERENCES retailers(name) ON UPDATE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_products_retailer ON products(retailer);
CREATE INDEX IF NOT EXISTS idx_products_last_seen ON products(last_seen);
CREATE TABLE IF NOT EXISTS product_states (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL UNIQUE,
    availability TEXT NOT NULL,
    price TEXT,
    currency TEXT NOT NULL,
    preorder INTEGER NOT NULL DEFAULT 0 CHECK (preorder IN (0, 1)),
    checked_at TEXT NOT NULL,
    previous_price TEXT,
    lowest_price TEXT,
    highest_price TEXT,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_product_states_checked_at ON product_states(checked_at);
CREATE TABLE IF NOT EXISTS product_state_history (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL,
    availability TEXT NOT NULL,
    price TEXT,
    currency TEXT NOT NULL,
    preorder INTEGER NOT NULL CHECK (preorder IN (0, 1)),
    checked_at TEXT NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_product_state_history_product_checked
    ON product_state_history(product_id, checked_at);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL,
    alert_type TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    destination TEXT NOT NULL,
    success INTEGER NOT NULL CHECK (success IN (0, 1)),
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_alerts_product_sent ON alerts(product_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_alerts_type_sent ON alerts(alert_type, sent_at);
CREATE TABLE IF NOT EXISTS notification_deliveries (
    deduplication_key TEXT PRIMARY KEY,
    alert_type TEXT NOT NULL,
    destination TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notification_deliveries_sent_at
    ON notification_deliveries(sent_at);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self.connection is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        await self.connection.execute("PRAGMA foreign_keys = ON")
        await self.connection.execute("PRAGMA journal_mode = WAL")
        await self.connection.execute("PRAGMA busy_timeout = 5000")

    async def initialize(self) -> None:
        if self.connection is None:
            await self.connect()
        assert self.connection is not None
        await self.connection.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS does not evolve databases created by older releases.
        await self._add_missing_columns("products", {
            "missing_scans": "INTEGER NOT NULL DEFAULT 0", "removed_at": "TEXT", "sku": "TEXT"
        })
        await self._add_missing_columns("product_states", {
            "previous_price": "TEXT", "lowest_price": "TEXT", "highest_price": "TEXT"
        })
        await self.connection.commit()

    async def _add_missing_columns(self, table: str, columns: dict[str, str]) -> None:
        assert self.connection is not None
        existing = {row[1] for row in await (await self.connection.execute(
            f"PRAGMA table_info({table})"
        )).fetchall()}
        for name, definition in columns.items():
            if name not in existing:
                await self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None

    async def __aenter__(self) -> "Database":
        await self.connect()
        await self.initialize()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

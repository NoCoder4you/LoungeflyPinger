"""Async SQLite connection lifecycle and schema management."""

import hashlib
import json
import asyncio

from pathlib import Path

import aiosqlite


def release_history_key(values: tuple[object, ...]) -> str:
    """Return a stable, NULL-safe identity for one normalized release observation."""
    encoded = json.dumps(values, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS retailers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    last_success TEXT,
    last_failure TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0)
    ,release_sync_completed INTEGER NOT NULL DEFAULT 0 CHECK (release_sync_completed IN (0, 1)),
    last_error TEXT,
    response_status INTEGER,
    request_duration REAL,
    health TEXT NOT NULL DEFAULT 'HEALTHY',
    failure_alert_sent INTEGER NOT NULL DEFAULT 0 CHECK (failure_alert_sent IN (0, 1))
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    retailer TEXT NOT NULL,
    retailer_product_id TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    image_url TEXT,
    sku TEXT,
    variant_id TEXT,
    barcode TEXT,
    vendor TEXT,
    tags TEXT,
    listing_published_at TEXT,
    compare_at_price TEXT,
    collections TEXT,
    sale INTEGER NOT NULL DEFAULT 0 CHECK (sale IN (0, 1)),
    clearance INTEGER NOT NULL DEFAULT 0 CHECK (clearance IN (0, 1)),
    last_chance INTEGER NOT NULL DEFAULT 0 CHECK (last_chance IN (0, 1)),
    limited_edition INTEGER NOT NULL DEFAULT 0 CHECK (limited_edition IN (0, 1)),
    limited_release INTEGER NOT NULL DEFAULT 0 CHECK (limited_release IN (0, 1)),
    collection_type TEXT,
    vaulted INTEGER NOT NULL DEFAULT 0 CHECK (vaulted IN (0, 1)),
    exclusivity_text TEXT,
    license TEXT,
    property TEXT,
    characters TEXT,
    edition TEXT,
    style TEXT,
    incoming_status TEXT,
    event_exclusive INTEGER NOT NULL DEFAULT 0 CHECK (event_exclusive IN (0, 1)),
    event_name TEXT,
    event_year INTEGER,
    discovery_sources TEXT,
    franchise TEXT,
    character TEXT,
    product_type TEXT NOT NULL,
    exclusive INTEGER NOT NULL DEFAULT 0 CHECK (exclusive IN (0, 1)),
    exclusive_retailer TEXT,
    exclusive_region TEXT,
    new_release INTEGER NOT NULL DEFAULT 0 CHECK (new_release IN (0, 1)),
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
    release_date TEXT,
    release_time TEXT,
    release_timezone TEXT,
    release_datetime TEXT,
    release_precision TEXT,
    release_text TEXT,
    release_source TEXT,
    release_timezone_inferred INTEGER NOT NULL DEFAULT 0 CHECK (release_timezone_inferred IN (0, 1)),
    release_month INTEGER,
    release_year INTEGER,
    estimated_ship_date TEXT,
    estimated_arrival_date TEXT,
    estimated_arrival_text TEXT,
    estimated_dispatch_date TEXT,
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
CREATE TABLE IF NOT EXISTS release_history (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL,
    release_date TEXT,
    release_time TEXT,
    release_timezone TEXT,
    release_datetime TEXT,
    release_precision TEXT NOT NULL,
    release_text TEXT,
    release_source TEXT,
    release_timezone_inferred INTEGER NOT NULL DEFAULT 0 CHECK (release_timezone_inferred IN (0, 1)),
    release_month INTEGER,
    release_year INTEGER,
    release_key TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    UNIQUE(product_id, release_key),
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_release_history_product_detected
    ON release_history(product_id, detected_at);
CREATE TABLE IF NOT EXISTS release_reminders (
    product_id INTEGER NOT NULL,
    release_datetime TEXT NOT NULL,
    reminder_seconds INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY(product_id, release_datetime, reminder_seconds),
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
);
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
        # A single SQLite connection cannot safely host overlapping transactions.
        # Network discovery remains concurrent; only each atomic persistence phase is serialized.
        self.write_lock = asyncio.Lock()

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
            "missing_scans": "INTEGER NOT NULL DEFAULT 0", "removed_at": "TEXT", "sku": "TEXT",
            "variant_id": "TEXT", "barcode": "TEXT", "vendor": "TEXT", "tags": "TEXT",
            "listing_published_at": "TEXT",
            "new_release": "INTEGER NOT NULL DEFAULT 0",
            "exclusive_retailer": "TEXT",
            "exclusive_region": "TEXT",
            "compare_at_price": "TEXT", "collections": "TEXT",
            "license": "TEXT", "property": "TEXT", "characters": "TEXT",
            "edition": "TEXT", "style": "TEXT", "incoming_status": "TEXT",
            "event_exclusive": "INTEGER NOT NULL DEFAULT 0", "event_name": "TEXT",
            "event_year": "INTEGER", "discovery_sources": "TEXT",
            "sale": "INTEGER NOT NULL DEFAULT 0", "clearance": "INTEGER NOT NULL DEFAULT 0",
            "last_chance": "INTEGER NOT NULL DEFAULT 0",
            "limited_edition": "INTEGER NOT NULL DEFAULT 0",
            "limited_release": "INTEGER NOT NULL DEFAULT 0",
            "collection_type": "TEXT", "vaulted": "INTEGER NOT NULL DEFAULT 0",
            "exclusivity_text": "TEXT",
        })
        await self._add_missing_columns("retailers", {
            "release_sync_completed": "INTEGER NOT NULL DEFAULT 0",
            "last_error": "TEXT", "response_status": "INTEGER",
            "request_duration": "REAL", "health": "TEXT NOT NULL DEFAULT 'HEALTHY'",
            "failure_alert_sent": "INTEGER NOT NULL DEFAULT 0",
        })
        await self._add_missing_columns("product_states", {
            "previous_price": "TEXT", "lowest_price": "TEXT", "highest_price": "TEXT",
            "release_date": "TEXT", "release_time": "TEXT", "release_timezone": "TEXT",
            "release_datetime": "TEXT", "release_precision": "TEXT", "release_text": "TEXT",
            "release_source": "TEXT", "release_timezone_inferred": "INTEGER NOT NULL DEFAULT 0",
            "release_month": "INTEGER", "release_year": "INTEGER",
            "estimated_ship_date": "TEXT",
            "estimated_arrival_date": "TEXT", "estimated_arrival_text": "TEXT",
            "estimated_dispatch_date": "TEXT",
        })
        await self._add_missing_columns("release_history", {"release_key": "TEXT"})
        await self._migrate_release_history_keys()
        await self.connection.commit()

    async def _migrate_release_history_keys(self) -> None:
        """Backfill keys and collapse duplicates created by SQLite NULL uniqueness semantics."""
        assert self.connection is not None
        rows = await (await self.connection.execute(
            """SELECT id, release_date, release_time, release_timezone, release_datetime,
                      release_precision, release_text, release_source, release_timezone_inferred,
                      release_month, release_year
                 FROM release_history"""
        )).fetchall()
        for row in rows:
            await self.connection.execute(
                "UPDATE release_history SET release_key=? WHERE id=?",
                (release_history_key(tuple(row[1:])), row[0]),
            )
        await self.connection.execute(
            """DELETE FROM release_history
                 WHERE id NOT IN (
                       SELECT MIN(id) FROM release_history GROUP BY product_id, release_key
                 )"""
        )
        await self.connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_release_history_product_key
                   ON release_history(product_id, release_key)"""
        )

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

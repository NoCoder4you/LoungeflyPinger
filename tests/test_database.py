from pathlib import Path

import pytest

from app.database import Database


@pytest.mark.asyncio
async def test_database_initialization(tmp_path: Path) -> None:
    database = Database(tmp_path / "monitor.db")
    await database.connect()
    await database.initialize()
    assert database.connection is not None
    cursor = await database.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in await cursor.fetchall()}
    assert {"retailers", "products", "product_states", "product_state_history", "alerts",
            "notification_deliveries", "release_history", "release_reminders"} <= tables
    product_columns = {
        row[1] for row in await (await database.connection.execute(
            "PRAGMA table_info(products)"
        )).fetchall()
    }
    assert "sku" in product_columns
    foreign_keys = await (await database.connection.execute("PRAGMA foreign_keys")).fetchone()
    assert foreign_keys == (1,)
    await database.close()
    assert database.connection is None


@pytest.mark.asyncio
async def test_database_migrates_sku_onto_existing_products_table(tmp_path: Path) -> None:
    database = Database(tmp_path / "legacy.db")
    await database.connect()
    assert database.connection is not None
    await database.connection.execute(
        """CREATE TABLE products (
               id INTEGER PRIMARY KEY, retailer TEXT NOT NULL, retailer_product_id TEXT NOT NULL,
               name TEXT NOT NULL, url TEXT NOT NULL, image_url TEXT, franchise TEXT,
               character TEXT, product_type TEXT NOT NULL, exclusive INTEGER NOT NULL DEFAULT 0,
               first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
               UNIQUE (retailer, retailer_product_id)
           )"""
    )
    await database.connection.commit()

    await database.initialize()

    columns = {
        row[1] for row in await (await database.connection.execute(
            "PRAGMA table_info(products)"
        )).fetchall()
    }
    assert {"sku", "missing_scans", "removed_at"} <= columns
    await database.close()

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
    assert {"retailers", "products", "product_states", "alerts"} <= tables
    foreign_keys = await (await database.connection.execute("PRAGMA foreign_keys")).fetchone()
    assert foreign_keys == (1,)
    await database.close()
    assert database.connection is None

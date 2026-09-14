import sqlite3
from pathlib import Path

import pytest

from app.database import Database


@pytest.mark.asyncio
async def test_online_backups_are_valid_and_retention_is_bounded(tmp_path: Path) -> None:
    database_path = tmp_path / "state.db"
    backup_directory = tmp_path / "backups"
    async with Database(database_path) as database:
        assert database.connection is not None
        await database.connection.execute(
            "INSERT INTO retailers(name, enabled) VALUES ('Production Test', 1)"
        )
        await database.connection.commit()
        for _ in range(3):
            await database.backup(backup_directory, keep=2)

    backups = sorted(backup_directory.glob("state-*.db"))
    assert len(backups) == 2
    with sqlite3.connect(backups[-1]) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert restored.execute(
            "SELECT name FROM retailers WHERE name = 'Production Test'"
        ).fetchone() == ("Production Test",)


@pytest.mark.asyncio
async def test_restart_preserves_baseline_state(tmp_path: Path) -> None:
    path = tmp_path / "restart.db"
    async with Database(path) as database:
        assert database.connection is not None
        await database.connection.execute(
            "INSERT INTO retailers(name, enabled, health) VALUES ('Restart Test', 1, 'HEALTHY')"
        )
        await database.connection.commit()

    async with Database(path) as restarted:
        assert restarted.connection is not None
        row = await (await restarted.connection.execute(
            "SELECT enabled, health FROM retailers WHERE name = 'Restart Test'"
        )).fetchone()
        assert row == (1, "HEALTHY")

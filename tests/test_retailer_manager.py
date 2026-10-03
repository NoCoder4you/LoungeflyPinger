import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.config import AppConfig, LoggingConfig, MonitorConfig, SUPPORTED_RETAILERS
from app.database import Database
from app.scheduler import Scheduler
from app.services.retailer_manager import (
    RetailerDefinition, RetailerManager, ScanAlreadyRunning,
    build_retailer_registry,
)


class FakeMonitor:
    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.gate = gate
        self.calls = 0

    async def discover_products(self):
        self.calls += 1
        if self.gate:
            await self.gate.wait()
        return []


class FakeWatchlists:
    @property
    def current(self):
        return None


@pytest_asyncio.fixture
async def manager_parts(tmp_path: Path):
    database = Database(tmp_path / "retailers.db")
    await database.connect(); await database.initialize()
    scheduler = Scheduler()
    monitor = FakeMonitor()
    registry = {"shop": RetailerDefinition("shop", "Shop", lambda _h, _s: monitor)}
    config = AppConfig(monitor=MonitorConfig(), database_path=tmp_path / "retailers.db",
                       logging=LoggingConfig(path=tmp_path / "log"),
                       retailers={"shop": {"enabled": False, "interval_minutes": 5}})
    manager = RetailerManager(config, database, scheduler, object(), AsyncMock(), FakeWatchlists(), registry)  # type: ignore[arg-type]
    await manager.initialize()
    yield manager, database, scheduler, monitor, config, registry
    await scheduler.stop(); await database.close()


def test_registry_is_closed_and_covers_every_supported_retailer():
    registry = build_retailer_registry()
    assert set(registry) == set(SUPPORTED_RETAILERS)
    assert all(item.key == key and item.display_name for key, item in registry.items())


@pytest.mark.asyncio
async def test_overrides_persist_reschedule_and_reset(manager_parts):
    manager, database, scheduler, _, config, registry = manager_parts
    assert not manager.effective("shop").enabled
    await manager.set_enabled("shop", True, "42")
    assert scheduler.has_job("shop")
    await manager.set_enabled("shop", True, "42")
    assert scheduler.has_job("shop")
    await manager.set_interval("shop", 8, "42")
    assert manager.effective("shop").interval_minutes == 8

    reconstructed = RetailerManager(config, database, scheduler, object(), AsyncMock(), FakeWatchlists(), registry)  # type: ignore[arg-type]
    await reconstructed.initialize()
    assert reconstructed.effective("shop").enabled
    assert reconstructed.effective("shop").interval_minutes == 8
    await reconstructed.set_enabled("shop", False, "42")
    assert not scheduler.has_job("shop")
    await reconstructed.reset("shop")
    assert reconstructed.effective("shop").enabled is False
    assert reconstructed.effective("shop").interval_minutes == 5


@pytest.mark.asyncio
async def test_interval_bounds(manager_parts):
    manager, *_ = manager_parts
    with pytest.raises(ValueError): await manager.set_interval("shop", .5, "42")
    with pytest.raises(ValueError): await manager.set_interval("shop", 1441, "42")


@pytest.mark.asyncio
async def test_manual_disabled_scan_and_overlap_rejection(manager_parts):
    manager, _, _, monitor, *_ = manager_parts
    gate = asyncio.Event(); monitor.gate = gate
    first = asyncio.create_task(manager.scan("shop", "42"))
    await asyncio.sleep(0)
    with pytest.raises(ScanAlreadyRunning): await manager.scan("shop", "42")
    gate.set(); result = await first
    assert result["success"] and result["enabled"] is False and monitor.calls == 1
    assert not manager.scheduler.has_job("shop")


@pytest.mark.asyncio
async def test_disable_during_scan_preserves_disabled_health(manager_parts):
    manager, database, _, monitor, *_ = manager_parts
    gate = asyncio.Event(); monitor.gate = gate
    await manager.set_enabled("shop", True, "42")
    for _ in range(20):
        if (await manager.get_state("shop")).running:
            break
        await asyncio.sleep(0)
    assert (await manager.get_state("shop")).running

    disabled = await manager.set_enabled("shop", False, "42")
    assert disabled.health == "DISABLED"
    gate.set()
    for _ in range(20):
        if not (await manager.get_state("shop")).running:
            break
        await asyncio.sleep(0)
    assert not (await manager.get_state("shop")).running
    row = await (await database.connection.execute(
        "SELECT enabled, health FROM retailers WHERE name='Shop'"
    )).fetchone()  # type: ignore[union-attr]
    assert tuple(row) == (0, "DISABLED")
    assert (await manager.get_state("shop")).health == "DISABLED"


@pytest.mark.asyncio
async def test_scan_history_is_bounded_per_retailer_on_startup(manager_parts):
    manager, database, scheduler, _, config, registry = manager_parts
    assert database.connection is not None
    await database.connection.executemany(
        """INSERT INTO retailer_scan_history
           (retailer_key,started_at,completed_at,trigger_source,success,duration)
           VALUES('shop', ?, ?, 'scheduled', ?, 0.1)""",
        [(f"start-{number}", f"end-{number}", number % 2) for number in range(7)],
    )
    await database.connection.commit()

    reconstructed = RetailerManager(
        config, database, scheduler, object(), AsyncMock(), FakeWatchlists(), registry,
        scan_history_limit=3,
    )  # type: ignore[arg-type]
    await reconstructed.initialize()

    rows = await (await database.connection.execute(
        "SELECT completed_at FROM retailer_scan_history WHERE retailer_key='shop' ORDER BY id"
    )).fetchall()
    assert [row[0] for row in rows] == ["end-4", "end-5", "end-6"]


@pytest.mark.asyncio
async def test_scan_history_is_pruned_after_each_scan(manager_parts):
    manager, database, *_ = manager_parts
    manager.scan_history_limit = 2
    await manager.scan("shop", "42")
    await manager.scan("shop", "42")
    await manager.scan("shop", "42")

    count = await (await database.connection.execute(
        "SELECT COUNT(*) FROM retailer_scan_history WHERE retailer_key='shop'"
    )).fetchone()
    assert count[0] == 2

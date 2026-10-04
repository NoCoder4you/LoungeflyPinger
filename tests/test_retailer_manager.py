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
@pytest.mark.parametrize("health", ["FAILED", "DEGRADED"])
async def test_noop_enable_preserves_unhealthy_state(manager_parts, health):
    manager, database, scheduler, *_ = manager_parts
    await manager.set_enabled("shop", True, "42")
    await database.connection.execute("UPDATE retailers SET health=? WHERE name='Shop'", (health,))
    await database.connection.commit()
    job = scheduler._jobs["shop"]
    result = await manager.set_enabled("shop", True, "42")
    assert result.health == health
    assert scheduler._jobs["shop"] is job


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


@pytest.mark.asyncio
async def test_scan_without_full_deadline_waits_for_completion(manager_parts):
    manager, _, _, monitor, *_ = manager_parts
    monitor.gate = asyncio.Event()
    scan = asyncio.create_task(manager.scan("shop", "42"))

    # The default configuration has no whole-scan deadline, so an incomplete
    # adapter remains active until it can finish.
    await asyncio.sleep(0.02)
    assert not scan.done()

    monitor.gate.set()
    result = await asyncio.wait_for(scan, 0.5)
    assert result["success"] is True


@pytest.mark.asyncio
async def test_manual_scan_timeout_records_failure_and_releases_lock(manager_parts):
    _, database, scheduler, monitor, config, registry = manager_parts
    monitor.gate = asyncio.Event()
    timeout_config = AppConfig(
        monitor=MonitorConfig(retailer_job_timeout_seconds=0.01),
        database_path=config.database_path,
        logging=config.logging,
        retailers=config.retailers,
    )
    manager = RetailerManager(
        timeout_config, database, scheduler, object(), AsyncMock(), FakeWatchlists(), registry,
    )  # type: ignore[arg-type]
    await manager.initialize()

    result = await asyncio.wait_for(manager.scan("shop", "42"), 0.5)

    assert result["success"] is False
    assert result["health"] == "DISABLED"
    assert not (await manager.get_state("shop")).running
    health = await (await database.connection.execute(
        "SELECT consecutive_failures, last_error FROM retailers WHERE name='Shop'"
    )).fetchone()
    assert health[0] == 1
    assert health[1] == "scan exceeded configured timeout of 0.01 seconds"
    history = await (await database.connection.execute(
        """SELECT trigger_source, success, error_summary
           FROM retailer_scan_history WHERE retailer_key='shop' ORDER BY id DESC LIMIT 1"""
    )).fetchone()
    assert tuple(history) == (
        "manual", 0, "scan exceeded configured timeout of 0.01 seconds",
    )


@pytest.mark.asyncio
async def test_scheduled_scan_timeout_is_recorded_before_job_returns(manager_parts):
    _, database, _, monitor, config, registry = manager_parts
    monitor.gate = asyncio.Event()
    scheduler = Scheduler()
    timeout_config = AppConfig(
        monitor=MonitorConfig(retailer_job_timeout_seconds=0.01),
        database_path=config.database_path,
        logging=config.logging,
        retailers={"shop": {"enabled": True, "interval_minutes": 5}},
    )
    manager = RetailerManager(
        timeout_config, database, scheduler, object(), AsyncMock(), FakeWatchlists(), registry,
    )  # type: ignore[arg-type]
    try:
        await manager.initialize()
        async with asyncio.timeout(0.5):
            while True:
                history = await (await database.connection.execute(
                    """SELECT trigger_source, discord_user_id, success, error_summary
                       FROM retailer_scan_history
                       WHERE retailer_key='shop' ORDER BY id DESC LIMIT 1"""
                )).fetchone()
                if history is not None and not (await manager.get_state("shop")).running:
                    break
                await asyncio.sleep(0.005)

        assert tuple(history) == (
            "scheduled", None, 0,
            "scan exceeded configured timeout of 0.01 seconds",
        )
        state = await manager.get_state("shop")
        assert state.health == "DEGRADED"
        assert state.consecutive_failures == 1
        assert not state.running
    finally:
        await scheduler.stop()

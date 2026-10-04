import asyncio
from pathlib import Path

import pytest
from unittest.mock import AsyncMock

from app.application import Application
from app.config import AppConfig, DiscordBotConfig, LoggingConfig, MonitorConfig


@pytest.mark.asyncio
async def test_application_starts_and_stops_gracefully(tmp_path: Path) -> None:
    config = AppConfig(
        monitor=MonitorConfig(),
        database_path=tmp_path / "app.db",
        logging=LoggingConfig(path=tmp_path / "app.log"),
    )
    application = Application(config)
    task = asyncio.create_task(application.run())
    for _ in range(100):
        if application.database.connection is not None:
            break
        await asyncio.sleep(0.01)
    assert application.database.connection is not None
    application.request_shutdown()
    await asyncio.wait_for(task, timeout=2)
    assert application.database.connection is None


@pytest.mark.asyncio
async def test_backup_is_scheduled_promptly_then_uses_configured_interval(tmp_path: Path) -> None:
    config = AppConfig(monitor=MonitorConfig(), database_path=tmp_path / "app.db",
                       logging=LoggingConfig(path=tmp_path / "app.log"),
                       retailers={}, backup_initial_delay_seconds=0.02,
                       backup_interval_hours=1)
    application = Application(config)
    application.database.backup = AsyncMock(return_value=tmp_path / "backup.db")
    await application.start()
    try:
        await asyncio.sleep(0.08)
        assert application.database.backup.await_count == 1
        await asyncio.sleep(0.03)
        assert application.database.backup.await_count == 1
    finally:
        await application.close()


@pytest.mark.asyncio
async def test_application_starts_enabled_discord_control(tmp_path: Path) -> None:
    config = AppConfig(
        monitor=MonitorConfig(),
        database_path=tmp_path / "app.db",
        logging=LoggingConfig(path=tmp_path / "app.log"),
        discord_bot=DiscordBotConfig(enabled=True, token="test-token"),
    )
    application = Application(config)
    application.discord_control.start = AsyncMock()
    application.discord_control.close = AsyncMock()

    await application.start()
    try:
        application.discord_control.start.assert_awaited_once_with()
    finally:
        await application.close()

    application.discord_control.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_close_attempts_every_resource_and_is_idempotent(tmp_path: Path) -> None:
    config = AppConfig(monitor=MonitorConfig(), database_path=tmp_path / "app.db",
                       logging=LoggingConfig(path=tmp_path / "app.log"))
    application = Application(config)
    order: list[str] = []

    async def close(name: str, fail: bool = False) -> None:
        order.append(name)
        if fail:
            raise RuntimeError(name)

    application.scheduler.stop = lambda: close("scheduler")  # type: ignore[method-assign]
    application.notifier.close = lambda: close("notifier", True)  # type: ignore[method-assign]
    application.http.close = lambda: close("http", True)  # type: ignore[method-assign]
    application.database.close = lambda: close("database")  # type: ignore[method-assign]
    await application.close()
    await application.close()
    assert order == ["scheduler", "notifier", "http", "database"]


@pytest.mark.asyncio
async def test_backup_failure_is_isolated_and_later_scheduler_job_runs() -> None:
    from app.scheduler import Scheduler

    scheduler = Scheduler()
    retailer_ran = asyncio.Event()

    async def failed_backup() -> None:
        raise OSError("simulated backup failure")

    async def retailer() -> None:
        retailer_ran.set()

    scheduler.add_interval_job("backup", failed_backup, 3600)
    scheduler.add_interval_job("retailer", retailer, 3600)
    try:
        await asyncio.wait_for(retailer_ran.wait(), 1)
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_shutdown_cancels_and_awaits_active_backup(tmp_path: Path) -> None:
    config = AppConfig(monitor=MonitorConfig(), database_path=tmp_path / "app.db",
                       logging=LoggingConfig(path=tmp_path / "app.log"),
                       backup_initial_delay_seconds=0.01)
    application = Application(config)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking_backup(*_: object) -> Path:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    application.database.backup = blocking_backup  # type: ignore[method-assign]
    await application.start()
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(application.close(), 1)
    assert cancelled.is_set()

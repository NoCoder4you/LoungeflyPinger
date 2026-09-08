import asyncio
from pathlib import Path

import pytest

from app.application import Application
from app.config import AppConfig, LoggingConfig, MonitorConfig


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

from pathlib import Path

import pytest

from app.config import NotificationConfig
from app.database import Database
from app.models import AlertType
from app.services.notification_settings import NotificationSettingsService


@pytest.mark.asyncio
async def test_notification_settings_persist_and_preserve_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.db"
    async with Database(path) as database:
        settings = NotificationSettingsService(
            database, NotificationConfig(product_removed_enabled=False)
        )
        await settings.initialize()
        assert not settings.enabled(AlertType.PRODUCT_REMOVED)
        assert settings.enabled(AlertType.RESTOCK)

        await settings.set_enabled(AlertType.PRODUCT_REMOVED, True, "42")
        await settings.set_enabled(AlertType.RESTOCK, False, "42")

    async with Database(path) as database:
        restored = NotificationSettingsService(database, NotificationConfig())
        await restored.initialize()
        assert restored.enabled(AlertType.PRODUCT_REMOVED)
        assert not restored.enabled(AlertType.RESTOCK)
        assert all(overridden for kind, _, overridden in restored.states()
                   if kind in {AlertType.PRODUCT_REMOVED, AlertType.RESTOCK})


@pytest.mark.asyncio
async def test_unknown_persisted_alert_type_is_ignored(tmp_path: Path) -> None:
    async with Database(tmp_path / "unknown.db") as database:
        await database.connection.execute(
            """INSERT INTO notification_runtime_overrides
               (alert_type,enabled,updated_at,updated_by_discord_user_id)
               VALUES ('FUTURE_ALERT',0,'2026-01-01T00:00:00+00:00','42')"""
        )
        await database.connection.commit()
        settings = NotificationSettingsService(database, NotificationConfig())

        await settings.initialize()

        assert all(enabled for _, enabled, _ in settings.states())

"""Durable runtime controls for outbound notification categories."""

from datetime import UTC, datetime

from app.config import NotificationConfig
from app.database import Database
from app.models import AlertType


class NotificationSettingsService:
    """Resolve startup defaults plus persistent Discord-managed overrides."""

    def __init__(self, database: Database, config: NotificationConfig) -> None:
        self.database = database
        self.config = config
        self._overrides: dict[AlertType, bool] = {}

    async def initialize(self) -> None:
        if self.database.connection is None:
            raise RuntimeError("database is not connected")
        rows = await (await self.database.connection.execute(
            "SELECT alert_type, enabled FROM notification_runtime_overrides"
        )).fetchall()
        overrides: dict[AlertType, bool] = {}
        for raw_type, raw_enabled in rows:
            try:
                overrides[AlertType(raw_type)] = bool(raw_enabled)
            except ValueError:
                # Ignore unknown values left by a newer application version.
                continue
        self._overrides = overrides

    def default_enabled(self, alert_type: AlertType) -> bool:
        if alert_type is AlertType.PRODUCT_REMOVED:
            return self.config.product_removed_enabled
        if alert_type is AlertType.OUT_OF_STOCK:
            return self.config.sold_out_enabled
        return True

    def enabled(self, alert_type: AlertType) -> bool:
        return self._overrides.get(alert_type, self.default_enabled(alert_type))

    def states(self) -> tuple[tuple[AlertType, bool, bool], ...]:
        return tuple(
            (alert_type, self.enabled(alert_type), alert_type in self._overrides)
            for alert_type in AlertType
        )

    async def set_enabled(self, alert_type: AlertType, enabled: bool, actor: str) -> None:
        if self.database.connection is None:
            raise RuntimeError("database is not connected")
        async with self.database.write_lock:
            await self.database.connection.execute(
                """INSERT INTO notification_runtime_overrides
                   (alert_type, enabled, updated_at, updated_by_discord_user_id)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(alert_type) DO UPDATE SET enabled=excluded.enabled,
                   updated_at=excluded.updated_at,
                   updated_by_discord_user_id=excluded.updated_by_discord_user_id""",
                (alert_type.value, int(enabled), datetime.now(UTC).isoformat(), actor),
            )
            await self.database.connection.commit()
        self._overrides[alert_type] = enabled

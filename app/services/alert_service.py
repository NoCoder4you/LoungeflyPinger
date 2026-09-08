"""Audit alert delivery attempts."""

from datetime import UTC, datetime

from app.database import Database
from app.models import AlertType


class AlertService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def record(self, product_id: int, alert_type: AlertType, destination: str, success: bool) -> None:
        connection = self.database.connection
        if connection is None:
            raise RuntimeError("database is not connected")
        await connection.execute(
            "INSERT INTO alerts(product_id, alert_type, sent_at, destination, success) VALUES (?, ?, ?, ?, ?)",
            (product_id, alert_type.value, datetime.now(UTC).isoformat(), destination, success),
        )
        await connection.commit()

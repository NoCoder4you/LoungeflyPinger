"""Durable domain events, independent of notification delivery."""
from datetime import UTC, datetime
from decimal import Decimal

from app.database import Database
from app.models import AlertType, Availability

DEFAULT_EVENT_LIMIT = 20_000
DEFAULT_PER_PRODUCT_LIMIT = 250


class ProductEventService:
    def __init__(self, database: Database, *, row_limit: int = DEFAULT_EVENT_LIMIT,
                 per_product_limit: int = DEFAULT_PER_PRODUCT_LIMIT) -> None:
        self.database = database
        self.row_limit = row_limit
        self.per_product_limit = per_product_limit

    async def record(self, product_id: int, event_type: AlertType | str, *,
                     previous_summary: str | None = None, new_summary: str | None = None,
                     price: Decimal | None = None, currency: str | None = None,
                     availability: Availability | str | None = None) -> None:
        connection = self.database.connection
        if connection is None:
            raise RuntimeError("database is not connected")
        kind = event_type.value if isinstance(event_type, AlertType) else str(event_type)
        stock = availability.value if isinstance(availability, Availability) else availability
        await connection.execute("""INSERT INTO product_events
            (product_id,event_type,detected_at,previous_summary,new_summary,price,currency,availability)
            VALUES(?,?,?,?,?,?,?,?)""", (product_id, kind, datetime.now(UTC).isoformat(),
            (previous_summary or "")[:500] or None, (new_summary or "")[:500] or None,
            str(price) if price is not None else None, currency, stock))
        await connection.execute("""DELETE FROM product_events WHERE product_id=? AND id NOT IN
            (SELECT id FROM product_events WHERE product_id=? ORDER BY id DESC LIMIT ?)""",
            (product_id, product_id, self.per_product_limit))
        await connection.execute("""DELETE FROM product_events WHERE id NOT IN
            (SELECT id FROM product_events ORDER BY id DESC LIMIT ?)""", (self.row_limit,))

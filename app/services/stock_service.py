"""Persistence operations for current product state."""

from datetime import UTC, datetime

from app.database import Database
from app.models import Product


class StockService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def record(self, product_id: int, product: Product) -> None:
        connection = self.database.connection
        if connection is None:
            raise RuntimeError("database is not connected")
        await connection.execute(
            """INSERT INTO product_states(product_id, availability, price, currency, preorder, checked_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(product_id) DO UPDATE SET availability=excluded.availability,
                 price=excluded.price, currency=excluded.currency, preorder=excluded.preorder,
                 checked_at=excluded.checked_at""",
            (product_id, product.availability.value, str(product.price) if product.price is not None else None,
             product.currency, product.preorder, datetime.now(UTC).isoformat()),
        )
        await connection.commit()

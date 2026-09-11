"""Persistence operations for normalized products."""

from datetime import UTC, datetime

from app.database import Database
from app.models import Product


class ProductService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def upsert(self, product: Product) -> int:
        connection = self.database.connection
        if connection is None:
            raise RuntimeError("database is not connected")
        now = datetime.now(UTC).isoformat()
        await connection.execute(
            "INSERT INTO retailers(name) VALUES (?) ON CONFLICT(name) DO NOTHING",
            (product.retailer,),
        )
        await connection.execute(
            """INSERT INTO products
               (retailer, retailer_product_id, name, url, image_url, sku, franchise, character,
                product_type, exclusive, new_release, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(retailer, retailer_product_id) DO UPDATE SET
                 name=excluded.name, url=excluded.url, image_url=excluded.image_url,
                 sku=COALESCE(excluded.sku, products.sku),
                 franchise=excluded.franchise, character=excluded.character,
                 product_type=excluded.product_type, exclusive=excluded.exclusive,
                 new_release=excluded.new_release,
                 last_seen=excluded.last_seen, missing_scans=0, removed_at=NULL""",
            (product.retailer, product.retailer_product_id, product.name, product.url,
             product.image_url, product.sku, product.franchise, product.character,
             product.product_type, product.exclusive, product.new_release, now, now),
        )
        cursor = await connection.execute(
            "SELECT id FROM products WHERE retailer=? AND retailer_product_id=?",
            (product.retailer, product.retailer_product_id),
        )
        row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    async def mark_seen(self, product_id: int) -> None:
        """Clear persisted absence state after a successful product observation."""
        connection = self.database.connection
        if connection is None:
            raise RuntimeError("database is not connected")
        await connection.execute(
            "UPDATE products SET missing_scans=0, removed_at=NULL WHERE id=?", (product_id,)
        )

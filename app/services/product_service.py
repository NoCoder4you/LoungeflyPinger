"""Persistence operations for normalized products."""

import json
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
                variant_id, barcode, vendor, tags, listing_published_at, product_type, exclusive,
                exclusive_retailer, exclusive_region, new_release, compare_at_price, collections,
                sale, clearance, collection_type, vaulted, exclusivity_text, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(retailer, retailer_product_id) DO UPDATE SET
                 name=excluded.name, url=excluded.url, image_url=excluded.image_url,
                 sku=COALESCE(excluded.sku, products.sku),
                 variant_id=COALESCE(excluded.variant_id, products.variant_id),
                 barcode=COALESCE(excluded.barcode, products.barcode), vendor=excluded.vendor,
                 tags=excluded.tags, listing_published_at=excluded.listing_published_at,
                 franchise=excluded.franchise, character=excluded.character,
                 product_type=excluded.product_type, exclusive=excluded.exclusive,
                 exclusive_retailer=excluded.exclusive_retailer,
                 exclusive_region=excluded.exclusive_region,
                 new_release=excluded.new_release,
                 compare_at_price=excluded.compare_at_price, collections=excluded.collections,
                 sale=excluded.sale, clearance=excluded.clearance,
                 collection_type=excluded.collection_type, vaulted=excluded.vaulted,
                 exclusivity_text=excluded.exclusivity_text,
                 last_seen=excluded.last_seen, missing_scans=0, removed_at=NULL""",
            (product.retailer, product.retailer_product_id, product.name, product.url,
             product.image_url, product.sku, product.franchise, product.character,
             product.variant_id, product.barcode, product.vendor,
             json.dumps(product.tags),
             product.listing_published_at.isoformat() if product.listing_published_at else None,
             product.product_type, product.exclusive, product.exclusive_retailer,
             product.exclusive_region,
             product.new_release,
             str(product.compare_at_price) if product.compare_at_price is not None else None,
             json.dumps(product.collections), product.sale, product.clearance,
             product.collection_type, product.vaulted, product.exclusivity_text, now, now),
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

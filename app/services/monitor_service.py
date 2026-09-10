"""Orchestration shared by retailer adapters."""

import logging
from datetime import UTC, datetime
from decimal import Decimal

from app.config import PriceAlertConfig
from app.database import Database
from app.models import Alert, AlertType, Availability, Product
from app.monitors.base import RetailerMonitor
from app.notifications.base import NotificationProvider
from app.services.product_service import ProductService
from app.services.stock_service import StockService
from app.watchlist import Watchlist, classify_product

LOGGER = logging.getLogger("monitor.retailers")


class MonitorService:
    """Persist one discovery pass and emit only meaningful post-baseline changes."""

    def __init__(
        self,
        monitor: RetailerMonitor,
        database: Database,
        notifier: NotificationProvider,
        *,
        retailer_name: str,
        watchlist: Watchlist | None = None,
        price_alerts: PriceAlertConfig | None = None,
        missing_scan_threshold: int = 3,
    ) -> None:
        self.monitor = monitor
        self.database = database
        self.notifier = notifier
        self.retailer_name = retailer_name
        self.products = ProductService(database)
        self.stock = StockService(database)
        # None keeps backwards compatibility for programmatic users; an explicitly
        # empty configured watchlist intentionally sends no product alerts.
        self.watchlist = watchlist
        self.price_alerts = price_alerts or PriceAlertConfig()
        if missing_scan_threshold < 1:
            raise ValueError("missing_scan_threshold must be at least one")
        self.missing_scan_threshold = missing_scan_threshold

    @staticmethod
    def _stock_alert(previous: Availability, product: Product) -> AlertType | None:
        current = product.availability
        if previous == Availability.OUT_OF_STOCK and current == Availability.IN_STOCK:
            return AlertType.RESTOCK
        if previous == Availability.COMING_SOON and current == Availability.IN_STOCK:
            return AlertType.AVAILABILITY
        if previous in {Availability.COMING_SOON, Availability.OUT_OF_STOCK} and (
            current == Availability.PREORDER or product.preorder
        ):
            return AlertType.PREORDER_OPEN
        return None

    def _is_price_drop(self, previous: Decimal | None, product: Product, currency: str | None) -> bool:
        if not self.price_alerts.enabled or previous is None or product.price is None:
            return False
        if currency != product.currency or previous <= product.price or previous == 0:
            return False
        drop = previous - product.price
        percent = drop * Decimal("100") / previous
        return (drop >= Decimal(str(self.price_alerts.minimum_drop_value)) and
                percent >= Decimal(str(self.price_alerts.minimum_drop_percent)))

    def _matches(self, product: Product):
        return self.watchlist.match(product) if self.watchlist is not None else ()

    async def _send(self, alert: Alert, alerts: list[Alert]) -> None:
        if self.watchlist is not None and not alert.watch_matches:
            return
        alerts.append(alert)
        await self.notifier.send(alert)

    async def synchronize(self) -> list[Alert]:
        connection = self.database.connection
        if connection is None:
            raise RuntimeError("database is not connected")
        discovered = await self.monitor.discover_products()
        retailer = discovered[0].retailer if discovered else self.retailer_name
        prior_sync = await (
            await connection.execute("SELECT last_success FROM retailers WHERE name = ?", (retailer,))
        ).fetchone()
        alerts: list[Alert] = []
        seen_ids: set[int] = set()
        for product in discovered:
            product = classify_product(product)
            known_product = await (await connection.execute(
                "SELECT id, removed_at FROM products WHERE retailer=? AND retailer_product_id=?",
                (product.retailer, product.retailer_product_id),
            )).fetchone()
            was_removed = known_product is not None and known_product[1] is not None
            product_id = await self.products.upsert(product)
            seen_ids.add(product_id)
            prior_state = await self.stock.current(product_id)
            previous = prior_state.availability if prior_state else None
            alert_types: list[AlertType] = []
            if product.availability != Availability.ERROR:
                if prior_sync is not None and prior_sync[0] is not None and known_product is None:
                    alert_types.append(AlertType.NEW_PRODUCT)
                elif previous is not None:
                    if was_removed:
                        alert_types.append(AlertType.AVAILABILITY)
                    else:
                        stock_alert = self._stock_alert(previous, product)
                        if stock_alert is not None:
                            alert_types.append(stock_alert)
                    if self._is_price_drop(prior_state.price, product, prior_state.currency):
                        alert_types.append(AlertType.PRICE_DROP)
            # A failed parse/check provides no inventory evidence. Preserve the
            # last known stock state until a successful observation replaces it.
            if product.availability != Availability.ERROR:
                await self.stock.record(product_id, product)
            for alert_type in alert_types:
                alert = Alert(
                    alert_type, product, product_id,
                    previous_price=prior_state.price if alert_type == AlertType.PRICE_DROP else None,
                    previous_availability=previous, watch_matches=self._matches(product),
                )
                await self._send(alert, alerts)
            # Keep a tombstone through an unsuccessful check so the next usable
            # observation still produces the reappearance notification.
            if product.availability != Availability.ERROR:
                await self.products.mark_seen(product_id)
        # Absence is only evidence after repeated successful, complete listing scans.
        missing_rows = await (await connection.execute(
            """SELECT p.id, p.retailer, p.retailer_product_id, p.name, p.url, p.image_url, p.sku,
                      p.product_type, p.franchise, p.character, p.exclusive, p.missing_scans,
                      s.availability, s.price, s.currency, s.preorder
                 FROM products p LEFT JOIN product_states s ON s.product_id=p.id
                WHERE p.retailer=? AND p.removed_at IS NULL""", (retailer,)
        )).fetchall()
        now = datetime.now(UTC).isoformat()
        for row in missing_rows:
            if row[0] in seen_ids:
                continue
            count = row[11] + 1
            removed = count >= self.missing_scan_threshold
            await connection.execute(
                "UPDATE products SET missing_scans=?, removed_at=? WHERE id=?",
                (count, now if removed else None, row[0]),
            )
            if removed and row[12] is not None:
                missing_product = Product(
                    retailer=row[1], retailer_product_id=row[2], name=row[3], url=row[4],
                    availability=Availability(row[12]), image_url=row[5],
                    price=Decimal(row[13]) if row[13] is not None else None,
                    currency=row[14], product_type=row[7], franchise=row[8], character=row[9],
                    exclusive=bool(row[10]), preorder=bool(row[15]), sku=row[6],
                )
                await self._send(Alert(
                    AlertType.PRODUCT_REMOVED, missing_product, row[0],
                    previous_availability=Availability(row[12]),
                    watch_matches=self._matches(missing_product),
                ), alerts)
        await connection.execute(
            """INSERT INTO retailers(name, last_success, consecutive_failures) VALUES (?, ?, 0)
               ON CONFLICT(name) DO UPDATE SET last_success=excluded.last_success,
                 consecutive_failures=0""",
            (retailer, now),
        )
        await connection.commit()
        LOGGER.info("Retailer synchronized", extra={"retailer": retailer, "products": len(discovered)})
        return alerts

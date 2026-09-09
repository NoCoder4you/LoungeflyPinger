"""Orchestration shared by retailer adapters."""

import logging
from datetime import UTC, datetime

from app.database import Database
from app.models import Alert, AlertType, Availability
from app.monitors.base import RetailerMonitor
from app.notifications.base import NotificationProvider
from app.services.product_service import ProductService
from app.services.stock_service import StockService

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
    ) -> None:
        self.monitor = monitor
        self.database = database
        self.notifier = notifier
        self.retailer_name = retailer_name
        self.products = ProductService(database)
        self.stock = StockService(database)

    async def synchronize(self) -> list[Alert]:
        connection = self.database.connection
        if connection is None:
            raise RuntimeError("database is not connected")
        try:
            return await self._synchronize()
        except Exception:
            now = datetime.now(UTC).isoformat()
            await connection.execute(
                """INSERT INTO retailers(name, last_failure, consecutive_failures) VALUES (?, ?, 1)
                   ON CONFLICT(name) DO UPDATE SET last_failure=excluded.last_failure,
                     consecutive_failures=retailers.consecutive_failures + 1""",
                (self.retailer_name, now),
            )
            await connection.commit()
            LOGGER.exception("Retailer synchronization failed", extra={"retailer": self.retailer_name})
            raise

    async def _synchronize(self) -> list[Alert]:
        connection = self.database.connection
        assert connection is not None
        discovered = await self.monitor.discover_products()
        retailer = discovered[0].retailer if discovered else self.retailer_name
        prior_sync = await (
            await connection.execute("SELECT last_success FROM retailers WHERE name = ?", (retailer,))
        ).fetchone()
        alerts: list[Alert] = []
        for product in discovered:
            product_id = await self.products.upsert(product)
            previous = await self.stock.current_availability(product_id)
            alert_type: AlertType | None = None
            if prior_sync is not None and prior_sync[0] is not None and previous is None:
                alert_type = AlertType.NEW_PRODUCT
            elif previous == Availability.OUT_OF_STOCK and product.availability == Availability.IN_STOCK:
                alert_type = AlertType.RESTOCK
            # A failed parse/check provides no inventory evidence. Preserve the
            # last known stock state until a successful observation replaces it.
            if product.availability != Availability.ERROR:
                await self.stock.record(product_id, product)
            if alert_type is not None:
                alert = Alert(alert_type, product, product_id, previous_availability=previous)
                alerts.append(alert)
                await self.notifier.send(alert)
        now = datetime.now(UTC).isoformat()
        await connection.execute(
            """INSERT INTO retailers(name, last_success, consecutive_failures) VALUES (?, ?, 0)
               ON CONFLICT(name) DO UPDATE SET last_success=excluded.last_success,
                 consecutive_failures=0""",
            (retailer, now),
        )
        await connection.commit()
        LOGGER.info("Retailer synchronized", extra={"retailer": retailer, "products": len(discovered)})
        return alerts

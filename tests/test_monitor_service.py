from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.database import Database
from app.models import AlertType, Availability, Product
from app.services.monitor_service import MonitorService


class Monitor:
    def __init__(self, products): self.products = products
    async def discover_products(self): return self.products


class FailingMonitor:
    async def discover_products(self): raise RuntimeError("challenge")


class Notifier:
    def __init__(self): self.alerts = []
    async def send(self, alert): self.alerts.append(alert); return True
    async def close(self): pass


def product(product_id="1", availability=Availability.OUT_OF_STOCK):
    return Product("GeekCore", product_id, f"Bag {product_id}", f"https://example.com/{product_id}",
                   availability, price=Decimal("10"), currency="GBP")


@pytest.mark.asyncio
async def test_initial_sync_is_silent_then_new_and_restock_are_alerted(tmp_path: Path):
    async with Database(tmp_path / "state.db") as database:
        notifier = Notifier()
        monitor = Monitor([product()])
        service = MonitorService(monitor, database, notifier, retailer_name="GeekCore")
        assert await service.synchronize() == []
        assert notifier.alerts == []

        monitor.products = [product(availability=Availability.IN_STOCK), product("2")]
        alerts = await service.synchronize()
        assert [alert.alert_type for alert in alerts] == [AlertType.RESTOCK, AlertType.NEW_PRODUCT]
        assert [alert.previous_availability for alert in alerts] == [Availability.OUT_OF_STOCK, None]


@pytest.mark.asyncio
async def test_parser_error_does_not_overwrite_known_stock_state(tmp_path: Path):
    async with Database(tmp_path / "state.db") as database:
        monitor = Monitor([product(availability=Availability.IN_STOCK)])
        service = MonitorService(monitor, database, Notifier(), retailer_name="GeekCore")
        await service.synchronize()

        monitor.products = [product(availability=Availability.ERROR)]
        await service.synchronize()

        row = await (await database.connection.execute(
            "SELECT id FROM products WHERE retailer=? AND retailer_product_id=?", ("GeekCore", "1")
        )).fetchone()
        assert row is not None
        assert await service.stock.current_availability(row[0]) == Availability.IN_STOCK


@pytest.mark.asyncio
async def test_failed_scan_records_failure_and_preserves_known_stock(tmp_path: Path):
    async with Database(tmp_path / "state.db") as database:
        initial = MonitorService(Monitor([product(availability=Availability.IN_STOCK)]), database, Notifier(), retailer_name="GeekCore")
        await initial.synchronize()

        failed = MonitorService(FailingMonitor(), database, Notifier(), retailer_name="GeekCore")
        with pytest.raises(RuntimeError, match="challenge"):
            await failed.synchronize()

        row = await (await database.connection.execute(
            "SELECT p.id, r.consecutive_failures FROM products p JOIN retailers r ON r.name=p.retailer "
            "WHERE p.retailer=? AND p.retailer_product_id=?", ("GeekCore", "1")
        )).fetchone()
        assert row is not None
        assert row[1] == 1
        assert await failed.stock.current_availability(row[0]) == Availability.IN_STOCK

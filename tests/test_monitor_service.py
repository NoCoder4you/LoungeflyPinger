from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.database import Database
from app.config import PriceAlertConfig
from app.models import AlertType, Availability, Product
from app.services.monitor_service import MonitorService
from app.watchlist import WatchRule, Watchlist


class Monitor:
    def __init__(self, products): self.products = products
    async def discover_products(self): return self.products


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
async def test_parser_error_does_not_overwrite_state_or_emit_price_drop(tmp_path: Path):
    async with Database(tmp_path / "state.db") as database:
        notifier = Notifier()
        monitor = Monitor([product(availability=Availability.IN_STOCK)])
        service = MonitorService(monitor, database, notifier, retailer_name="GeekCore")
        await service.synchronize()

        monitor.products = [replace(
            product(availability=Availability.ERROR), price=Decimal("1")
        )]
        assert await service.synchronize() == []
        assert notifier.alerts == []

        row = await (await database.connection.execute(
            "SELECT id FROM products WHERE retailer=? AND retailer_product_id=?", ("GeekCore", "1")
        )).fetchone()
        assert row is not None
        state = await service.stock.current(row[0])
        assert state is not None
        assert state.availability == Availability.IN_STOCK
        assert state.price == Decimal("10")
        history_count = await (await database.connection.execute(
            "SELECT COUNT(*) FROM product_state_history WHERE product_id=?", (row[0],)
        )).fetchone()
        assert history_count == (1,)


@pytest.mark.asyncio
async def test_configured_watchlist_filters_alerts_and_attaches_matches(tmp_path: Path):
    async with Database(tmp_path / "watch.db") as database:
        notifier = Notifier()
        monitor = Monitor([product()])
        watches = Watchlist((WatchRule("Only wanted", keywords=("wanted",)),))
        service = MonitorService(
            monitor, database, notifier, retailer_name="GeekCore", watchlist=watches
        )
        await service.synchronize()
        monitor.products = [
            product("2"),
            replace(product("1"), name="Wanted Bag", availability=Availability.IN_STOCK),
        ]
        alerts = await service.synchronize()

        assert len(alerts) == 1
        assert alerts[0].product.retailer_product_id == "1"
        assert [match.name for match in alerts[0].watch_matches] == ["Only wanted"]
        assert notifier.alerts == alerts


@pytest.mark.asyncio
@pytest.mark.parametrize(("before", "after", "preorder", "expected"), [
    (Availability.OUT_OF_STOCK, Availability.IN_STOCK, False, AlertType.RESTOCK),
    (Availability.COMING_SOON, Availability.IN_STOCK, False, AlertType.AVAILABILITY),
    (Availability.COMING_SOON, Availability.PREORDER, True, AlertType.PREORDER_OPEN),
    (Availability.OUT_OF_STOCK, Availability.PREORDER, True, AlertType.PREORDER_OPEN),
    (Availability.ERROR, Availability.IN_STOCK, False, None),
    (Availability.IN_STOCK, Availability.IN_STOCK, False, None),
])
async def test_stock_transitions(tmp_path: Path, before, after, preorder, expected):
    async with Database(tmp_path / f"{before}-{after}.db") as database:
        monitor = Monitor([replace(product(), availability=before, preorder=before == Availability.PREORDER)])
        service = MonitorService(monitor, database, Notifier(), retailer_name="GeekCore")
        await service.synchronize()
        monitor.products = [replace(product(), availability=after, preorder=preorder)]
        alerts = await service.synchronize()
        assert [item.alert_type for item in alerts] == ([] if expected is None else [expected])


@pytest.mark.asyncio
async def test_every_successful_check_is_historic_and_tracks_price_range(tmp_path: Path):
    async with Database(tmp_path / "history.db") as database:
        monitor = Monitor([replace(product(), price=Decimal("79.99"))])
        service = MonitorService(monitor, database, Notifier(), retailer_name="GeekCore")
        await service.synchronize()
        monitor.products = [replace(product(), price=Decimal("59.99"), preorder=True)]
        await service.synchronize()
        product_id = (await (await database.connection.execute("SELECT id FROM products")).fetchone())[0]
        state = await service.stock.current(product_id)
        assert state.price == Decimal("59.99")
        assert state.previous_price == Decimal("79.99")
        assert state.lowest_price == Decimal("59.99")
        assert state.highest_price == Decimal("79.99")
        history = await (await database.connection.execute(
            "SELECT availability, price, currency, preorder, checked_at FROM product_state_history"
        )).fetchall()
        assert len(history) == 2
        assert history[-1][1:4] == ("59.99", "GBP", 1)
        assert history[-1][4]


@pytest.mark.asyncio
@pytest.mark.parametrize(("old", "new", "expected"), [
    ("79.99", "59.99", True),
    ("50", "45", True),
    ("50", "46", False),
    ("100", "94", False),
    ("50", "55", False),
])
async def test_price_drop_thresholds(tmp_path: Path, old, new, expected):
    async with Database(tmp_path / f"price-{old}-{new}.db") as database:
        monitor = Monitor([replace(product(), price=Decimal(old))])
        notifier = Notifier()
        service = MonitorService(monitor, database, notifier, retailer_name="GeekCore",
                                 price_alerts=PriceAlertConfig(True, 10, 5))
        await service.synchronize()
        monitor.products = [replace(product(), price=Decimal(new))]
        alerts = await service.synchronize()
        assert [a.alert_type for a in alerts] == ([AlertType.PRICE_DROP] if expected else [])
        if expected:
            assert alerts[0].previous_price == Decimal(old)


@pytest.mark.asyncio
async def test_missing_product_alerts_once_only_after_threshold_and_survives_restart(tmp_path: Path):
    path = tmp_path / "missing.db"
    async with Database(path) as database:
        monitor = Monitor([product()])
        notifier = Notifier()
        service = MonitorService(monitor, database, notifier, retailer_name="GeekCore",
                                 missing_scan_threshold=2)
        await service.synchronize()
        monitor.products = []
        assert await service.synchronize() == []
    async with Database(path) as database:
        notifier = Notifier()
        service = MonitorService(Monitor([]), database, notifier, retailer_name="GeekCore",
                                 missing_scan_threshold=2)
        alerts = await service.synchronize()
        assert [a.alert_type for a in alerts] == [AlertType.PRODUCT_REMOVED]
        assert await service.synchronize() == []


@pytest.mark.asyncio
async def test_removed_product_reappearance_alerts_with_unchanged_availability(tmp_path: Path):
    path = tmp_path / "reappears.db"
    async with Database(path) as database:
        monitor = Monitor([product(availability=Availability.OUT_OF_STOCK)])
        service = MonitorService(
            monitor, database, Notifier(), retailer_name="GeekCore", missing_scan_threshold=1
        )
        await service.synchronize()
        monitor.products = []
        assert [a.alert_type for a in await service.synchronize()] == [AlertType.PRODUCT_REMOVED]

    async with Database(path) as database:
        notifier = Notifier()
        service = MonitorService(
            Monitor([product(availability=Availability.OUT_OF_STOCK)]),
            database,
            notifier,
            retailer_name="GeekCore",
            missing_scan_threshold=1,
        )
        alerts = await service.synchronize()

        assert [a.alert_type for a in alerts] == [AlertType.AVAILABILITY]
        assert alerts[0].previous_availability == Availability.OUT_OF_STOCK
        row = await (await database.connection.execute(
            "SELECT missing_scans, removed_at FROM products WHERE retailer_product_id='1'"
        )).fetchone()
        assert row == (0, None)


@pytest.mark.asyncio
async def test_removed_product_error_retains_reappearance_alert(tmp_path: Path):
    async with Database(tmp_path / "reappears-after-error.db") as database:
        monitor = Monitor([product()])
        service = MonitorService(
            monitor, database, Notifier(), retailer_name="GeekCore", missing_scan_threshold=1
        )
        await service.synchronize()
        monitor.products = []
        await service.synchronize()
        monitor.products = [product(availability=Availability.ERROR)]
        assert await service.synchronize() == []
        monitor.products = [product()]
        assert [a.alert_type for a in await service.synchronize()] == [AlertType.AVAILABILITY]

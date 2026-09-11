from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

import app.monitors.boxlunch as module
from app.database import Database
from app.models import AlertType, Availability, ReleasePrecision
from app.monitors.boxlunch import BoxLunchMonitor, BoxLunchParseError
from app.services.monitor_service import MonitorService

FIXTURES = Path(__file__).parent / "fixtures" / "boxlunch"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeHttp:
    def __init__(self, responses): self.responses = list(responses); self.urls = []
    async def get_text(self, url): self.urls.append(url); return self.responses.pop(0)


@pytest.mark.asyncio
async def test_discovery_paginates_filters_and_deduplicates_across_pages(monkeypatch):
    monkeypatch.setattr(module, "PAGE_SIZE", 4)
    http = FakeHttp([fixture("listing.html"), fixture("page2.html")])
    products = await BoxLunchMonitor(http).discover_products()
    assert [p.retailer_product_id for p in products] == ["BL100", "BL200", "BL500"]
    assert "q=loungefly+mini+backpack&start=0&sz=4" in http.urls[0]
    assert "start=4&sz=4" in http.urls[1]


def test_exclusive_sale_price_availability_and_sku_are_normalized():
    raw = BoxLunchMonitor.parse_listing(fixture("listing.html"))[0]
    product = BoxLunchMonitor(FakeHttp([])).parse_product(raw, source_url="https://www.boxlunch.com/")
    assert product.retailer == "BoxLunch" and product.currency == "USD"
    assert product.price == Decimal("69.90")
    assert product.availability == Availability.IN_STOCK
    assert product.retailer_product_id == product.sku == "BL100"
    assert product.exclusive and product.exclusive_retailer == "BoxLunch"


def test_out_of_stock_and_preorder_release_data_are_normalized():
    listing = BoxLunchMonitor.parse_listing(fixture("listing.html"))
    page2 = BoxLunchMonitor.parse_listing(fixture("page2.html"))
    monitor = BoxLunchMonitor(FakeHttp([]))
    sold_out = monitor.parse_product(listing[1], source_url="https://www.boxlunch.com/")
    preorder = monitor.parse_product(page2[1], source_url="https://www.boxlunch.com/")
    assert sold_out.availability == Availability.OUT_OF_STOCK
    assert preorder.availability == Availability.PREORDER and preorder.preorder
    assert preorder.release.precision == ReleasePrecision.EXACT_DATETIME
    assert preorder.release.timezone == "America/Los_Angeles"


@pytest.mark.parametrize("name,brand", [
    ("Loungefly Mini Backpack Wallet", "LOUNGFLY"),
    ("Loungefly Mini Backpack Bag Charm", "LOUNGFLY"),
    ("Loungefly Mini Backpack Crossbody", "LOUNGFLY"),
    ("Loungefly Mini Backpack Tote", "LOUNGFLY"),
    ("Disney Princess Mini Backpack", "Disney"),
])
def test_filtering_excludes_non_target_products(name, brand):
    assert not BoxLunchMonitor._is_loungefly_mini_backpack({"name": name, "brand": {"name": brand}})


@pytest.mark.asyncio
async def test_product_page_preorder_and_parser_failure_preserves_state():
    original_raw = BoxLunchMonitor.parse_listing(fixture("listing.html"))[0]
    original = BoxLunchMonitor(FakeHttp([])).parse_product(original_raw, source_url="https://www.boxlunch.com/")
    checked = await BoxLunchMonitor(FakeHttp([fixture("product.html")])).check_product(
        replace(original, retailer_product_id="BL500", sku="BL500", url="https://www.boxlunch.com/product/loungefly-coraline-mini-backpack/BL500.html")
    )
    assert checked.availability == Availability.PREORDER
    failed = await BoxLunchMonitor(FakeHttp(["<html>changed</html>"])).check_product(original)
    assert failed.availability == Availability.ERROR
    assert failed.price == original.price and failed.exclusive_retailer == "BoxLunch"


@pytest.mark.parametrize("html", ["", "<html>changed</html>", fixture("malformed.html")])
def test_malformed_listing_fails(html):
    with pytest.raises(BoxLunchParseError):
        BoxLunchMonitor.parse_listing(html)


@pytest.mark.asyncio
async def test_new_product_after_initial_sync_uses_existing_alert_flow(tmp_path):
    raw = BoxLunchMonitor.parse_listing(fixture("listing.html"))
    adapter = BoxLunchMonitor(FakeHttp([]))
    products = [adapter.parse_product(item, source_url="https://www.boxlunch.com/") for item in raw[:2]]
    class Monitor:
        def __init__(self): self.products = products[:1]
        async def discover_products(self): return self.products
    class Notifier:
        def __init__(self): self.alerts = []
        async def send(self, alert): self.alerts.append(alert); return True
    async with Database(tmp_path / "boxlunch.db") as database:
        monitor = Monitor(); notifier = Notifier()
        service = MonitorService(monitor, database, notifier, retailer_name="BoxLunch")
        assert await service.synchronize() == []
        monitor.products = products
        alerts = await service.synchronize()
        assert [alert.alert_type for alert in alerts] == [AlertType.NEW_PRODUCT]

from pathlib import Path

import pytest

import app.monitors.disney_store_uk as module
from app.models import AlertType, Availability, ReleasePrecision
from app.monitors.disney_store_uk import DisneyStoreParseError, DisneyStoreUSMonitor
from app.services.monitor_service import MonitorService

FIXTURES = Path(__file__).parent / "fixtures" / "disney_store_us"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeHttp:
    def __init__(self, responses): self.responses = list(responses); self.urls = []
    async def get_text(self, url): self.urls.append(url); return self.responses.pop(0)


@pytest.mark.asyncio
async def test_us_discovery_requires_loungefly_mini_backpacks_and_deduplicates(monkeypatch):
    monkeypatch.setattr(module, "PAGE_SIZE", 9)
    http = FakeHttp([fixture("listing.html"), fixture("page2.html")])
    products = await DisneyStoreUSMonitor(http).discover_products()
    assert [p.retailer_product_id for p in products] == [str(i) for i in range(1001, 1008)] + ["1009"]
    assert "brands/loungefly/?start=0&sz=9" in http.urls[0]
    assert all(p.retailer == "Disney Store US" and p.currency == "USD" for p in products)


def parsed_products():
    monitor = DisneyStoreUSMonitor(FakeHttp([]))
    return [monitor.parse_product(raw, source_url="https://www.disneystore.com/brands/loungefly/")
            for raw in monitor.parse_listing(fixture("listing.html"))
            if monitor._is_mini_backpack(raw["product"])]


def test_available_backorder_back_in_stock_and_new_are_normalized():
    available, backorder, back_in_stock, new = parsed_products()[:4]
    assert available.availability == Availability.IN_STOCK
    assert backorder.availability == Availability.BACKORDER
    assert back_in_stock.availability == Availability.IN_STOCK
    assert not back_in_stock.new_release
    assert new.availability == Availability.IN_STOCK and new.new_release
    assert parsed_products()[-1].availability == Availability.OUT_OF_STOCK


def test_sale_price_item_number_and_images_are_normalized():
    sale = parsed_products()[4]
    assert str(sale.price) == "67.50"
    assert sale.retailer_product_id == sale.sku == "1005"
    assert sale.image_url == "https://cdn.example.com/1005.jpg"


def test_disney_store_and_disney_parks_exclusives_are_identified():
    store, parks = parsed_products()[5:7]
    assert store.exclusive and store.franchise is None
    assert parks.exclusive and parks.franchise == "Disney Parks"


def test_disney_parks_collection_statement_on_product_page_is_reliable_evidence():
    raw = DisneyStoreUSMonitor.parse_listing(fixture("listing.html"))[0]
    raw["page_text"] = "From the Disney Parks | Loungefly Collection"
    product = DisneyStoreUSMonitor(FakeHttp([])).parse_product(
        raw, source_url="https://www.disneystore.com/brands/loungefly/"
    )
    assert product.exclusive and product.franchise == "Disney Parks"


def test_non_loungefly_disney_backpack_is_excluded():
    raw = DisneyStoreUSMonitor.parse_listing(fixture("listing.html"))[7]["product"]
    assert not DisneyStoreUSMonitor._is_mini_backpack(raw)


@pytest.mark.asyncio
async def test_preorder_product_page_has_explicit_release_date_and_time():
    monitor = DisneyStoreUSMonitor(FakeHttp([fixture("preorder.html")]))
    original = parsed_products()[1]
    checked = await monitor.check_product(original)
    assert checked.availability == Availability.PREORDER and checked.preorder
    assert checked.release.precision == ReleasePrecision.EXACT_DATETIME
    assert checked.release.timezone == "America/Los_Angeles"


@pytest.mark.parametrize("html", ["", "<html>changed</html>", fixture("malformed.html")])
def test_malformed_listing_fails(html):
    with pytest.raises(DisneyStoreParseError):
        DisneyStoreUSMonitor.parse_listing(html)


@pytest.mark.asyncio
async def test_parser_failure_preserves_previous_state_as_error():
    original = parsed_products()[0]
    checked = await DisneyStoreUSMonitor(FakeHttp(["<html>changed</html>"])).check_product(original)
    assert checked.availability == Availability.ERROR
    assert checked.price == original.price and checked.sku == original.sku


def test_backorder_to_available_produces_one_restock_not_released():
    previous = parsed_products()[1]
    current = parsed_products()[0]
    current = __import__('dataclasses').replace(current, retailer_product_id=previous.retailer_product_id,
                                                sku=previous.sku, name=previous.name, url=previous.url)
    assert MonitorService._stock_alert(previous.availability, current) == AlertType.RESTOCK

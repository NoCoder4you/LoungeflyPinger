from decimal import Decimal
from pathlib import Path

import pytest

import app.monitors.boxlunch as module
from app.models import Availability, ReleasePrecision
from app.monitors.boxlunch import BoxLunchParseError, HotTopicUSMonitor

FIXTURES = Path(__file__).parent / "fixtures" / "hot_topic_us"


def fixture(name): return (FIXTURES / name).read_text(encoding="utf-8")


class FakeHttp:
    def __init__(self, responses): self.responses = list(responses); self.urls = []
    async def get_text(self, url): self.urls.append(url); return self.responses.pop(0)


@pytest.mark.asyncio
async def test_pagination_and_overlapping_categories_are_deduplicated(monkeypatch):
    monkeypatch.setattr(module, "PAGE_SIZE", 4)
    http = FakeHttp([fixture("listing.html"), fixture("page2.html"), fixture("new_arrivals.html")])
    products = await HotTopicUSMonitor(http).discover_products()
    assert [p.retailer_product_id for p in products] == ["HT100", "HT200", "HT500"]
    assert "q=loungefly+mini+backpack&start=4&sz=4" in http.urls[1]
    assert "/backpacks-bags/new-arrivals/?start=0&sz=4" in http.urls[2]
    assert products[0].new_release and products[2].new_release


def test_exclusive_current_sale_price_original_and_promotion_are_not_confused():
    raw = HotTopicUSMonitor.parse_listing(fixture("listing.html"))[0]
    product = HotTopicUSMonitor(FakeHttp([])).parse_product(raw, source_url="https://www.hottopic.com/")
    assert product.price == Decimal("52.90")
    assert raw["offers"]["originalPrice"] == "89.90"
    assert raw["offers"]["promotionText"] == "30% off with code"
    assert product.exclusive and product.exclusive_retailer == "Hot Topic"
    assert product.sku == product.retailer_product_id == "HT100"


def test_stock_preorder_and_explicit_release_are_normalized():
    listing = HotTopicUSMonitor.parse_listing(fixture("listing.html"))
    preorder_raw = HotTopicUSMonitor.parse_listing(fixture("page2.html"))[0]
    monitor = HotTopicUSMonitor(FakeHttp([]))
    assert monitor.parse_product(listing[0], source_url="https://www.hottopic.com/").availability == Availability.IN_STOCK
    assert monitor.parse_product(listing[1], source_url="https://www.hottopic.com/").availability == Availability.OUT_OF_STOCK
    preorder = monitor.parse_product(preorder_raw, source_url="https://www.hottopic.com/")
    assert preorder.availability == Availability.PREORDER and preorder.preorder
    assert preorder.release.precision == ReleasePrecision.EXACT_DATETIME
    assert preorder.release.timezone == "America/Los_Angeles"


@pytest.mark.parametrize("name,brand", [
    ("Loungefly Mini Backpack Wallet", "LNGEFLY"),
    ("Loungefly Mini Backpack Bag Charm", "LNGEFLY"),
    ("Loungefly Mini Backpack Crossbody", "LNGEFLY"),
    ("Loungefly Mini Backpack Tote", "LNGEFLY"),
    ("Disney Character Mini Backpack", "Disney"),
])
def test_loungefly_filter_excludes_non_target_products(name, brand):
    assert not HotTopicUSMonitor._is_loungefly_mini_backpack({"name": name, "brand": {"name": brand}})


@pytest.mark.parametrize("html", ["", "<html>changed</html>", fixture("malformed.html")])
def test_malformed_pages_fail(html):
    with pytest.raises(BoxLunchParseError):
        HotTopicUSMonitor.parse_listing(html)


@pytest.mark.asyncio
async def test_product_parser_failure_returns_error_and_preserves_previous_state():
    raw = HotTopicUSMonitor.parse_listing(fixture("listing.html"))[0]
    original = HotTopicUSMonitor(FakeHttp([])).parse_product(raw, source_url="https://www.hottopic.com/")
    checked = await HotTopicUSMonitor(FakeHttp(["<html>changed</html>"])).check_product(original)
    assert checked.availability == Availability.ERROR
    assert checked.price == original.price and checked.exclusive_retailer == "Hot Topic"

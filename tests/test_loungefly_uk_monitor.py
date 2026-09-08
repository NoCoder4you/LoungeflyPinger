from pathlib import Path

import pytest

import app.monitors.loungefly_uk as loungefly_module
from app.models import Availability
from app.monitors.loungefly_uk import LoungeflyUKMonitor, LoungeflyUKParseError

FIXTURES = Path(__file__).parent / "fixtures" / "loungefly_uk"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    async def get_text(self, url):
        self.urls.append(url)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_discovery_filters_deduplicates_and_paginates(monkeypatch):
    monkeypatch.setattr(loungefly_module, "PAGE_SIZE", 4)
    http = FakeHttp([fixture("listing.html"), fixture("page2.html")])
    products = await LoungeflyUKMonitor(http).discover_products()

    assert [product.retailer_product_id for product in products] == ["WDBK1001", "WDBK1002", "HPBK2001"]
    assert "start=0&sz=4" in http.urls[0]
    assert "start=4&sz=4" in http.urls[1]


def test_product_parsing_normalizes_price_stock_image_and_exclusive():
    raw = LoungeflyUKMonitor.parse_listing(fixture("listing.html"))[0]
    product = LoungeflyUKMonitor(FakeHttp([])).parse_product(raw, source_url="https://loungefly.com/gb/")

    assert product.retailer == "Loungefly UK"
    assert product.price == 75
    assert product.currency == "GBP"
    assert product.availability == Availability.IN_STOCK
    assert product.image_url == "https://loungefly.com/images/stitch.png"
    assert product.product_type == "Mini Backpack"
    assert product.exclusive is True
    assert product.preorder is False


def test_preorder_and_description_exclusive_are_normalized():
    raw = LoungeflyUKMonitor.parse_product_page(fixture("preorder.html"))
    product = LoungeflyUKMonitor(FakeHttp([])).parse_product(raw, source_url=raw["@id"])
    assert product.availability == Availability.PREORDER
    assert product.preorder is True
    assert product.exclusive is True
    assert product.price == 85


def test_unpriced_out_of_stock_product_remains_out_of_stock_with_unknown_price():
    raw = LoungeflyUKMonitor.parse_listing(fixture("listing.html"))[1].copy()
    raw["offers"] = {**raw["offers"], "price": None, "priceCurrency": None}
    product = LoungeflyUKMonitor(FakeHttp([])).parse_product(raw, source_url="https://loungefly.com/gb/")
    assert product.price is None
    assert product.currency == "GBP"
    assert product.availability == Availability.OUT_OF_STOCK


@pytest.mark.parametrize("name", [
    "Loungefly Mini Backpack Wallet", "Mini Backpack Crossbody Bag", "Mini Backpack Pin Set",
    "Mini Backpack Accessory", "Mini Backpack T-Shirt", "Mini Backpack Purse", "Mini Backpack Cardholder",
])
def test_filter_excludes_non_backpack_products(name):
    assert not LoungeflyUKMonitor._is_mini_backpack({"name": name})


def test_filter_requires_mini_backpack_name():
    assert LoungeflyUKMonitor._is_mini_backpack({"name": "Star Wars Cosplay Mini Backpack"})
    assert not LoungeflyUKMonitor._is_mini_backpack({"name": "Star Wars Midi Backpack"})


@pytest.mark.parametrize("html", ["", "<html>changed</html>", fixture("malformed.html")])
def test_missing_or_malformed_structured_data_fails(html):
    with pytest.raises(LoungeflyUKParseError):
        LoungeflyUKMonitor.parse_listing(html)


def test_invalid_item_fails_entire_listing():
    html = '<script type="application/ld+json">{"@type":"ItemList","itemListElement":[{}]}</script>'
    with pytest.raises(LoungeflyUKParseError):
        LoungeflyUKMonitor.parse_listing(html)


@pytest.mark.parametrize("field", ["sku", "image", "offers"])
def test_missing_required_product_data_fails(field):
    raw = LoungeflyUKMonitor.parse_listing(fixture("listing.html"))[0].copy()
    raw.pop(field)
    with pytest.raises(LoungeflyUKParseError):
        LoungeflyUKMonitor(FakeHttp([])).parse_product(raw, source_url="https://loungefly.com/gb/")


@pytest.mark.asyncio
async def test_parser_failure_returns_error_not_out_of_stock():
    original = LoungeflyUKMonitor(FakeHttp([])).parse_product(
        LoungeflyUKMonitor.parse_listing(fixture("listing.html"))[0], source_url="https://loungefly.com/gb/"
    )
    checked = await LoungeflyUKMonitor(FakeHttp(["<html>changed</html>"])).check_product(original)
    assert checked.availability == Availability.ERROR

import json
from pathlib import Path

import pytest

from app.models import Availability, ReleasePrecision
from app.monitors.modern_pinup import ModernPinUpMonitor, ModernPinUpParseError

FIXTURES = Path(__file__).parent / "fixtures" / "modern_pinup"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    async def get_json(self, url):
        self.urls.append(url)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_discovery_filters_accessories_and_uses_structured_identity():
    monitor = ModernPinUpMonitor(FakeHttp([fixture("listing.json")]))
    products = await monitor.discover_products()
    assert [product.retailer_product_id for product in products] == ["101", "102"]
    assert products[0].url == "https://www.modernpinup.com/products/moon-mini-backpack"
    assert products[0].sku == "LF-MOON-01"
    assert products[0].image_url == "https://cdn.shopify.com/moon.jpg"
    assert products[0].new_release is True


def test_exclusive_preorder_sale_and_explicit_release_are_normalized():
    product = ModernPinUpMonitor(FakeHttp([])).parse_product(fixture("listing.json")["products"][0])
    assert product.exclusive is True
    assert product.exclusive_retailer == "Modern PinUp"
    assert product.availability == Availability.PREORDER and product.preorder
    assert str(product.price) == "72.00"
    assert str(product.original_price) == "90.00"
    assert product.currency == "USD"
    assert product.release.precision == ReleasePrecision.EXACT_DATETIME
    assert product.release.release_datetime.isoformat() == "2026-09-20T09:00:00-07:00"


def test_published_at_is_not_conflated_with_release_date():
    product = ModernPinUpMonitor(FakeHttp([])).parse_product(fixture("listing.json")["products"][1])
    assert product.release is None
    assert product.availability == Availability.OUT_OF_STOCK


def test_structured_coming_soon_tag_controls_availability():
    product = ModernPinUpMonitor(FakeHttp([])).parse_product(fixture("coming_soon.json"))
    assert product.availability == Availability.COMING_SOON


@pytest.mark.parametrize("payload", [{}, {"products": None}, {"products": [None]}])
def test_malformed_listing_is_rejected(payload):
    with pytest.raises(ModernPinUpParseError):
        ModernPinUpMonitor._product_list(payload)


@pytest.mark.parametrize("mutation", [
    lambda value: value.pop("variants"),
    lambda value: value.update(variants=[{"price": "85.00"}]),
    lambda value: value["variants"][0].update(price="not-money"),
    lambda value: value.update(published_at="not-a-date"),
])
def test_malformed_structured_product_is_rejected(mutation):
    value = fixture("coming_soon.json")
    mutation(value)
    with pytest.raises(ModernPinUpParseError):
        ModernPinUpMonitor(FakeHttp([])).parse_product(value)


@pytest.mark.asyncio
async def test_check_parse_failure_is_error_not_a_false_sellout():
    original = ModernPinUpMonitor(FakeHttp([])).parse_product(fixture("coming_soon.json"))
    checked = await ModernPinUpMonitor(FakeHttp([{"unexpected": True}])).check_product(original)
    assert checked.availability == Availability.ERROR

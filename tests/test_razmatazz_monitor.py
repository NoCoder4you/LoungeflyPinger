import json
from datetime import date, time
from decimal import Decimal
from pathlib import Path

import pytest

from app.models import Availability, ReleasePrecision
from app.monitors.razmatazz import RazmatazzMonitor, RazmatazzParseError

FIXTURE = Path(__file__).parent / "fixtures" / "razmatazz" / "products.json"


def products():
    return json.loads(FIXTURE.read_text())["products"]


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    async def get_json(self, url):
        self.urls.append(url)
        return self.responses.pop(0)


def parse(index):
    return RazmatazzMonitor(FakeHttp([])).parse_product(products()[index])


def test_structured_identity_stock_and_sale_price():
    product = parse(0)
    assert product.retailer == "Razmatazz UK"
    assert product.retailer_product_id == "201"
    assert product.variant_id == "2010" and product.sku == "LF-RH"
    assert product.availability == Availability.IN_STOCK
    assert product.price == Decimal("69.99")
    assert product.original_price == Decimal("79.99")
    assert product.currency == "GBP" and product.product_type == "Mini Backpack"


def test_preorder_and_release_information():
    product = parse(1)
    assert product.preorder and product.availability == Availability.PREORDER
    assert product.release is not None
    assert product.release.precision == ReleasePrecision.EXACT_DATETIME
    assert product.release.release_date == date(2026, 9, 20)
    assert product.release.release_time == time(9, 30)
    assert product.release.timezone == "Europe/London"


def test_out_of_stock():
    assert parse(2).availability == Availability.OUT_OF_STOCK


@pytest.mark.asyncio
async def test_discovery_uses_collection_and_filters_accessories_and_other_brands():
    http = FakeHttp([{"products": products()}])
    found = await RazmatazzMonitor(http).discover_products()
    assert [product.retailer_product_id for product in found] == ["201", "202", "203"]
    assert http.urls == [
        "https://www.razmatazz.co.uk/collections/loungefly/products.json?limit=250&page=1"
    ]


@pytest.mark.parametrize("payload", [{}, {"products": None}, {"products": [None]}])
def test_malformed_listing(payload):
    with pytest.raises(RazmatazzParseError):
        RazmatazzMonitor._product_list(payload)


@pytest.mark.parametrize("mutation", [
    lambda value: value.pop("variants"),
    lambda value: value.update(variants=[{"id": 1, "price": "79.99"}]),
    lambda value: value["variants"][0].update(price="invalid"),
    lambda value: value.update(published_at="invalid"),
])
def test_malformed_product(mutation):
    value = products()[0]
    mutation(value)
    with pytest.raises(RazmatazzParseError):
        RazmatazzMonitor(FakeHttp([])).parse_product(value)


@pytest.mark.asyncio
async def test_health_check_validates_shopify_shape():
    monitor = RazmatazzMonitor(FakeHttp([{"products": []}]))
    assert await monitor.health_check()


@pytest.mark.asyncio
async def test_product_js_integer_prices_are_converted_from_pence():
    raw = products()[0]
    raw["variants"][0].update(price=6999, compare_at_price=7999)
    original = parse(0)
    checked = await RazmatazzMonitor(FakeHttp([raw])).check_product(original)
    assert checked.price == Decimal("69.99")
    assert checked.original_price == Decimal("79.99")

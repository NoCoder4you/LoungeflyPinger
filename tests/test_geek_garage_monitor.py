import json
from datetime import date, time
from decimal import Decimal
from pathlib import Path

import pytest

from app.models import Availability, ReleasePrecision
from app.monitors.geek_garage import GeekGarageMonitor, GeekGarageParseError

FIXTURE = Path(__file__).parent / "fixtures" / "geek_garage" / "products.json"


def products():
    return json.loads(FIXTURE.read_text())["products"]


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    async def get_json(self, url):
        self.urls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def parse(index):
    return GeekGarageMonitor(FakeHttp([])).parse_product(products()[index])


def test_normal_product_and_structured_identity():
    product = parse(0)
    assert product.retailer == "Geek Garage"
    assert product.retailer_product_id == "101"
    assert product.variant_id == "1010" and product.sku == "LF-STITCH"
    assert product.price == Decimal("79.99") and product.currency == "GBP"
    assert product.product_type == "Mini Backpack"
    assert product.exclusive is False


def test_emea_exclusive_is_preserved_as_region_metadata():
    product = parse(1)
    assert product.exclusive is True
    assert product.exclusive_region == "EMEA"
    assert product.exclusive_retailer is None


def test_retailer_exclusive_requires_explicit_wording():
    product = parse(2)
    assert product.exclusive is True
    assert product.exclusive_retailer == "Geek Garage"
    assert product.exclusive_region is None


def test_in_stock_and_sale_price():
    product = parse(3)
    assert product.availability == Availability.IN_STOCK
    assert product.price == Decimal("59.99")
    assert product.original_price == Decimal("74.99")


def test_preorder_and_release_metadata():
    product = parse(4)
    assert product.preorder and product.availability == Availability.PREORDER
    assert product.release is not None
    assert product.release.precision == ReleasePrecision.EXACT_DATETIME
    assert product.release.release_date == date(2026, 9, 20)
    assert product.release.release_time == time(9, 30)
    assert product.release.timezone == "Europe/London"
    assert product.release.timezone_inferred


@pytest.mark.asyncio
async def test_discovery_filters_accessories_and_uses_shopify_brand_metadata():
    payload = {"products": products()}
    monitor = GeekGarageMonitor(FakeHttp([payload]))
    found = await monitor.discover_products()
    assert [product.retailer_product_id for product in found] == ["101", "102", "103", "104", "105"]


@pytest.mark.parametrize("payload", [{}, {"products": None}, {"products": [None]}])
def test_malformed_listing_data(payload):
    with pytest.raises(GeekGarageParseError):
        GeekGarageMonitor._product_list(payload)


@pytest.mark.parametrize("mutation", [
    lambda value: value.pop("variants"),
    lambda value: value.update(variants=[{"id": 1, "price": "79.99"}]),
    lambda value: value["variants"][0].update(price="not-money"),
    lambda value: value.update(published_at="not-a-date"),
])
def test_malformed_product_data(mutation):
    value = products()[0]
    mutation(value)
    with pytest.raises(GeekGarageParseError):
        GeekGarageMonitor(FakeHttp([])).parse_product(value)


@pytest.mark.asyncio
async def test_parser_failure_is_error_not_out_of_stock():
    original = parse(0)
    checked = await GeekGarageMonitor(FakeHttp([{"unexpected": True}])).check_product(original)
    assert checked.availability == Availability.ERROR
    assert checked.price == original.price

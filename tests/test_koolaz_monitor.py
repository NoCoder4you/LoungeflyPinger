import json
from datetime import date, time
from decimal import Decimal
from pathlib import Path

import pytest

from app.models import Availability, ReleasePrecision
from app.monitors.koolaz import KoolazMonitor, KoolazParseError

FIXTURE = Path(__file__).parent / "fixtures" / "koolaz" / "products.json"


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
    return KoolazMonitor(FakeHttp([])).parse_product(products()[index])


def test_in_stock_gbp_mini_backpack_and_product_identity():
    product = parse(0)
    assert product.retailer == "Koolaz UK"
    assert product.retailer_product_id == "9001"
    assert product.variant_id == "9101" and product.sku == "LF-CAS"
    assert product.availability == Availability.IN_STOCK
    assert product.price == Decimal("74.99") and product.currency == "GBP"
    assert product.product_type == "Mini Backpack" and product.new_release


def test_explicit_sold_out_normalizes_to_out_of_stock():
    product = parse(1)
    assert product.availability == Availability.OUT_OF_STOCK
    assert product.original_price is None


def test_sale_and_compare_at_price_do_not_change_stock_status():
    product = parse(2)
    assert product.price == Decimal("49.99")
    assert product.original_price == Decimal("74.99")
    assert product.availability == Availability.IN_STOCK
    raw = products()[2]
    raw["variants"][0]["available"] = False
    sold_out_sale = KoolazMonitor(FakeHttp([])).parse_product(raw)
    assert sold_out_sale.original_price == Decimal("74.99")
    assert sold_out_sale.availability == Availability.OUT_OF_STOCK


def test_equal_compare_at_price_is_not_normalized_as_sale():
    assert parse(3).original_price is None


def test_preorder_and_release_metadata():
    product = parse(3)
    assert product.preorder and product.availability == Availability.PREORDER
    assert product.release is not None
    assert product.release.precision == ReleasePrecision.EXACT_DATETIME
    assert product.release.release_date == date(2026, 9, 20)
    assert product.release.release_time == time(9, 30)
    assert product.release.timezone == "Europe/London"
    assert product.release.timezone_inferred


@pytest.mark.asyncio
async def test_discovery_uses_collection_and_vendor_metadata_and_excludes_non_backpacks():
    monitor = KoolazMonitor(FakeHttp([{"products": products()}]))
    found = await monitor.discover_products()
    assert [product.retailer_product_id for product in found] == ["9001", "9002", "9003", "9004"]
    assert monitor.http.urls == [
        "https://koolaz.co.uk/collections/loungefly/products.json?limit=250&page=1"
    ]


@pytest.mark.parametrize("payload", [{}, {"products": None}, {"products": [None]}])
def test_malformed_listing_is_parser_failure(payload):
    with pytest.raises(KoolazParseError):
        KoolazMonitor._product_list(payload)


@pytest.mark.parametrize("mutation", [
    lambda value: value.pop("id"),
    lambda value: value.update(variants=[]),
    lambda value: value["variants"][0].pop("available"),
    lambda value: value["variants"][0].update(price="not-money"),
    lambda value: value.update(published_at="not-a-date"),
])
def test_malformed_product_is_parser_failure(mutation):
    value = products()[0]
    mutation(value)
    with pytest.raises(KoolazParseError):
        KoolazMonitor(FakeHttp([])).parse_product(value)


@pytest.mark.asyncio
async def test_parser_failure_is_error_not_sold_out_transition():
    original = parse(0)
    checked = await KoolazMonitor(FakeHttp([{"unexpected": True}])).check_product(original)
    assert checked.availability == Availability.ERROR
    assert checked.price == original.price

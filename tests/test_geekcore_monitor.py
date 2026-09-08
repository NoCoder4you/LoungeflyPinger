import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.models import Availability, Product
from app.monitors.geekcore import GeekCoreMonitor, GeekCoreParseError

FIXTURES = Path(__file__).parent / "fixtures" / "geekcore"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


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


@pytest.mark.asyncio
async def test_discovery_filters_non_backpacks_and_duplicates():
    http = FakeHttp([fixture("product_listing.json"), {"products": []}])
    products = await GeekCoreMonitor(http).discover_products()
    assert len(products) == 1
    product = products[0]
    assert product.name == "Loungefly x Disney Stitch Cosplay Mini Backpack"
    assert product.url == "https://www.geekcore.co.uk/products/loungefly-stitch-mini-backpack"
    assert product.retailer_product_id == "123456789"
    assert product.image_url == "https://cdn.shopify.com/stitch.jpg"
    assert str(product.price) == "74.99"
    assert product.currency == "GBP"
    assert product.franchise == "Lilo and Stitch"
    assert product.character == "Stitch"
    assert product.exclusive is True


@pytest.mark.parametrize(
    ("name", "availability", "preorder"),
    [("in_stock.json", Availability.IN_STOCK, False),
     ("out_of_stock.json", Availability.OUT_OF_STOCK, False),
     ("preorder.json", Availability.PREORDER, True)],
)
def test_stock_and_preorder_parsing(name, availability, preorder):
    product = GeekCoreMonitor(FakeHttp([])).parse_product(fixture(name))
    assert product.availability == availability
    assert product.preorder is preorder


@pytest.mark.parametrize("payload", [{}, {"products": None}, {"products": [None]}])
def test_changed_or_missing_feed_shape_is_error(payload):
    with pytest.raises(GeekCoreParseError):
        GeekCoreMonitor._product_list(payload)


@pytest.mark.parametrize("mutation", [
    lambda value: value.pop("variants"),
    lambda value: value.update(variants=[{"price": "74.99"}]),
    lambda value: value["variants"][0].pop("price"),
])
def test_malformed_product_is_parser_error(mutation):
    value = fixture("in_stock.json")
    mutation(value)
    with pytest.raises(GeekCoreParseError):
        GeekCoreMonitor(FakeHttp([])).parse_product(value)


@pytest.mark.asyncio
async def test_check_parser_failure_is_error_not_out_of_stock():
    original = GeekCoreMonitor(FakeHttp([])).parse_product(fixture("in_stock.json"))
    checked = await GeekCoreMonitor(FakeHttp([{"unexpected": True}])).check_product(original)
    assert checked.availability == Availability.ERROR

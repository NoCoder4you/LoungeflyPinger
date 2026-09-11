import json
from pathlib import Path

import pytest

from app.models import Availability, ReleasePrecision
from app.monitors.pink_a_la_mode import PinkALaModeMonitor, PinkALaModeParseError

FIXTURES = Path(__file__).parent / "fixtures" / "pink_a_la_mode"


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
async def test_discovery_filters_merchandise_marks_new_and_suppresses_duplicates():
    monitor = PinkALaModeMonitor(FakeHttp([
        fixture("mini_backpacks.json"), fixture("new_arrivals.json")
    ]))
    products = await monitor.discover_products()
    assert [product.retailer_product_id for product in products] == ["201", "202"]
    assert products[0].url == "https://pinkalamode.com/products/haunted-castle-mini-backpack"
    assert products[1].new_release is True
    assert products[1].sku == "LF-STARS"


def test_exclusive_preorder_price_sku_and_explicit_release_are_normalized():
    raw = fixture("mini_backpacks.json")["products"][0]
    product = PinkALaModeMonitor(FakeHttp([])).parse_product(raw)
    assert product.exclusive is True
    assert product.exclusive_retailer == "Pink a la Mode"
    assert product.availability == Availability.PREORDER and product.preorder
    assert str(product.price) == "79.99"
    assert str(product.original_price) == "89.99"
    assert product.currency == "USD"
    assert product.retailer_product_id == "201"
    assert product.sku == "PALM-CASTLE"
    assert product.release.precision == ReleasePrecision.EXACT_DATETIME
    assert product.release.release_datetime.isoformat() == "2026-09-20T09:00:00-07:00"


def test_marketing_publication_is_not_treated_as_product_release():
    raw = fixture("mini_backpacks.json")["products"][1]
    raw["body_html"] = "<p>Our launch news was published September 10, 2026.</p>"
    product = PinkALaModeMonitor(FakeHttp([])).parse_product(raw)
    assert raw["published_at"] == "2026-09-10T12:00:00-07:00"
    assert product.release is None


@pytest.mark.asyncio
async def test_stock_and_price_refresh_uses_structured_collection_product():
    original = PinkALaModeMonitor(FakeHttp([])).parse_product(
        fixture("mini_backpacks.json")["products"][1]
    )
    checked = await PinkALaModeMonitor(
        FakeHttp([{"products": [fixture("in_stock.json")]}])
    ).check_product(original)
    assert checked.availability == Availability.IN_STOCK
    assert str(checked.price) == "82.00"
    assert checked.new_release is False


@pytest.mark.asyncio
async def test_missing_product_is_unavailable():
    product = PinkALaModeMonitor(FakeHttp([])).parse_product(
        fixture("mini_backpacks.json")["products"][1]
    )
    checked = await PinkALaModeMonitor(FakeHttp([{"products": []}])).check_product(product)
    assert checked.availability == Availability.UNAVAILABLE


@pytest.mark.parametrize("payload", [{}, {"products": None}, {"products": [None]}])
def test_malformed_listing_is_rejected(payload):
    with pytest.raises(PinkALaModeParseError):
        PinkALaModeMonitor._product_list(payload)


@pytest.mark.parametrize("mutation", [
    lambda value: value.pop("variants"),
    lambda value: value.update(variants=[{"price": "85.00"}]),
    lambda value: value["variants"][0].update(price="not-money"),
    lambda value: value.update(published_at="not-a-date"),
])
def test_malformed_structured_product_is_rejected(mutation):
    value = fixture("in_stock.json")
    mutation(value)
    with pytest.raises(PinkALaModeParseError):
        PinkALaModeMonitor(FakeHttp([])).parse_product(value)


@pytest.mark.asyncio
async def test_check_parse_failure_is_error_not_false_stock_change():
    original = PinkALaModeMonitor(FakeHttp([])).parse_product(fixture("in_stock.json"))
    checked = await PinkALaModeMonitor(
        FakeHttp([{"products": [{"id": 202}]}])
    ).check_product(original)
    assert checked.availability == Availability.ERROR

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from app.http import HttpClientError, HttpErrorKind
from app.models import Availability, ReleasePrecision
from app.monitors.get_ready_comics import GetReadyComicsMonitor, GetReadyComicsParseError

FIXTURE = Path(__file__).parent / "fixtures" / "get_ready_comics" / "products.json"


def products():
    return json.loads(FIXTURE.read_text())


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


def parse(product_id):
    raw = next(item for item in products() if item["id"] == product_id)
    return GetReadyComicsMonitor(FakeHttp([])).parse_product(raw)


def test_in_stock_product_identity_and_gbp_minor_units():
    product = parse(267825)
    assert product.retailer == "Get Ready Comics UK"
    assert product.retailer_product_id == "267825" and product.sku == "MYBK0002"
    assert product.name == "Book Of The Living The Mummy Loungefly Mini Backpack"
    assert product.price == Decimal("79.99") and product.currency == "GBP"
    assert product.availability == Availability.IN_STOCK
    assert product.product_type == "Mini Backpack"


def test_out_of_stock_product_and_html_entities():
    product = parse(208422)
    assert "&" in product.name and "&#038;" not in product.name
    assert product.availability == Availability.COMING_SOON
    assert product.release is not None
    assert product.release.precision == ReleasePrecision.COMING_SOON


def test_preorder_is_open_only_while_stock_is_available():
    available = parse(284398)
    unavailable = parse(285817)
    assert available.preorder and available.availability == Availability.PREORDER
    assert unavailable.preorder and unavailable.availability == Availability.OUT_OF_STOCK


def test_convertible_full_size_backpack_is_filtered_out():
    raw = next(item for item in products() if item["id"] == 196931)
    assert not GetReadyComicsMonitor._is_loungefly_mini_backpack(raw)


@pytest.mark.asyncio
async def test_discovery_filters_and_paginates_until_short_page():
    first = products()
    first.extend(deepcopy(first[0]) for _ in range(95))
    monitor = GetReadyComicsMonitor(FakeHttp([first, products()[:2]]))
    found = await monitor.discover_products()
    assert {product.retailer_product_id for product in found} == {
        "208422", "267825", "285817", "284398"
    }
    assert "page=1" in monitor.http.urls[0] and "page=2" in monitor.http.urls[1]


@pytest.mark.asyncio
async def test_targeted_check_uses_public_store_product_endpoint():
    original = parse(267825)
    raw = next(item for item in products() if item["id"] == 267825)
    raw["is_in_stock"] = False
    monitor = GetReadyComicsMonitor(FakeHttp([raw]))
    checked = await monitor.check_product(original)
    assert checked.availability == Availability.OUT_OF_STOCK
    assert monitor.http.urls == [
        "https://getreadycomics.com/wp-json/wc/store/v1/products/267825"
    ]


@pytest.mark.asyncio
async def test_missing_product_is_unavailable_and_bad_data_is_error():
    original = parse(267825)
    missing = HttpClientError(HttpErrorKind.NOT_FOUND, "missing", 404)
    assert (await GetReadyComicsMonitor(FakeHttp([missing])).check_product(original)).availability == Availability.UNAVAILABLE
    assert (await GetReadyComicsMonitor(FakeHttp([{}])).check_product(original)).availability == Availability.ERROR


@pytest.mark.parametrize("payload", [{}, None, [None]])
def test_malformed_listing_is_rejected(payload):
    with pytest.raises(GetReadyComicsParseError):
        GetReadyComicsMonitor._product_list(payload)


@pytest.mark.parametrize("mutation", [
    lambda value: value.pop("prices"),
    lambda value: value["prices"].update(currency_code="USD"),
    lambda value: value.update(is_in_stock="yes"),
    lambda value: value.update(permalink="https://example.com/shop/not-ours/"),
])
def test_malformed_product_is_rejected(mutation):
    value = deepcopy(products()[1])
    mutation(value)
    with pytest.raises(GetReadyComicsParseError):
        GetReadyComicsMonitor(FakeHttp([])).parse_product(value)

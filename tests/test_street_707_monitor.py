import copy
import json
from pathlib import Path

import pytest

from app.http import HttpClientError, HttpErrorKind
from app.models import Availability, ReleasePrecision
from app.monitors.street_707 import Street707Monitor, Street707ParseError

FIXTURES = Path(__file__).parent / "fixtures" / "street_707"


def fixture():
    return json.loads((FIXTURES / "product.json").read_text())


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


def test_structured_product_fields_availability_sku_tags_and_release():
    product = Street707Monitor(FakeHttp([])).parse_product(fixture())
    assert product.retailer_product_id == "9001"
    assert product.variant_id == "9102"
    assert product.sku == "671803000001" and product.barcode == "671803000001"
    assert product.vendor == "Loungefly"
    assert product.url == "https://707street.com/products/loungefly-disney-moonlight-mini-backpack"
    assert product.image_url == "https://cdn.shopify.com/moonlight.jpg"
    assert str(product.price) == "82.00" and str(product.original_price) == "90.00"
    assert product.availability == Availability.PREORDER and product.preorder is True
    assert product.tags == ("Backpack", "Loungefly", "Exclusive", "LFSEPT26")
    assert product.listing_published_at.isoformat() == "2026-08-01T12:30:00-07:00"
    assert product.release.precision == ReleasePrecision.EXACT_DATETIME
    assert product.release.release_datetime.isoformat() == "2026-09-20T09:00:00-07:00"


def test_publication_and_wave_tag_are_not_release_dates():
    raw = fixture()
    raw["body_html"] = "<p>A new design.</p>"
    product = Street707Monitor(FakeHttp([])).parse_product(raw)
    assert product.listing_published_at is not None
    assert product.release is None


def test_duplicate_variant_ids_are_collapsed_deterministically():
    raw = fixture()
    raw["variants"].insert(0, copy.deepcopy(raw["variants"][1]))
    product = Street707Monitor(FakeHttp([])).parse_product(raw)
    assert product.variant_id == "9102"
    assert product.sku == "671803000001"


@pytest.mark.parametrize("available, expected", [(True, Availability.IN_STOCK),
                                                   (False, Availability.OUT_OF_STOCK)])
def test_shopify_boolean_controls_availability(available, expected):
    raw = fixture()
    raw["body_html"] = ""
    for variant in raw["variants"]:
        variant["available"] = available
    assert Street707Monitor(FakeHttp([])).parse_product(raw).availability == expected


@pytest.mark.parametrize("mutation", [
    lambda raw: raw.update(variants=[{"id": 1, "available": "true", "price": "90"}]),
    lambda raw: raw.update(published_at="yesterday"),
    lambda raw: raw.update(tags={"not": "a list"}),
    lambda raw: raw["variants"][1].update(price="not-money"),
])
def test_malformed_structured_json_is_rejected(mutation):
    raw = fixture()
    mutation(raw)
    with pytest.raises(Street707ParseError):
        Street707Monitor(FakeHttp([])).parse_product(raw)


@pytest.mark.asyncio
async def test_discovery_filters_full_size_and_non_loungefly_and_deduplicates_products():
    good = fixture()
    full_size = {**fixture(), "id": 2, "title": "Loungefly Full-Size Backpack"}
    other = {**fixture(), "id": 3, "vendor": "Other Brand"}
    monitor = Street707Monitor(FakeHttp([{"products": [good, full_size, other, good]}]))
    products = await monitor.discover_products()
    assert [product.retailer_product_id for product in products] == ["9001"]


@pytest.mark.asyncio
async def test_product_removal_is_unavailable():
    original = Street707Monitor(FakeHttp([])).parse_product(fixture())
    missing = HttpClientError(HttpErrorKind.NOT_FOUND, "gone", 404)
    checked = await Street707Monitor(FakeHttp([missing])).check_product(original)
    assert checked.availability == Availability.UNAVAILABLE


@pytest.mark.asyncio
async def test_malformed_json_response_is_error_not_false_removal():
    original = Street707Monitor(FakeHttp([])).parse_product(fixture())
    malformed = HttpClientError(HttpErrorKind.PARSER, "invalid JSON")
    checked = await Street707Monitor(FakeHttp([malformed])).check_product(original)
    assert checked.availability == Availability.ERROR

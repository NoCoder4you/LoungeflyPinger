from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from app.models import Availability, ReleasePrecision
from app.monitors.cool_merch import CoolMerchMonitor, CoolMerchParseError


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


def product(**changes):
    value = {
        "id": 15050462626168,
        "title": "LOUNGEFLY : DISNEY - Stitch Cosplay Mini Backpack",
        "handle": "loungefly-stitch-cosplay-mini-backpack",
        "product_type": "Bags",
        "vendor": "Funko",
        "tags": ["Bags", "Disney", "Loungefly", "Loungefly August 2026"],
        "body_html": "<p>Official Loungefly bag.</p>",
        "published_at": "2026-08-28T14:10:14+01:00",
        "images": [{"src": "//cdn.shopify.com/stitch.jpg"}],
        "variants": [{
            "id": 55643327267192, "sku": "LF-STITCH", "barcode": "1234567890123",
            "price": "75.00", "compare_at_price": None, "available": True,
        }],
    }
    value.update(changes)
    return value


@pytest.mark.parametrize(("candidate", "expected"), [
    (product(), True),
    (product(title="LOUNGEFLY - Mystery Mini Backpack Keychain Charm",
             product_type="Blind Box/Bag Product", tags=["Loungefly", "Bag Charm"]), False),
    (product(title="LOUNGEFLY - Stitch Mini Backpack Bag Charm",
             product_type="Bags", tags=["Bags", "Loungefly", "Bag Charm"]), False),
    (product(title="LOUNGEFLY - Stitch Crossbody Bag"), False),
    (product(title="LOUNGEFLY - Stitch Mini Backpack", product_type="Wallets & Purses",
             tags=["Loungefly", "Wallets & Purses"]), False),
])
def test_careful_product_classification(candidate, expected):
    assert CoolMerchMonitor._is_loungefly_mini_backpack(candidate) is expected


@pytest.mark.asyncio
async def test_discovery_reports_new_canonical_products_and_deduplicates():
    duplicate = product(title="Updated title")
    http = FakeHttp([{"products": [product(), duplicate]}])
    products = await CoolMerchMonitor(http).discover_products()
    assert len(products) == 1
    assert products[0].retailer_product_id == "15050462626168"
    assert products[0].variant_id == "55643327267192"
    assert products[0].currency == "GBP"


@pytest.mark.parametrize(("available", "expected"), [
    (True, Availability.IN_STOCK),
    (False, Availability.OUT_OF_STOCK),
])
def test_stock(available, expected):
    raw = product()
    raw["variants"][0]["available"] = available
    assert CoolMerchMonitor(FakeHttp([])).parse_product(raw).availability == expected


def test_price_and_sale_price():
    raw = product()
    raw["variants"][0].update(price="55.00", compare_at_price="75.00")
    parsed = CoolMerchMonitor(FakeHttp([])).parse_product(raw)
    assert parsed.price == Decimal("55.00")
    assert parsed.original_price == Decimal("75.00")


def test_preorder_and_explicit_exclusive():
    raw = product(body_html="<p>Pre-order now. A Cool-Merch exclusive.</p>")
    parsed = CoolMerchMonitor(FakeHttp([])).parse_product(raw)
    assert parsed.preorder is True
    assert parsed.availability == Availability.PREORDER
    assert parsed.exclusive is True
    assert parsed.exclusive_retailer == "Cool-Merch UK"


def test_release_information_does_not_use_publication_timestamp():
    raw = product(body_html="<p>Available from 15 September 2026.</p>")
    parsed = CoolMerchMonitor(FakeHttp([])).parse_product(raw)
    assert parsed.release is not None
    assert parsed.release.precision == ReleasePrecision.DATE_ONLY
    assert parsed.release.release_date == date(2026, 9, 15)
    assert parsed.listing_published_at.isoformat() == "2026-08-28T14:10:14+01:00"
    without_release = CoolMerchMonitor(FakeHttp([])).parse_product(product())
    assert without_release.release is None


@pytest.mark.parametrize("mutation", [
    lambda value: value.pop("variants"),
    lambda value: value.update(variants=[{"price": "75.00"}]),
    lambda value: value["variants"][0].pop("price"),
    lambda value: value.update(published_at="not-a-date"),
])
def test_parser_failure(mutation):
    raw = deepcopy(product())
    mutation(raw)
    with pytest.raises(CoolMerchParseError):
        CoolMerchMonitor(FakeHttp([])).parse_product(raw)


@pytest.mark.asyncio
async def test_check_parser_failure_is_error_not_out_of_stock():
    original = CoolMerchMonitor(FakeHttp([])).parse_product(product())
    checked = await CoolMerchMonitor(FakeHttp([{"unexpected": True}])).check_product(original)
    assert checked.availability == Availability.ERROR

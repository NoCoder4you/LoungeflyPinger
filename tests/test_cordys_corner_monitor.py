import copy
from decimal import Decimal

import pytest

from app.http import HttpClientError, HttpErrorKind
from app.models import Availability, ReleasePrecision
from app.monitors.cordys_corner import CordysCornerMonitor, CordysCornerParseError


def product(**changes):
    raw = {
        "id": 8820157120572,
        "title": "Loungefly Disney Mulan Stained Glass Mini Backpack Shop Exclusive",
        "handle": "loungefly-mulan-stained-glass-mini-backpack",
        "body_html": "<p>Pre-order now. Releases September 20, 2026 at 09:00.</p>",
        "published_at": "2026-01-27T11:57:42-06:00",
        "vendor": "Loungefly",
        "product_type": "Backpack",
        "tags": ["Backpack", "Loungefly", "Loungefly mini backpack", "Shop Exclusives", "new-arrival"],
        "variants": [{"id": 445, "sku": "LF-MULAN", "available": True,
                      "price": "75.00", "compare_at_price": "90.00"}],
        "images": [{"src": "//cdn.shopify.com/mulan.jpg"}],
    }
    raw.update(changes)
    return raw


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


def test_product_identity_and_exclusive_metadata_are_normalized():
    parsed = CordysCornerMonitor(FakeHttp([])).parse_product(product())
    assert parsed.retailer == "Cordy's Corner"
    assert parsed.retailer_product_id == "8820157120572"
    assert parsed.variant_id == "445" and parsed.sku == "LF-MULAN"
    assert parsed.url == "https://cordyscorner.com/products/loungefly-mulan-stained-glass-mini-backpack"
    assert parsed.product_type == "Mini Backpack"
    assert parsed.exclusive is True and parsed.exclusive_retailer == "Cordy's Corner"
    assert parsed.new_release is True


@pytest.mark.parametrize("available, expected", [
    (True, Availability.IN_STOCK), (False, Availability.OUT_OF_STOCK),
])
def test_shopify_stock_boolean_controls_stock(available, expected):
    raw = product(body_html="")
    raw["variants"][0]["available"] = available
    assert CordysCornerMonitor(FakeHttp([])).parse_product(raw).availability == expected


def test_preorder_overrides_stock_without_confusing_clearance_for_release_state():
    parsed = CordysCornerMonitor(FakeHttp([])).parse_product(product(tags=["Clearance"]))
    assert parsed.preorder is True and parsed.availability == Availability.PREORDER
    sale = CordysCornerMonitor(FakeHttp([])).parse_product(
        product(body_html="", tags=["Clearance"])
    )
    assert sale.availability == Availability.IN_STOCK and sale.preorder is False


def test_price_and_sale_price_are_normalized_from_active_variant():
    parsed = CordysCornerMonitor(FakeHttp([])).parse_product(product())
    assert parsed.price == Decimal("75.00")
    assert parsed.original_price == Decimal("90.00")


def test_release_data_is_explicit_and_publication_remains_listing_metadata():
    parsed = CordysCornerMonitor(FakeHttp([])).parse_product(product())
    assert parsed.listing_published_at.isoformat() == "2026-01-27T11:57:42-06:00"
    assert parsed.release.precision == ReleasePrecision.EXACT_DATETIME
    assert parsed.release.release_datetime.isoformat() == "2026-09-20T09:00:00-05:00"
    no_release = CordysCornerMonitor(FakeHttp([])).parse_product(
        product(body_html="Limited edition release of only 1000 pieces.")
    )
    assert no_release.listing_published_at is not None and no_release.release is None


@pytest.mark.parametrize("title, vendor, kind", [
    ("Loungefly Full-Size Backpack", "Loungefly", "Backpack"),
    ("Loungefly Mystery Mini Backpack Bundle", "Loungefly", "Backpack"),
    ("Loungefly Minnie Wallet", "Loungefly", "Wallet"),
    ("Loungefly Minnie Crossbody", "Loungefly", "Crossbody"),
    ("Loungefly Minnie Mini Backpack Hoodie", "Loungefly", "Backpack"),
    ("Other Minnie Mini Backpack", "Other", "Backpack"),
])
def test_mini_backpack_filter_excludes_unwanted_merchandise(title, vendor, kind):
    raw = product(title=title, vendor=vendor, product_type=kind)
    assert CordysCornerMonitor._is_loungefly_mini_backpack(raw) is False
    assert CordysCornerMonitor._is_loungefly_mini_backpack(product()) is True


@pytest.mark.asyncio
async def test_discovery_reads_both_dedicated_collections_and_deduplicates_overlap():
    plain = product(tags=["Loungefly"], title="Loungefly Mulan Mini Backpack")
    exclusive = copy.deepcopy(plain)
    excluded = product(id=99, title="Loungefly Mystery Mini Backpack Bundle")
    http = FakeHttp([{"products": [plain, excluded]}, {"products": [exclusive]}])
    found = await CordysCornerMonitor(http).discover_products()
    assert len(found) == 1 and found[0].exclusive is True
    assert found[0].exclusive_retailer == "Cordy's Corner"
    assert "/collections/loungefly-backpack/products.json" in http.urls[0]
    assert "/collections/shop-exclusive-loungefly/products.json" in http.urls[1]


@pytest.mark.asyncio
async def test_exclusive_collection_marks_product_without_title_or_tag_hint():
    raw = product(title="Loungefly Mulan Mini Backpack", tags=["Loungefly"])
    found = await CordysCornerMonitor(FakeHttp([
        {"products": []}, {"products": [raw]},
    ])).discover_products()
    assert found[0].exclusive and found[0].exclusive_retailer == "Cordy's Corner"


@pytest.mark.parametrize("mutation", [
    lambda raw: raw.pop("id"),
    lambda raw: raw.update(tags={"bad": "tags"}),
    lambda raw: raw.update(published_at="yesterday"),
    lambda raw: raw.update(variants=[{"id": 1, "available": "yes", "price": "75"}]),
    lambda raw: raw["variants"][0].update(price="not-money"),
    lambda raw: raw["variants"][0].pop("id"),
])
def test_parser_failure_rejects_malformed_structured_data(mutation):
    raw = product()
    mutation(raw)
    with pytest.raises(CordysCornerParseError):
        CordysCornerMonitor(FakeHttp([])).parse_product(raw)


@pytest.mark.asyncio
async def test_product_parser_failure_is_error_not_false_stock_or_removal():
    original = CordysCornerMonitor(FakeHttp([])).parse_product(product())
    error = HttpClientError(HttpErrorKind.PARSER, "invalid JSON")
    checked = await CordysCornerMonitor(FakeHttp([error])).check_product(original)
    assert checked.availability == Availability.ERROR

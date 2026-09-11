import copy
from decimal import Decimal

import pytest

from app.http import HttpClientError, HttpErrorKind
from app.models import Availability, ReleasePrecision
from app.monitors.infinity_collectables import (
    PAGE_SIZE, InfinityCollectablesMonitor, InfinityCollectablesParseError,
)


def product(**changes):
    raw = {
        "id": 15911571390848,
        "title": "Loungefly Disney Haunted Mansion Mini Backpack",
        "handle": "loungefly-disney-haunted-mansion-mini-backpack",
        "body_html": "<p>Pre-order now. Releases 20 September 2026 at 9:00 am.</p>",
        "published_at": "2026-08-25T19:38:47+01:00",
        "vendor": "Abgee",
        "product_type": "Collectible",
        "tags": ["Loungefly", "New Arrivals"],
        "variants": [{"id": 58754340159872, "sku": "986 HM001", "available": True,
                      "price": "64.99", "compare_at_price": "79.99"}],
        "images": [{"src": "//cdn.shopify.com/haunted.jpg"}],
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


def test_identity_sku_sale_stock_preorder_and_release_are_normalized():
    parsed = InfinityCollectablesMonitor(FakeHttp([])).parse_product(product())
    assert parsed.retailer == "Infinity Collectables"
    assert parsed.retailer_product_id == "15911571390848"
    assert parsed.variant_id == "58754340159872" and parsed.sku == "986 HM001"
    assert parsed.currency == "GBP"
    assert parsed.price == Decimal("64.99") and parsed.original_price == Decimal("79.99")
    assert parsed.preorder and parsed.availability == Availability.PREORDER
    assert parsed.release.precision == ReleasePrecision.EXACT_DATETIME
    assert parsed.release.release_datetime.isoformat() == "2026-09-20T09:00:00+01:00"


def test_listing_timestamp_is_not_treated_as_release_date():
    parsed = InfinityCollectablesMonitor(FakeHttp([])).parse_product(
        product(body_html="<p>Officially licensed Loungefly merchandise.</p>")
    )
    assert parsed.listing_published_at.isoformat() == "2026-08-25T19:38:47+01:00"
    assert parsed.release is None


@pytest.mark.parametrize("available, body, expected", [
    (True, "", Availability.IN_STOCK),
    (False, "", Availability.OUT_OF_STOCK),
    (False, "Coming soon", Availability.COMING_SOON),
])
def test_structured_stock_and_explicit_coming_soon(available, body, expected):
    raw = product(body_html=body)
    raw["variants"][0]["available"] = available
    assert InfinityCollectablesMonitor(FakeHttp([])).parse_product(raw).availability == expected


@pytest.mark.parametrize("title", [
    "Loungefly Minnie Wallet",
    "Loungefly Minnie Card Holder",
    "Loungefly Minnie Crossbody Bag",
    "Loungefly Minnie Tote Bag",
    "Loungefly Minnie Enamel Pin",
    "Loungefly Mystery Mini Backpack Keychain",
    "Loungefly Minnie Full Size Backpack",
    "Other Brand Minnie Mini Backpack",
])
def test_strict_mini_backpack_classification_excludes_other_products(title):
    assert not InfinityCollectablesMonitor._is_loungefly_mini_backpack(product(title=title))
    assert InfinityCollectablesMonitor._is_loungefly_mini_backpack(product())


@pytest.mark.asyncio
async def test_discovery_paginates_filters_and_deduplicates_by_product_id():
    wanted = product()
    duplicate = copy.deepcopy(wanted)
    duplicate["title"] = "Loungefly Disney Haunted Mansion Mini Backpack Updated"
    filler = product(id=2, title="Loungefly Minnie Wallet")
    first_page = [filler] * PAGE_SIZE
    first_page[3] = wanted
    http = FakeHttp([{"products": first_page}, {"products": [duplicate]}])
    found = await InfinityCollectablesMonitor(http).discover_products()
    assert len(found) == 1 and found[0].name.endswith("Updated")
    assert "limit=250&page=1" in http.urls[0]
    assert "limit=250&page=2" in http.urls[1]


@pytest.mark.parametrize("mutation", [
    lambda raw: raw.pop("id"),
    lambda raw: raw.update(tags={"invalid": True}),
    lambda raw: raw.update(published_at="recently"),
    lambda raw: raw.update(variants=[]),
    lambda raw: raw.update(variants=[{"id": 1, "available": "yes", "price": "10"}]),
    lambda raw: raw["variants"][0].update(price="invalid"),
    lambda raw: raw["variants"][0].pop("id"),
])
def test_malformed_structured_data_is_rejected(mutation):
    raw = product()
    mutation(raw)
    with pytest.raises(InfinityCollectablesParseError):
        InfinityCollectablesMonitor(FakeHttp([])).parse_product(raw)


@pytest.mark.asyncio
async def test_parser_failure_returns_error_and_preserves_previous_fields():
    original = InfinityCollectablesMonitor(FakeHttp([])).parse_product(product(body_html=""))
    error = HttpClientError(HttpErrorKind.PARSER, "malformed JSON")
    checked = await InfinityCollectablesMonitor(FakeHttp([error])).check_product(original)
    assert checked.availability == Availability.ERROR
    assert checked.price == original.price and checked.sku == original.sku


def test_explicit_exclusive_is_recorded_but_not_inferred():
    ordinary = InfinityCollectablesMonitor(FakeHttp([])).parse_product(product(body_html=""))
    exclusive = InfinityCollectablesMonitor(FakeHttp([])).parse_product(
        product(body_html="Infinity Collectables Exclusive", tags=["Loungefly"])
    )
    assert not ordinary.exclusive
    assert exclusive.exclusive and exclusive.exclusive_retailer == "Infinity Collectables"

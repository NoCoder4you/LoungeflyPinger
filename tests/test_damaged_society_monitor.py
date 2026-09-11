from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from app.models import Availability, ReleasePrecision
from app.monitors.damaged_society import (
    CARD_HOLDER,
    CROSSBODY,
    FULL_SIZE_BACKPACK,
    MINI_BACKPACK,
    WALLET,
    DamagedSocietyMonitor,
    DamagedSocietyParseError,
)


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
        "id": 16093542842748,
        "title": "Coraline Other Mother Cosplay Mini Backpack",
        "handle": "coraline-other-mother-cosplay-mini-backpack",
        "body_html": "<p>Official Loungefly bag.</p>",
        "published_at": "2026-08-26T15:21:21+01:00",
        "vendor": "Loungefly",
        "product_type": "Backpacks",
        "tags": ["Halloween", "NEW"],
        "images": [{"src": "//cdn.shopify.com/coraline.jpg"}],
        "variants": [{
            "id": 57345825669500,
            "sku": "COBK0032",
            "barcode": "0671803587557",
            "price": "80.00",
            "compare_at_price": None,
            "available": True,
        }],
    }
    value.update(changes)
    return value


@pytest.mark.parametrize(("candidate", "expected"), [
    (product(), MINI_BACKPACK),
    (product(title="Pokémon Pikachu Full Size Backpack"), FULL_SIZE_BACKPACK),
    (product(title="Pokémon Bulbasaur Backpack"), FULL_SIZE_BACKPACK),
    (product(title="Madam Mim Lenticular Crossbody Bag",
             product_type="Crossbody Bags"), CROSSBODY),
    (product(title="Alice White Rabbit Zip Wallet", product_type="Wallets"), WALLET),
    # The live collection currently has a card holder under Shopify's Wallets type.
    (product(title="Coraline Other Mother Cosplay Card Holder",
             product_type="Wallets"), CARD_HOLDER),
])
def test_collection_product_classification(candidate, expected):
    assert DamagedSocietyMonitor.classify_product(candidate) == expected


@pytest.mark.asyncio
async def test_new_listing_discovers_only_mini_backpacks_with_stable_identity():
    mini = product()
    full_size = product(id=2, title="Pikachu Full Size Backpack", handle="pikachu-backpack")
    http = FakeHttp([{"products": [mini, full_size, deepcopy(mini)]}])

    discovered = await DamagedSocietyMonitor(http).discover_products()

    assert len(discovered) == 1
    assert discovered[0].retailer_product_id == "16093542842748"
    assert discovered[0].variant_id == "57345825669500"
    assert discovered[0].new_release is True
    assert discovered[0].currency == "GBP"


@pytest.mark.parametrize(("available", "expected"), [
    (True, Availability.IN_STOCK),
    (False, Availability.OUT_OF_STOCK),
])
def test_variant_stock_and_sold_out(available, expected):
    raw = product()
    raw["variants"][0]["available"] = available
    assert DamagedSocietyMonitor(FakeHttp([])).parse_product(raw).availability == expected


@pytest.mark.asyncio
async def test_check_product_detects_restock_from_variant_inventory():
    monitor = DamagedSocietyMonitor(FakeHttp([]))
    sold_out_raw = product()
    sold_out_raw["variants"][0]["available"] = False
    sold_out = monitor.parse_product(sold_out_raw)
    restocked_raw = product(product_type=None, type="Backpacks", description="Official bag")
    restocked_raw.pop("body_html")
    restocked_raw["variants"][0]["price"] = 8000

    checked = await DamagedSocietyMonitor(FakeHttp([restocked_raw])).check_product(sold_out)

    assert sold_out.availability == Availability.OUT_OF_STOCK
    assert checked.availability == Availability.IN_STOCK
    assert checked.retailer_product_id == sold_out.retailer_product_id


def test_price_and_sale_price():
    raw = product()
    raw["variants"][0].update(price="55.00", compare_at_price="80.00")
    parsed = DamagedSocietyMonitor(FakeHttp([])).parse_product(raw)
    assert parsed.price == Decimal("55.00")
    assert parsed.original_price == Decimal("80.00")


def test_product_js_integer_prices_are_pounds_not_pence():
    raw = product(product_type=None, type="Backpacks", description="Official bag")
    raw.pop("body_html")
    raw["variants"][0].update(price=5500, compare_at_price=8000)
    parsed = DamagedSocietyMonitor(FakeHttp([])).parse_product(raw)
    assert parsed.price == Decimal("55")
    assert parsed.original_price == Decimal("80")


def test_preorder_and_release_metadata():
    raw = product(body_html="<p>Pre-order now. Available from 15 September 2026.</p>")
    parsed = DamagedSocietyMonitor(FakeHttp([])).parse_product(raw)
    assert parsed.preorder is True
    assert parsed.availability == Availability.PREORDER
    assert parsed.release is not None
    assert parsed.release.precision == ReleasePrecision.DATE_ONLY
    assert parsed.release.release_date == date(2026, 9, 15)
    assert parsed.listing_published_at.isoformat() == "2026-08-26T15:21:21+01:00"


@pytest.mark.parametrize("mutation", [
    lambda value: value.pop("variants"),
    lambda value: value.update(variants=[{"price": "80.00"}]),
    lambda value: value["variants"][0].pop("price"),
])
def test_malformed_structured_product_is_rejected(mutation):
    raw = product()
    mutation(raw)
    with pytest.raises(DamagedSocietyParseError):
        DamagedSocietyMonitor(FakeHttp([])).parse_product(raw)

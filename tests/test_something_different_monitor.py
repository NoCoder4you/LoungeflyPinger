from dataclasses import replace
from decimal import Decimal

import pytest

from app.models import Availability, ReleasePrecision
from app.monitors.something_different import (
    SomethingDifferentMonitor, SomethingDifferentParseError,
)


def product(**changes):
    raw = {
        "id": 15762299158905,
        "title": "Loungefly Monster High Frankie Stein Cosplay Mini Backpack",
        "handle": "loungefly-monster-high-frankie-mini-backpack",
        "body_html": "A proper mini backpack.",
        "published_at": "2026-08-18T10:00:00+01:00",
        "vendor": "Loungefly",
        "tags": ["Loungefly", "Loungefly backpack", "LF August", "New In"],
        "variants": [{
            "id": 64474146079097, "available": True, "price": "65.00",
            "compare_at_price": "80.00", "sku": "671803588073", "barcode": "671803588073",
        }],
        "images": [{"src": "https://cdn.example/backpack.jpg"}],
    }
    raw.update(changes)
    return raw


def inventory(quantity):
    language = "Sold out" if quantity == 0 else "Only 1 left" if quantity == 1 else "in stock"
    return (
        f'<div class="product-stock-level__badge-text">{language}</div>'
        f'<script>window._RestockRocketConfig.variantsInventoryQuantity = '
        f'{{64474146079097 : parseInt("{quantity}"),}};</script>'
    )


class FakeHttp:
    def __init__(self, json_responses=(), text_responses=()):
        self.json_responses = list(json_responses)
        self.text_responses = list(text_responses)
        self.urls = []

    async def get_json(self, url):
        self.urls.append(url)
        response = self.json_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def get_text(self, url):
        self.urls.append(url)
        response = self.text_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_new_in_in_stock_price_sale_and_product_id():
    item = SomethingDifferentMonitor(FakeHttp()).parse_product(product(), html=inventory(3))
    assert item.retailer == "Something Different Gift Shop UK"
    assert item.retailer_product_id == "15762299158905"
    assert item.variant_id == "64474146079097"
    assert item.price == Decimal("65.00")
    assert item.original_price == Decimal("80.00")
    assert item.currency == "GBP"
    assert item.availability == Availability.IN_STOCK
    assert item.new_release is True


def test_only_one_left_is_low_stock_and_still_available():
    item = SomethingDifferentMonitor(FakeHttp()).parse_product(product(), html=inventory(1))
    assert item.availability == Availability.LOW_STOCK
    assert item.preorder is False


@pytest.mark.parametrize("quantity, expected", [(0, Availability.OUT_OF_STOCK), (-2, Availability.OUT_OF_STOCK)])
def test_sold_out_and_out_of_stock(quantity, expected):
    raw = product()
    raw["variants"][0]["available"] = False
    assert SomethingDifferentMonitor(FakeHttp()).parse_product(
        raw, html=inventory(quantity)
    ).availability == expected


def test_preorder_normalization_and_release_metadata():
    raw = product(body_html="Pre-order now. Releases 24 August 2026.")
    item = SomethingDifferentMonitor(FakeHttp()).parse_product(raw, html=inventory(3))
    assert item.preorder is True
    assert item.availability == Availability.PREORDER
    assert item.release.precision == ReleasePrecision.DATE_ONLY
    assert item.release.release_date.isoformat() == "2026-08-24"


def test_monthly_collection_is_metadata_not_fabricated_date():
    item = SomethingDifferentMonitor(FakeHttp()).parse_product(
        product(), html=inventory(3), collection_wave=(8, 2026, "Loungefly August 2026")
    )
    assert item.release.precision == ReleasePrecision.MONTH_ONLY
    assert item.release.release_month == 8
    assert item.release.release_year == 2026
    assert item.release.release_date is None
    assert "not an exact date" in item.release.source


@pytest.mark.asyncio
async def test_monthly_collection_membership_is_discovered():
    http = FakeHttp(json_responses=[
        {"collections": [
            {"title": "Loungefly August 2026", "handle": "loungefly-august-2026"},
            {"title": "Loungefly Backpacks", "handle": "loungefly-backpacks"},
        ]},
        {"products": [product()]},
    ])
    waves = await SomethingDifferentMonitor(http)._monthly_collection_memberships()
    assert waves == {"15762299158905": (8, 2026, "Loungefly August 2026")}


@pytest.mark.parametrize("title", [
    "Loungefly Coraline Mystery Mini Backpack Keychain Charm",
    "Loungefly Mini Backpack Bag Charm Blind Box", "Loungefly Minnie Wallet",
    "Loungefly Minnie Cardholder", "Loungefly Enamel Pin", "Loungefly Crossbody Bag",
    "Loungefly Minnie Purse",
])
def test_mini_backpack_charms_and_other_products_are_filtered(title):
    assert not SomethingDifferentMonitor._is_mini_backpack(product(title=title))


def test_real_mini_backpack_is_included_even_when_name_omits_brand():
    assert SomethingDifferentMonitor._is_mini_backpack(product(
        title="Monsters University Oozma Kappa House Figural Mini Backpack"
    ))


@pytest.mark.asyncio
async def test_discovery_suppresses_duplicate_product_ids():
    first, second = product(), product(title="Updated Loungefly Mini Backpack")
    http = FakeHttp(
        json_responses=[{"collections": []}, {"products": [first, second]}],
        text_responses=[inventory(3), inventory(1)],
    )
    items = await SomethingDifferentMonitor(http).discover_products()
    assert len(items) == 1
    assert items[0].name == "Updated Loungefly Mini Backpack"
    assert items[0].availability == Availability.LOW_STOCK


def test_missing_inventory_is_parser_failure():
    with pytest.raises(SomethingDifferentParseError):
        SomethingDifferentMonitor(FakeHttp()).parse_product(product(), html="<html>in stock</html>")


@pytest.mark.asyncio
async def test_check_parser_failure_returns_error():
    monitor = SomethingDifferentMonitor(FakeHttp(
        json_responses=[product()], text_responses=["<html>broken</html>",]
    ))
    original = monitor.parse_product(product(), html=inventory(3))
    checked = await monitor.check_product(replace(original, availability=Availability.IN_STOCK))
    assert checked.availability == Availability.ERROR

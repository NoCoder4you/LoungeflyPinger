from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from app.database import Database
from app.models import AlertType, Availability, ReleasePrecision
from app.monitors.cm_pop import CMPopMonitor, CMPopParseError
from app.services.monitor_service import MonitorService


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    async def get_json(self, url):
        self.urls.append(url)
        return self.responses.pop(0)


class Notifier:
    def __init__(self):
        self.alerts = []

    async def send(self, alert):
        self.alerts.append(alert)
        return True


def raw_product(**changes):
    value = {
        "id": 123,
        "title": "Disney Stitch Mini Backpack - Loungefly",
        "handle": "stitch-mini-backpack",
        "body_html": "<p>Available from 20 September 2026.</p>",
        "vendor": "Loungefly",
        "product_type": "Loungefly",
        "tags": [],
        "published_at": "2026-08-01T10:00:00+01:00",
        "images": [{"src": "https://cdn.shopify.com/stitch.jpg"}],
        "variants": [{
            "id": 456, "sku": "LF-STITCH-01", "available": True,
            "price": "59.99", "compare_at_price": "74.99",
        }],
    }
    value.update(changes)
    return value


@pytest.mark.parametrize("title", [
    "Loungefly Mini Backpack Wallet", "Loungefly Mini Backpack Crossbody",
    "Loungefly Mini Backpack Tote", "Loungefly Mini Backpack Pins",
    "Loungefly Mini Backpack Cardholder", "Loungefly Mini Backpack Keychain",
])
def test_mini_backpack_filter_excludes_other_product_types(title):
    assert not CMPopMonitor._is_mini_backpack(raw_product(title=title))


def test_mini_backpack_filter_requires_shopify_brand_and_explicit_category():
    assert CMPopMonitor._is_mini_backpack(raw_product())
    assert not CMPopMonitor._is_mini_backpack(raw_product(vendor="Funko"))
    assert not CMPopMonitor._is_mini_backpack(raw_product(title="Stitch Backpack"))


@pytest.mark.parametrize(("evidence", "available", "expected", "preorder"), [
    ("", True, Availability.IN_STOCK, False),
    ("", False, Availability.OUT_OF_STOCK, False),
    ("Pre-order now", False, Availability.PREORDER, True),
    ("Coming Soon", False, Availability.COMING_SOON, False),
])
def test_availability_preorder_and_coming_soon(evidence, available, expected, preorder):
    raw = raw_product(body_html=evidence)
    raw["variants"][0]["available"] = available
    product = CMPopMonitor(FakeHttp([])).parse_product(raw)
    assert product.availability == expected
    assert product.preorder is preorder


@pytest.mark.parametrize(("label", "region"), [
    ("CMPOP Exclusive", None), ("CM POP Exclusive", None),
    ("CMPOP EMEA EXCLUSIVE", "EMEA"),
])
def test_preserves_explicit_cm_pop_exclusives(label, region):
    product = CMPopMonitor(FakeHttp([])).parse_product(raw_product(tags=[label]))
    assert product.exclusive is True
    assert product.exclusive_retailer == "CM POP UK"
    assert product.exclusive_region == region


def test_preserves_other_explicit_retailer_exclusive_flag():
    product = CMPopMonitor(FakeHttp([])).parse_product(
        raw_product(body_html="A BoxLunch Exclusive!")
    )
    assert product.exclusive is True
    assert product.exclusive_retailer is None


def test_gbp_sale_price_sku_and_shopify_metadata():
    product = CMPopMonitor(FakeHttp([])).parse_product(raw_product())
    assert product.price == Decimal("59.99")
    assert product.original_price == Decimal("74.99")
    assert product.currency == "GBP"
    assert product.retailer_product_id == "123"
    assert product.variant_id == "456"
    assert product.sku == "LF-STITCH-01"
    assert product.vendor == "Loungefly"


@pytest.mark.asyncio
async def test_discovery_deduplicates_product_and_variants():
    raw = raw_product(variants=[
        {"id": 1, "sku": "SOLD", "available": False, "price": "70.00"},
        {"id": 2, "sku": "LIVE", "available": True, "price": "65.00"},
    ])
    products = await CMPopMonitor(FakeHttp([{"products": [raw, deepcopy(raw)]}])).discover_products()
    assert len(products) == 1
    assert products[0].variant_id == "2"
    assert products[0].sku == "LIVE"


def test_release_metadata_is_explicit_and_published_at_is_not_release_date():
    monitor = CMPopMonitor(FakeHttp([]))
    product = monitor.parse_product(raw_product())
    assert product.release.precision == ReleasePrecision.DATE_ONLY
    assert product.release.release_date == date(2026, 9, 20)
    assert product.listing_published_at.isoformat() == "2026-08-01T10:00:00+01:00"
    no_release = monitor.parse_product(raw_product(body_html="A newly published product."))
    assert no_release.release is None


@pytest.mark.parametrize("payload", [None, {}, {"products": None}, {"products": [None]}])
def test_malformed_structured_listing_data(payload):
    with pytest.raises(CMPopParseError):
        CMPopMonitor._product_list(payload)


@pytest.mark.parametrize("mutation", [
    lambda raw: raw.pop("id"),
    lambda raw: raw.update(variants=[]),
    lambda raw: raw["variants"][0].pop("available"),
    lambda raw: raw.update(tags="not-a-list"),
    lambda raw: raw.update(published_at="not-a-date"),
])
def test_malformed_structured_product_data(mutation):
    raw = raw_product()
    mutation(raw)
    with pytest.raises(CMPopParseError):
        CMPopMonitor(FakeHttp([])).parse_product(raw)


@pytest.mark.asyncio
async def test_new_listing_is_reported_after_initial_scan(tmp_path):
    first, second = raw_product(), raw_product(id=999, handle="new-bag", title="New Mini Backpack")
    monitor = CMPopMonitor(FakeHttp([{"products": [first]}, {"products": [first, second]}]))
    async with Database(tmp_path / "cm-pop.db") as database:
        notifier = Notifier()
        service = MonitorService(monitor, database, notifier, retailer_name="CM POP UK")
        assert await service.synchronize() == []
        alerts = await service.synchronize()
        assert [alert.alert_type for alert in alerts] == [AlertType.NEW_PRODUCT]
        assert alerts[0].product.retailer_product_id == "999"

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import NotificationConfig
from app.database import Database
from app.models import AlertType, Availability, Product, ReleasePrecision
from app.monitors.ozzie_collectables import (
    OzzieCollectablesMonitor, OzzieCollectablesParseError,
)
from app.notifications.base import NotificationProvider
from app.services.monitor_service import MonitorService

FIXTURES = Path(__file__).parent / "fixtures" / "ozzie_collectables"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def raw(*, title="Disney - Sensational 6 Mini Backpack", tags=None, vendor="Loungefly",
        available=True, price=13999, compare=None, sku="LOU-1", barcode="671803000001",
        description="", inventory=10, product_type="Disney"):
    return {
        "id": abs(hash((title, sku))) % 1_000_000 + 1, "title": title,
        "handle": title.lower().replace(" ", "-").replace("/", "-").replace("&", "and"),
        "vendor": vendor, "type": product_type,
        "tags": tags if tags is not None else ["BACKPACKS", "LOUNGEFLY", "INSTOCK"],
        "description": description,
        "published_at": "2026-09-01T10:00:00+10:00",
        "featured_image": "//cdn.example.com/item.jpg",
        "variants": [{"id": 123, "sku": sku, "barcode": barcode, "price": price,
                      "compare_at_price": compare, "available": available,
                      "inventory_quantity": inventory, "inventory_policy": "deny"}],
    }


class FakeHttp:
    def __init__(self, responses): self.responses = list(responses); self.urls = []
    async def get_json(self, url): self.urls.append(url); return self.responses.pop(0)


class Recorder(NotificationProvider):
    def __init__(self): self.alerts = []
    async def send(self, alert): self.alerts.append(alert); return True


def parse(value=None, **kwargs):
    value = value or raw(**kwargs)
    monitor = OzzieCollectablesMonitor(FakeHttp([]))
    return monitor.parse_product(value, product_type=monitor.classify_product_type(value))


@pytest.mark.parametrize(("title", "tags", "expected"), [
    ("Disney - Sensational 6 Mini Backpack", ["BACKPACKS", "LOUNGEFLY"], "Mini Backpack"),
    ("Star Wars - Rebel Backpack", ["BACKPACKS", "LOUNGEFLY"], "Backpack"),
    ("Minnie Bow Crossbody", ["CROSSBODY BAGS", "LOUNGEFLY"], "Crossbody"),
    ("Avengers Floral Tattoo Shoulder Bag", ["CROSSBODY BAGS", "LOUNGEFLY"], "Shoulder Bag"),
    ("Overwatch Logo Messenger Bag", ["LOUNGEFLY"], "Messenger Bag"),
    ("Jack Skellington Suit Handbag", ["HANDBAGS", "LOUNGEFLY"], "Handbag"),
    ("Charmed Tote Bag", ["TOTES", "LOUNGEFLY"], "Tote"),
    ("Ariel Fork Charm Satchel Bag", ["OTHER BAGGAGE ITEMS", "LOUNGEFLY"], "Satchel"),
    ("Mickey Ears Sling Bag", ["OTHER BAGGAGE ITEMS", "LOUNGEFLY"], "Sling Bag"),
])
def test_all_enabled_bag_types_use_category_title_and_tag_evidence(title, tags, expected):
    value = raw(title=title, tags=tags)
    assert OzzieCollectablesMonitor.classify_product_type(value) == expected


@pytest.mark.parametrize("value", [
    fixture("keychain_charm.json"),
    raw(title="Toy Story 30th Anniversary - Mystery Mini Bag Charm", tags=["LOUNGEFLY", "OTHER BAGGAGE ITEMS"]),
    raw(title="Cars - Mystery Mini Backpack Keychain Charm", tags=["BACKPACKS", "LOUNGEFLY"]),
])
def test_mini_backpack_keychain_and_mystery_bag_charms_are_excluded(value):
    assert OzzieCollectablesMonitor.classify_product_type(value) is None


def test_structured_product_captures_sale_sku_barcode_brand_and_real_release():
    product = parse(fixture("mini_backpack.json"))
    assert product.retailer == "Ozzie Collectables"
    assert product.retailer_product_id == product.sku == "LOUD21138"
    assert product.barcode == "671803123456" and product.vendor == "Loungefly"
    assert (product.price, product.original_price, product.currency) == (
        Decimal("139.99"), Decimal("199.99"), "AUD")
    assert product.release.precision == ReleasePrecision.DATE_ONLY
    assert product.release.release_date == date(2026, 11, 15)


@pytest.mark.parametrize(("kwargs", "expected"), [
    ({}, Availability.IN_STOCK),
    ({"available": False}, Availability.OUT_OF_STOCK),
    ({"description": "Out of Stock. Add to cart", "available": True}, Availability.OUT_OF_STOCK),
    ({"tags": ["LOUNGEFLY", "BACKPACKS", "PREORDER"], "inventory": -1}, Availability.PREORDER),
    ({"tags": ["LOUNGEFLY", "BACKPACKS", "INSTOCK", "SHOW-PREORDER"]}, Availability.IN_STOCK),
    ({"description": "Backorder available"}, Availability.BACKORDER),
    ({"description": "Coming Soon"}, Availability.COMING_SOON),
    ({"inventory": 2}, Availability.LOW_STOCK),
])
def test_availability_precedence_and_normalization(kwargs, expected):
    assert parse(**kwargs).availability == expected


def test_preorder_eta_month_is_not_an_official_release_date():
    product = parse(tags=["BACKPACKS", "LOUNGEFLY", "PREORDER"],
                    description="ETA: October 2026")
    assert product.preorder and product.availability == Availability.PREORDER
    assert product.estimated_arrival_text == "October 2026"
    assert product.estimated_arrival_date is None and product.release is None


def test_exact_eta_is_stored_as_arrival_and_release_stays_separate():
    product = parse(tags=["BACKPACKS", "LOUNGEFLY", "PREORDER"],
                    description="ETA: 30/11/2026. Release Date: 15 November 2026")
    assert product.estimated_arrival_date == date(2026, 11, 30)
    assert product.estimated_arrival_text == "30 November 2026"
    assert product.release.release_date == date(2026, 11, 15)


def test_us_exclusive_is_regional_not_an_ozzie_exclusive():
    product = parse(description="US Exclusive")
    assert product.exclusive and product.exclusive_region == "US"
    assert product.exclusive_retailer is None


def test_retailer_exclusive_requires_explicit_ozzie_wording():
    product = parse(description="Ozzie Collectables Exclusive")
    assert product.exclusive and product.exclusive_retailer == "Ozzie Collectables"


def test_funko_vendor_is_accepted_when_structured_description_identifies_loungefly():
    value = raw(vendor="Funko", tags=["BACKPACKS", "FUNKO"],
                description="The Loungefly Funko Pop mini backpack.")
    assert OzzieCollectablesMonitor._is_loungefly(value)


@pytest.mark.parametrize("value", [None, {}, fixture("malformed.json"), {"products": "bad"}])
def test_malformed_structured_data_fails_closed(value):
    monitor = OzzieCollectablesMonitor(FakeHttp([]))
    if isinstance(value, dict) and "products" in value:
        with pytest.raises(OzzieCollectablesParseError): monitor._product_list(value)
    else:
        with pytest.raises(OzzieCollectablesParseError): monitor.parse_product(value)


@pytest.mark.asyncio
async def test_parser_failure_on_targeted_check_returns_error_and_preserves_values():
    product = parse()
    checked = await OzzieCollectablesMonitor(FakeHttp([fixture("malformed.json")])).check_product(product)
    assert checked.availability == Availability.ERROR
    assert checked.price == product.price and checked.retailer_product_id == product.retailer_product_id


@pytest.mark.asyncio
async def test_discovery_paginates_deduplicates_and_uses_targeted_detail_batch():
    summary = raw(sku="LOUD21138")
    detail = json.loads(json.dumps(summary))
    summary["variants"][0]["barcode"] = None
    duplicate = dict(summary)
    http = FakeHttp([{"products": [summary, duplicate]}, detail])
    products = await OzzieCollectablesMonitor(http, detail_batch_size=1).discover_products()
    assert len(products) == 1 and products[0].barcode == "671803000001"
    assert "/collections/loungefly/products.json" in http.urls[0]
    assert http.urls[1].endswith("/products/disney---sensational-6-mini-backpack.js")


@pytest.mark.asyncio
async def test_unchanged_catalogue_does_not_repeat_detail_request():
    value = raw()
    http = FakeHttp([{"products": [value]}, value, {"products": [value]}])
    monitor = OzzieCollectablesMonitor(http, detail_batch_size=1)
    await monitor.discover_products(); await monitor.discover_products()
    assert sum(url.endswith(".js") for url in http.urls) == 1


@pytest.mark.asyncio
async def test_check_observes_restock():
    old = parse(available=False)
    current = raw(available=True)
    checked = await OzzieCollectablesMonitor(FakeHttp([current])).check_product(old)
    assert old.availability == Availability.OUT_OF_STOCK
    assert checked.availability == Availability.IN_STOCK


@pytest.mark.asyncio
async def test_initial_sync_new_listing_restock_eta_change_and_restart_dedup(tmp_path):
    path = tmp_path / "ozzie.db"
    notifier = Recorder()
    base = parse(available=False, description="ETA: October 2026")
    async with Database(path) as database:
        service = MonitorService(type("Monitor", (), {"discover_products": lambda self: _result([base])})(),
                                 database, notifier, retailer_name="Ozzie Collectables")
        assert await service.synchronize() == []  # initial synchronization is silent
        assert await service.synchronize() == []
        new = parse(raw(title="New Loungefly Crossbody", tags=["CROSSBODY BAGS", "LOUNGEFLY"], sku="NEW-1"))
        service.monitor = type("Monitor", (), {"discover_products": lambda self: _result([base, new])})()
        alerts = await service.synchronize()
        assert [a.alert_type for a in alerts] == [AlertType.NEW_PRODUCT]
        changed = replace(base, availability=Availability.IN_STOCK,
                          estimated_arrival_text="November 2026")
        service.monitor = type("Monitor", (), {"discover_products": lambda self: _result([changed, new])})()
        alerts = await service.synchronize()
        assert {a.alert_type for a in alerts} == {AlertType.RESTOCK, AlertType.ETA_CHANGED}
    async with Database(path) as database:  # persisted state prevents restart duplicates
        restarted = MonitorService(type("Monitor", (), {"discover_products": lambda self: _result([changed, new])})(),
                                   database, Recorder(), retailer_name="Ozzie Collectables")
        assert await restarted.synchronize() == []


async def _result(value):
    return value

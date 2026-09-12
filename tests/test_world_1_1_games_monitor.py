from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import PriceAlertConfig, ReleaseAlertConfig
from app.database import Database
from app.http import HttpClientError, HttpErrorKind
from app.models import AlertType, Availability, ReleasePrecision
from app.monitors.shopify import ShopifyParseError, ShopifyRetailerMonitor
from app.monitors.world_1_1_games import World11GamesMonitor
from app.notifications.base import NotificationProvider
from app.services.monitor_service import MonitorService


def raw(*, product_id=101, title="Disney Moon Mini Backpack", product_type="Mini Backpack",
        tags=None, available=True, inventory=None, price="79.99", compare=None,
        description="", sku="LF-MOON", barcode="671803000001", vendor="Loungefly"):
    return {
        "id": product_id, "title": title,
        "handle": title.casefold().replace(" ", "-").replace("/", "-"),
        "body_html": description, "published_at": "2026-08-01T12:00:00-07:00",
        "vendor": vendor, "product_type": product_type,
        "tags": tags if tags is not None else ["Loungefly", "Disney"],
        "images": [{"src": "//cdn.shopify.com/moon.jpg"}],
        "variants": [{"id": product_id * 10, "sku": sku, "barcode": barcode,
                      "available": available, "inventory_quantity": inventory,
                      "price": price, "compare_at_price": compare}],
    }


def parse(value=None, *, collections=(), signals=()):
    monitor = World11GamesMonitor(FakeHttp({}))
    value = value or raw()
    return monitor.parse_product(value, product_type=monitor.classify_product_type(value),
                                 collections=set(collections), signals=set(signals))


class FakeHttp:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []
        self.response_status = None

    async def get_json(self, url):
        self.urls.append(url)
        value = self.responses[url] if isinstance(self.responses, dict) else self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def reset_metrics(self):
        self.response_status = None


class Recorder(NotificationProvider):
    def __init__(self): self.alerts = []
    async def send(self, alert): self.alerts.append(alert); return True


@pytest.mark.parametrize(("title", "product_type", "expected"), [
    ("Disney Mini Backpack", "Backpacks", "Mini Backpack"),
    ("Star Wars Backpack", "Backpacks", "Backpack"),
    ("Minnie Crossbody Bag", "Crossbody", "Crossbody"),
    ("Ariel Tote", "Totes", "Tote"),
    ("Marvel Shoulder Bag", "Fashion Bags", "Shoulder Bag"),
    ("Harry Potter Handbag", "Fashion Bags", "Handbag"),
    ("Sanrio Satchel", "Bags", "Satchel"),
    ("Pokemon Bucket Bag", "Bags", "Bucket Bag"),
    ("Anime Drawstring Bag", "Bags", "Drawstring Bag"),
    ("Universal Convertible Bag", "Bags", "Convertible Bag"),
    ("Horror Sling Bag", "Bags", "Sling Bag"),
    ("Disney Messenger Bag", "Bags", "Messenger Bag"),
    ("Marvel Duffle Bag", "Bags", "Duffle Bag"),
    ("Loungefly Camera Bag", "Bags", "Other Bag"),
])
def test_all_enabled_bag_types(title, product_type, expected):
    assert World11GamesMonitor.classify_product_type(
        raw(title=title, product_type=product_type)
    ) == expected


@pytest.mark.parametrize(("title", "product_type"), [
    ("Mini Backpack Wallet", "Wallets"),
    ("Mystery Mini Backpack Keychain Charm", "Bag Charms"),
    ("Mini Backpack Enamel Pin", "Pins"),
    ("Loungefly Logo Shirt", "Apparel"),
    ("Loungefly Card Holder", "Card Holders"),
])
def test_accessories_are_excluded_even_when_the_title_mentions_a_bag(title, product_type):
    assert World11GamesMonitor.classify_product_type(
        raw(title=title, product_type=product_type)
    ) is None


def test_shopify_identity_pricing_brand_and_metadata_are_preserved():
    product = parse(raw(compare="90.00", tags=["Loungefly", "Marvel", "Sale"]))
    assert product.retailer == "WORLD 1-1 GAMES"
    assert product.retailer_product_id == "101" and product.variant_id == "1010"
    assert product.sku == "LF-MOON" and product.barcode == "671803000001"
    assert product.vendor == "Loungefly" and product.tags == ("Loungefly", "Marvel", "Sale")
    assert product.franchise == "Marvel"
    assert product.url.endswith("/products/disney-moon-mini-backpack")
    assert product.image_url == "https://cdn.shopify.com/moon.jpg"
    assert product.price == Decimal("79.99")
    assert product.compare_at_price == product.original_price == Decimal("90.00")
    assert product.sale


@pytest.mark.parametrize(("kwargs", "signals", "expected"), [
    ({}, (), Availability.IN_STOCK),
    ({"available": False}, (), Availability.OUT_OF_STOCK),
    ({"inventory": 2}, (), Availability.LOW_STOCK),
    ({"description": "Pre-order now"}, (), Availability.PREORDER),
    ({}, ("preorder",), Availability.PREORDER),
    ({"description": "Backorder available"}, (), Availability.BACKORDER),
    ({"description": "Coming Soon"}, (), Availability.COMING_SOON),
])
def test_availability_uses_variant_and_explicit_merchandising_evidence(kwargs, signals, expected):
    assert parse(raw(**kwargs), signals=signals).availability == expected


def test_collection_metadata_sale_clearance_rare_and_vaulted():
    product = parse(raw(description="This retired and vaulted Loungefly bag."),
                    collections=("rare-loungefly", "sale-1", "clearance"),
                    signals=("rare", "sale", "clearance"))
    assert product.collections == ("clearance", "rare-loungefly", "sale-1")
    assert product.sale and product.clearance and product.vaulted
    assert product.collection_type == "RARE"


def test_exclusivity_requires_product_level_evidence_not_collection_membership():
    marketing_only = parse(collections=("exclusives",), signals=("exclusive_collection",))
    assert not marketing_only.exclusive
    retailer = parse(raw(description="W11G Exclusive"))
    assert retailer.exclusive and retailer.exclusive_retailer == "WORLD 1-1 GAMES"
    regional = parse(raw(description="US Exclusive"))
    assert regional.exclusive and regional.exclusive_region == "US"


def test_release_eta_and_shopify_publication_are_distinct():
    product = parse(raw(description=(
        "Release Date: September 20, 2026 at 09:30. "
        "Estimated arrival: October 2, 2026. Estimated ship date: October 4, 2026."
    )))
    assert product.release.precision == ReleasePrecision.EXACT_DATETIME
    assert product.release.release_date == date(2026, 9, 20)
    assert product.estimated_arrival_date == date(2026, 10, 2)
    assert product.estimated_ship_date == date(2026, 10, 4)
    assert product.listing_published_at.date() == date(2026, 8, 1)


def test_published_at_alone_never_becomes_release_date():
    product = parse()
    assert product.listing_published_at is not None and product.release is None


@pytest.mark.parametrize("bad", [None, {}, {"products": "bad"}, {"products": [None]}])
def test_malformed_json_fails_closed(bad):
    if isinstance(bad, dict) and "products" in bad:
        with pytest.raises(ShopifyParseError):
            ShopifyRetailerMonitor._product_list(bad)
    else:
        with pytest.raises((ShopifyParseError, TypeError)):
            World11GamesMonitor(FakeHttp({})).parse_product(
                bad, product_type="Mini Backpack", collections=set(), signals=set()
            )


def test_missing_variants_and_variant_availability_fail_closed():
    value = raw(); value["variants"] = []
    with pytest.raises(ShopifyParseError): parse(value)
    value = raw(); value["variants"][0].pop("available")
    with pytest.raises(ShopifyParseError): parse(value)


@pytest.mark.asyncio
async def test_check_parser_failure_returns_error_and_preserves_prior_stock_data():
    product = parse()
    checked = await World11GamesMonitor(FakeHttp([{"bad": True}])).check_product(product)
    assert checked.availability == Availability.ERROR
    assert checked.price == product.price and checked.retailer_product_id == "101"


@pytest.mark.asyncio
async def test_http_429_is_unhealthy_and_targeted_check_returns_error():
    error = HttpClientError(HttpErrorKind.RATE_LIMITED, "rate limited", 429)
    monitor = World11GamesMonitor(FakeHttp([error, error]))
    assert not await monitor.health_check()
    assert (await monitor.check_product(parse())).availability == Availability.ERROR


@pytest.mark.asyncio
async def test_discovery_paginates_and_deduplicates_overlapping_collections():
    monitor = World11GamesMonitor(FakeHttp([]))
    monitor.collections = {"loungefly": None, "new-arrival-1": "new_arrival",
                           "rare-loungefly": "rare"}
    monitor.page_size = 2
    one = raw(product_id=1, title="Disney Mini Backpack", sku="ONE")
    two = raw(product_id=2, title="Marvel Crossbody", product_type="Crossbody", sku="TWO")
    monitor.http.responses = [
        {"products": [one, two]}, {"products": []},
        {"products": [one]}, {"products": [one]},
    ]
    products = await monitor.discover_products()
    assert len(products) == 2
    first = next(product for product in products if product.retailer_product_id == "1")
    assert first.collections == ("loungefly", "new-arrival-1", "rare-loungefly")
    assert first.collection_type == "RARE"
    assert "page=2" in monitor.http.urls[1]


async def result(value): return value


@pytest.mark.asyncio
async def test_initial_sync_restart_dedup_and_all_core_change_alerts(tmp_path: Path):
    db_path = tmp_path / "world.db"
    notifier = Recorder()
    base = parse(raw(available=False, description="Release Date: September 20, 2026"))
    async with Database(db_path) as database:
        monitor = type("Monitor", (), {"discover_products": lambda self: result([base])})()
        service = MonitorService(monitor, database, notifier, retailer_name="WORLD 1-1 GAMES",
                                 price_alerts=PriceAlertConfig(minimum_drop_percent=10,
                                                              minimum_drop_value=5),
                                 release_alerts=ReleaseAlertConfig(notify_existing_on_upgrade=True))
        assert await service.synchronize() == []
        added = parse(raw(product_id=202, title="New Crossbody", product_type="Crossbody",
                              sku="NEW", description="Pre-order now"))
        service.monitor = type("Monitor", (), {"discover_products": lambda self: result([base, added])})()
        assert [a.alert_type for a in await service.synchronize()] == [AlertType.NEW_PRODUCT]
        changed_base = replace(base, availability=Availability.IN_STOCK, price=Decimal("69.99"),
                               release=parse(raw(description="Release Date: September 21, 2026")).release)
        opened = replace(added, availability=Availability.OUT_OF_STOCK, preorder=False)
        service.monitor = type("Monitor", (), {"discover_products": lambda self: result([changed_base, opened])})()
        kinds = {a.alert_type for a in await service.synchronize()}
        assert {AlertType.RESTOCK, AlertType.PRICE_DROP, AlertType.RELEASE_DATE_CHANGED} <= kinds
        reopened = replace(opened, availability=Availability.PREORDER, preorder=True)
        service.monitor = type("Monitor", (), {"discover_products": lambda self: result([changed_base, reopened])})()
        assert [a.alert_type for a in await service.synchronize()] == [AlertType.PREORDER_OPEN]
    async with Database(db_path) as database:
        restarted = MonitorService(
            type("Monitor", (), {"discover_products": lambda self: result([changed_base, reopened])})(),
            database, Recorder(), retailer_name="WORLD 1-1 GAMES"
        )
        assert await restarted.synchronize() == []

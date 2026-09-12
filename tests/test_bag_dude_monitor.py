from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

import pytest

from app.config import PriceAlertConfig
from app.database import Database
from app.http import HttpClientError, HttpErrorKind
from app.models import AlertType, Availability, ReleasePrecision
from app.monitors.bag_dude import BagDudeMonitor
from app.monitors.shopify import ShopifyParseError
from app.notifications.base import NotificationProvider
from app.services.monitor_service import MonitorService

FIXTURES = Path(__file__).parent / "fixtures/bag_dude"


class FakeHttp:
    def __init__(self, products, *, failure=None):
        self.products, self.failure, self.urls = products, failure, []

    async def get_json(self, url):
        self.urls.append(url)
        if self.failure:
            raise self.failure
        if "/products/" in url and url.endswith(".js"):
            handle = url.rsplit("/", 1)[-1][:-3]
            return next(item for item in self.products if item["handle"] == handle)
        page = int(url.rsplit("page=", 1)[-1])
        return {"products": self.products if page == 1 else []}

    async def get_text(self, url):
        self.urls.append(url)
        if self.failure:
            raise self.failure
        return (FIXTURES / "homepage.html").read_text()


class Recorder(NotificationProvider):
    def __init__(self): self.alerts = []
    async def send(self, alert):
        self.alerts.append(alert)
        return True


def raws(): return json.loads((FIXTURES / "products.json").read_text())["products"]


def parse(index, **changes):
    raw = json.loads(json.dumps(raws()[index]))
    raw.update(changes)
    monitor = BagDudeMonitor(FakeHttp([]))
    kind = monitor.classify_product_type(raw)
    return monitor.parse_product(raw, product_type=kind,
                                 collections={"loungefly-bags"}, signals=set())


@pytest.mark.parametrize(("index", "expected"), [
    (0, "Mini Backpack"), (1, "Backpack"), (2, "Crossbody"), (3, "Tote"),
    (4, "Sling Bag"), (5, "Convertible Bag"), (6, "Mini Backpack"),
])
def test_all_bag_shapes_are_classified(index, expected):
    assert BagDudeMonitor.classify_product_type(raws()[index]) == expected


@pytest.mark.parametrize("index", [7, 8, 9])
def test_accessories_are_excluded(index):
    assert BagDudeMonitor.classify_product_type(raws()[index]) is None


@pytest.mark.parametrize("index", [10, 11])
def test_other_bag_brands_are_not_loungefly(index):
    raw = json.loads(json.dumps(raws()[index]))
    raw["body_html"] = "Browse this retailer's Loungefly collection too."
    assert BagDudeMonitor.classify_product_type(raw)
    assert not BagDudeMonitor.is_loungefly(raw)


@pytest.mark.parametrize(("index", "expected"), [
    (0, Availability.IN_STOCK), (12, Availability.OUT_OF_STOCK),
    (13, Availability.LOW_STOCK), (14, Availability.PREORDER),
    (15, Availability.COMING_SOON),
])
def test_structured_availability(index, expected):
    assert parse(index).availability == expected


def test_shopify_identity_price_and_publication_metadata():
    product = parse(0)
    assert product.retailer == "The Bag Dude" and product.currency == "USD"
    assert (product.retailer_product_id, product.variant_id) == ("1", "10")
    assert (product.sku, product.barcode, product.vendor) == ("LF-1", "012345678901", "Loungefly")
    assert product.tags == ("Loungefly", "Disney") and product.url.endswith("/products/item-1")
    assert product.price == Decimal("75.00") and product.compare_at_price == Decimal("90.00")
    assert product.listing_published_at.date() == date(2026, 8, 1)
    assert product.release.precision == ReleasePrecision.DATE_ONLY
    assert product.release.release_date == date(2026, 9, 20)
    assert product.new_release is False and product.collection_type == "NEW"


def test_published_new_arrivals_and_vault_do_not_invent_release_dates():
    ordinary = parse(1)
    vault = BagDudeMonitor(FakeHttp([])).parse_product(
        raws()[16], product_type="Mini Backpack",
        collections={"loungefly-bags", "the-bag-dude-vault"}, signals={"vault"},
    )
    assert ordinary.listing_published_at and ordinary.release is None
    assert vault.vaulted and vault.collection_type == "VAULT" and vault.release is None
    assert vault.limited_edition and vault.exclusive_retailer == "Universal"
    assert vault.new_release is False


def test_exclusive_sources_are_kept_distinct():
    regional, retailer = parse(17), parse(18)
    assert regional.exclusive_region == "US" and regional.exclusive_retailer is None
    assert retailer.exclusive_retailer == "The Bag Dude"


@pytest.mark.asyncio
async def test_collections_homepage_pagination_and_deduplication():
    monitor = BagDudeMonitor(FakeHttp(raws()))
    monitor.page_size = len(raws())
    products = await monitor.discover_products()
    # Accessories and both unrelated brands are filtered despite collection membership.
    assert len(products) == 14
    assert len({p.retailer_product_id for p in products}) == len(products)
    first = next(p for p in products if p.retailer_product_id == "1")
    assert set(first.collections) == {
        "loungefly-bags", "backpack", "crossbody-totes", "the-bag-dude-vault",
        "homepage-new-arrivals",
    }
    assert first.vaulted and not first.new_release
    assert any("page=2" in url for url in monitor.http.urls)
    assert not any(url.endswith("item-1.js") for url in monitor.http.urls)


@pytest.mark.parametrize("value", [None, {}, {"variants": []}, {"variants": [{}]}])
def test_malformed_products_fail_closed(value):
    monitor = BagDudeMonitor(FakeHttp([]))
    with pytest.raises((ShopifyParseError, TypeError)):
        monitor.parse_product(value, product_type="Mini Backpack", collections=set(), signals=set())


@pytest.mark.asyncio
@pytest.mark.parametrize("status,kind", [(429, HttpErrorKind.RATE_LIMITED), (500, HttpErrorKind.SERVER)])
async def test_http_failures_preserve_checked_product(status, kind):
    product = parse(0)
    monitor = BagDudeMonitor(FakeHttp([], failure=HttpClientError(kind, "failure", status)))
    checked = await monitor.check_product(product)
    assert checked.availability == Availability.ERROR
    assert checked.price == product.price and checked.retailer_product_id == product.retailer_product_id


async def result(value): return value


@pytest.mark.asyncio
async def test_initial_sync_new_listing_restock_preorder_price_drop_and_restart(tmp_path):
    recorder = Recorder()
    sold_out = parse(12)
    async with Database(tmp_path / "bag-dude.db") as database:
        service = MonitorService(
            type("M", (), {"discover_products": lambda self: result([sold_out])})(),
            database, recorder, retailer_name="The Bag Dude",
            price_alerts=PriceAlertConfig(minimum_drop_percent=10, minimum_drop_value=5),
        )
        assert await service.synchronize() == []
        preorder = parse(14)
        service.monitor = type("M", (), {"discover_products": lambda self: result([sold_out, preorder])})()
        assert [a.alert_type for a in await service.synchronize()] == [AlertType.NEW_PRODUCT]
        restocked = replace(sold_out, availability=Availability.IN_STOCK, price=Decimal("70"))
        closed = replace(preorder, availability=Availability.OUT_OF_STOCK, preorder=False)
        service.monitor = type("M", (), {"discover_products": lambda self: result([restocked, closed])})()
        alerts = await service.synchronize()
        assert {a.alert_type for a in alerts} == {AlertType.RESTOCK, AlertType.PRICE_DROP}
        opened = replace(closed, availability=Availability.PREORDER, preorder=True)
        service.monitor = type("M", (), {"discover_products": lambda self: result([restocked, opened])})()
        assert [a.alert_type for a in await service.synchronize()] == [AlertType.PREORDER_OPEN]
    async with Database(tmp_path / "bag-dude.db") as database:
        service = MonitorService(
            type("M", (), {"discover_products": lambda self: result([restocked, opened])})(),
            database, Recorder(), retailer_name="The Bag Dude",
        )
        assert await service.synchronize() == []


@pytest.mark.asyncio
async def test_failed_scan_preserves_persisted_state(tmp_path):
    sold_out = parse(12)
    async with Database(tmp_path / "failure.db") as database:
        monitor = type("M", (), {"discover_products": lambda self: result([sold_out])})()
        service = MonitorService(monitor, database, Recorder(), retailer_name="The Bag Dude")
        await service.synchronize()
        async def fail(self): raise ShopifyParseError("bad feed")
        service.monitor = type("M", (), {"discover_products": fail})()
        assert await service.synchronize() == []
        row = await (await database.connection.execute(
            "SELECT availability, price FROM product_states"
        )).fetchone()
        assert row == (Availability.OUT_OF_STOCK.value, "90.00")

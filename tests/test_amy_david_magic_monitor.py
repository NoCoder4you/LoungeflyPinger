from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.database import Database
from app.models import AlertType, Availability
from app.monitors.amy_david_magic import AmyDavidMagicMonitor
from app.monitors.shopify import ShopifyParseError
from app.notifications.base import NotificationProvider
from app.services.monitor_service import MonitorService


class FakeHttp:
    def __init__(self, products=(), article=""): self.products, self.article = list(products), article
    async def get_json(self, url):
        if "/products/" in url:
            handle = url.rsplit("/", 1)[-1].removesuffix(".js")
            return next(product for product in self.products if product["handle"] == handle)
        return {"products": self.products if "page=1" in url else []}
    async def get_text(self, url): return self.article


class Recorder(NotificationProvider):
    async def send(self, alert): return True


def raw(title="Cinnamoroll Sweet Shop Mini Backpack Exclusive Loungefly Sanrio", **changes):
    value = {
        "id": 123, "title": title, "handle": "sweet-shop", "vendor": "Loungefly",
        "product_type": "Mini Backpack", "tags": ["Loungefly", "Sanrio"],
        "published_at": "2026-08-15T12:00:00-04:00", "featured_image": "https://x.test/a.jpg",
        "body_html": """<p>Product Details: Brand: Loungefly License: Official Sanrio product
        Character: Cinnamoroll Property: Sanrio Edition: Limited Edition Exclusive
        Style: Sweet Shop Mini Backpack Barcode: 012345678901 Dimensions: 9 x 10</p>""",
        "variants": [{"id": 456, "available": True, "price": "90.00",
                      "compare_at_price": "100.00", "sku": "LF-SAN-1",
                      "barcode": "012345678901", "inventory_quantity": 7}],
    }
    value.update(changes)
    return value


def parse(value):
    monitor = AmyDavidMagicMonitor(FakeHttp())
    return monitor.parse_product(value, product_type=monitor.classify_product_type(value),
                                 collections={"mini-backpacks-all"}, signals=set())


@pytest.mark.parametrize(("title", "kind"), [
    ("Sweet Shop Mini Backpack", "Mini Backpack"), ("Figural Crossbody", "Crossbody"),
    ("Figural Coin Bag", "Coin Bag"), ("Satchel Bag", "Satchel"),
])
def test_enabled_bag_classification(title, kind):
    assert AmyDavidMagicMonitor.classify_product_type(raw(title)) == kind


@pytest.mark.parametrize("title", ["Zip Around Wallet", "Large Card Holder", "2-Pack Pin Set"])
def test_non_bags_are_excluded(title):
    assert AmyDavidMagicMonitor.classify_product_type(raw(title)) is None


def test_collector_metadata_identity_price_and_publication_are_structured():
    product = parse(raw())
    assert (product.retailer_product_id, product.variant_id, product.sku, product.barcode) == (
        "123", "456", "LF-SAN-1", "012345678901")
    assert product.vendor == "Loungefly" and product.license == "Official Sanrio product"
    assert product.characters == ("Cinnamoroll",) and product.property == "Sanrio"
    assert product.edition == "Limited Edition Exclusive" and product.style == "Sweet Shop Mini Backpack"
    assert product.price == Decimal("90.00") and product.compare_at_price == Decimal("100.00")
    assert product.listing_published_at.date() == date(2026, 8, 15) and product.release is None
    assert product.limited_edition


def test_only_explicit_release_information_becomes_a_release_and_changes_parse():
    first = parse(raw(body_html="Official release date: September 20, 2026"))
    changed = parse(raw(body_html="Official release date: September 27, 2026"))
    assert first.release.release_date == date(2026, 9, 20)
    assert changed.release.release_date == date(2026, 9, 27)
    assert first.release.release_date != changed.release.release_date


@pytest.mark.parametrize(("text", "retailer", "event"), [
    ("Grotto Treasures Exclusive", "Grotto Treasures", False),
    ("Loungefly Exclusive", "Loungefly", False),
    ("AmyDavidMagic Exclusive", "AmyDavidMagic", False),
    ("D23 2026 Exclusive", None, True),
])
def test_original_exclusivity_is_preserved(text, retailer, event):
    product = parse(raw(body_html=f"<p>Edition: {text}</p>"))
    assert product.exclusive and product.exclusive_retailer == retailer
    assert product.event_exclusive is event
    if event: assert (product.event_name, product.event_year) == ("D23", 2026)


@pytest.mark.parametrize(("available", "body", "expected", "incoming"), [
    (True, "", Availability.IN_STOCK, None),
    (False, "", Availability.OUT_OF_STOCK, None),
    (False, "Coming soon", Availability.COMING_SOON, "COMING_SOON"),
    (False, "Ordered · In transit · Coming soon", Availability.COMING_SOON, "IN_TRANSIT"),
    (True, "Preorder", Availability.PREORDER, None),
])
def test_availability_uses_variants_and_non_stock_incoming_states(available, body, expected, incoming):
    product = parse(raw(body_html=body, variants=[{"id": 1, "available": available,
                        "price": "80", "sku": "X", "barcode": "1"}]))
    assert product.availability == expected and product.incoming_status == incoming


@pytest.mark.asyncio
async def test_blog_discovers_only_bags_and_links_exact_existing_product():
    title = "D23 Exclusive Stitch 626 Cosplay Mini Backpack"
    article = f'''<div class="article-template__content"><h3>{title}</h3>
      <p>Status: Ordered · In transit · Coming soon</p><h3>Buzz Card Holder</h3><p>Coming soon</p></div>'''
    monitor = AmyDavidMagicMonitor(FakeHttp([raw(title)], article))
    products = await monitor.discover_products()
    assert len(products) == 1 and products[0].retailer_product_id == "123"
    assert products[0].incoming_status == "IN_TRANSIT"
    assert products[0].release is None and "blog:d23-2026-loungefly-exclusives" in products[0].discovery_sources


@pytest.mark.asyncio
async def test_initial_sync_restart_restock_release_change_and_parser_failure(tmp_path):
    sold = parse(raw(variants=[{"id": 1, "available": False, "price": "90"}]))
    async def result(items): return items
    async with Database(tmp_path / "amy.db") as database:
        service = MonitorService(type("M", (), {"discover_products": lambda self: result([sold])})(),
                                 database, Recorder(), retailer_name="AmyDavidMagic")
        assert await service.synchronize() == []
        stocked = replace(sold, availability=Availability.IN_STOCK)
        service.monitor = type("M", (), {"discover_products": lambda self: result([stocked])})()
        assert [a.alert_type for a in await service.synchronize()] == [AlertType.RESTOCK]
    async with Database(tmp_path / "amy.db") as database:
        service = MonitorService(type("M", (), {"discover_products": lambda self: result([stocked])})(),
                                 database, Recorder(), retailer_name="AmyDavidMagic")
        assert await service.synchronize() == []
        async def fail(self): raise ShopifyParseError("malformed")
        service.monitor = type("M", (), {"discover_products": fail})()
        assert await service.synchronize() == []
        state = await (await database.connection.execute("SELECT availability FROM product_states")).fetchone()
        assert state[0] == Availability.IN_STOCK


@pytest.mark.parametrize("value", [None, {}, {"variants": []}, {"id": 1, "title": "Bag", "handle": "x", "variants": [{}]}])
def test_malformed_or_missing_variant_fails_closed(value):
    with pytest.raises((ShopifyParseError, TypeError)):
        AmyDavidMagicMonitor(FakeHttp()).parse_product(
            value, product_type="Mini Backpack", collections=set(), signals=set())

from dataclasses import replace
from datetime import date
from decimal import Decimal
import pytest

from app.config import PriceAlertConfig
from app.database import Database
from app.http import HttpClientError, HttpErrorKind
from app.models import Alert, AlertType, Availability, ReleasePrecision
from app.monitors.disney_mad import DisneyMadMonitor
from app.monitors.shopify import ShopifyParseError
from app.notifications.discord import build_discord_payload
from app.notifications.base import NotificationProvider
from app.services.monitor_service import MonitorService
from app.services.product_service import ProductService


class FakeHttp:
    def __init__(self, responses):
        self.responses, self.urls, self.response_status = responses, [], None

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


def raw(product_id=1, title="Loungefly Disney Rapunzel Lantern Night Mini Backpack", *,
        available=True, description="", product_type="Backpacks", vendor="Disney Mad Shop",
        tags=None, price="64.99", compare=None, sku="LF-RAP-001", barcode="671803000001",
        published="2026-08-01T12:00:00+01:00"):
    return {
        "id": product_id, "title": title,
        "handle": title.casefold().replace(" ", "-").replace("/", "-"),
        "body_html": description, "published_at": published, "vendor": vendor,
        "product_type": product_type, "tags": tags if tags is not None else ["Disney"],
        "images": [{"src": "//cdn.shopify.com/product.jpg"}],
        "variants": [{"id": product_id * 10, "available": available,
                      "inventory_quantity": None, "price": price,
                      "compare_at_price": compare, "sku": sku, "barcode": barcode}],
    }


def parse(value=None, *, collections=("backpacks",), signals=()):
    monitor = DisneyMadMonitor(FakeHttp([]))
    value = value or raw()
    kind = monitor.classify_product_type(value)
    assert kind is not None
    return monitor.parse_product(value, product_type=kind,
                                 collections=set(collections), signals=set(signals))


@pytest.mark.parametrize(("title", "expected"), [
    ("Loungefly Rapunzel Lantern Night Mini Backpack", "Mini Backpack"),
    ("Loungefly 101 Dalmatians Book Crossbody Bag", "Crossbody"),
    ("Loungefly Lion King Tropical Mini Satchel Bag", "Satchel"),
    ("Loungefly Mickey Backpack", "Backpack"),
    ("Loungefly Mickey Mid Size Backpack", "Mid Size Backpack"),
    ("Loungefly Mickey Full Size Backpack", "Full Size Backpack"),
    ("Loungefly Mickey Tote", "Tote"),
    ("Loungefly Mickey Shoulder Bag", "Shoulder Bag"),
    ("Loungefly Mickey Handbag", "Handbag"),
    ("Loungefly Mickey Bucket Bag", "Bucket Bag"),
    ("Loungefly Mickey Drawstring Bag", "Drawstring Bag"),
    ("Loungefly Mickey Convertible Bag", "Convertible Bag"),
    ("Loungefly Mickey Sling Bag", "Sling Bag"),
    ("Loungefly Mickey Messenger Bag", "Messenger Bag"),
    ("Loungefly Mickey Duffle Bag", "Duffle Bag"),
    ("Loungefly Mickey Camera Bag", "Other Bag"),
])
def test_all_enabled_bag_types_are_classified_from_product_evidence(title, expected):
    assert DisneyMadMonitor.classify_product_type(
        raw(title=title, product_type="Bags" if expected == "Other Bag" else "Backpacks")
    ) == expected


def test_backpack_and_coin_purse_bundle_keeps_the_bag_as_primary_item():
    product = parse(raw(title="Toy Story Loungefly Mini Backpack and Coin Purse Disney Parks"))
    assert product.product_type == "Mini Backpack"
    assert product.bundle and product.included_items == ("COIN_PURSE",)


def test_non_loungefly_disney_backpack_is_excluded():
    value = raw(title="Disney Winnie the Pooh Backpack", vendor="Disney Mad Shop", tags=[])
    value["body_html"] = "Official Disney backpack"
    assert not DisneyMadMonitor.is_loungefly(value)


@pytest.mark.parametrize(("title", "description", "origin"), [
    ("Loungefly Minnie Mini Backpack - Disney Parks", "Disney Parks Loungefly", None),
    ("Cinderella Castle Light-Up Loungefly Mini Backpack – Walt Disney World", "Direct from The Most Magical Place on Earth", "Walt Disney World"),
    ("Sleeping Beauty Castle Loungefly Mini Backpack – Disneyland 70th Anniversary - Disney Parks", "The Happiest Place on Earth", "Disneyland Resort"),
])
def test_explicit_disney_parks_products_and_origins(title, description, origin):
    product = parse(raw(title=title, description=description))
    assert product.disney_parks and product.franchise == "Disney Parks"
    assert product.parks_origin == origin


def test_incidental_trip_to_parks_wording_is_not_parks_origin_evidence():
    product = parse(raw(description="Take this ordinary bag on trips to the Disney Parks!"))
    assert not product.disney_parks and product.parks_origin is None


@pytest.mark.parametrize(("wording", "retailer"), [
    ("BoxLunch Exclusive", "BoxLunch"),
    ("Hot Topic Exclusive", "Hot Topic"),
    ("707 Street Exclusive", "707 Street"),
    ("Retrofacts exclusive", "Retrofacts"),
    ("D23 Member Exclusive", "D23"),
])
def test_imported_exclusive_origin_is_preserved(wording, retailer):
    product = parse(raw(title=f"Loungefly Cars Mini Backpack - {wording}"))
    assert product.exclusive and product.exclusive_retailer == retailer
    assert product.exclusive_retailer != "Disney Mad"
    assert product.exclusive_type == "RETAILER_EXCLUSIVE"


def test_parks_exclusive_and_shopdisney_evidence_is_preserved():
    product = parse(raw(description="Exclusive to Disney Parks and shopDisney"))
    assert product.disney_parks and product.exclusive
    assert product.exclusive_retailer == "Disney Parks"
    assert product.exclusive_type == "DISNEY_PARKS"


def test_discord_alert_retains_product_type_parks_and_original_exclusive_fields():
    imported = parse(raw(title="Loungefly Cars Mini Backpack - BoxLunch Exclusive"))
    fields = build_discord_payload(
        Alert(AlertType.NEW_PRODUCT, imported)
    )["embeds"][0]["fields"]
    displayed = {field["name"]: field["value"] for field in fields}
    assert displayed["Retailer"] == "Disney Mad"
    assert displayed["Product Type"] == "Mini Backpack"
    assert displayed["Exclusive Retailer"] == "BoxLunch"


@pytest.mark.parametrize(("available", "description", "expected"), [
    (True, "Add to cart", Availability.IN_STOCK),
    (False, "Sold out", Availability.OUT_OF_STOCK),
    (False, "Sold Out <button>Add to Cart</button>", Availability.OUT_OF_STOCK),
    (False, "<button>Add to Cart</button>", Availability.OUT_OF_STOCK),
    (True, "Pre-order now", Availability.PREORDER),
    (True, "Coming Soon", Availability.COMING_SOON),
    (True, "Backorder available", Availability.BACKORDER),
])
def test_structured_variant_stock_precedence_and_supported_states(available, description, expected):
    assert parse(raw(available=available, description=description)).availability == expected


def test_shopify_identity_gbp_prices_and_collection_metadata():
    product = parse(raw(product_id=42, compare="79.99"),
                    collections=("backpacks", "disney-parks"))
    assert product.retailer == "Disney Mad" and product.currency == "GBP"
    assert product.retailer_product_id == "42" and product.variant_id == "420"
    assert product.sku == "LF-RAP-001" and product.barcode == "671803000001"
    assert product.price == Decimal("64.99")
    assert product.compare_at_price == product.original_price == Decimal("79.99")
    assert product.collections == ("backpacks", "disney-parks")
    assert product.url.startswith("https://disneymad.com/products/")


@pytest.mark.parametrize("description", [
    "Celebrating Disneyland's 70th Anniversary, first opened July 17, 1955.",
    "The Little Mermaid has been popular since its release in 1989.",
    "Disneyland 70th Anniversary 2025 collection.",
])
def test_historical_and_anniversary_dates_are_not_release_dates(description):
    product = parse(raw(description=description))
    assert product.release is None


def test_shopify_publication_is_listing_metadata_not_release_date():
    product = parse()
    assert product.listing_published_at.date() == date(2026, 8, 1)
    assert product.release is None


def test_actual_explicit_release_date_uses_shared_release_architecture():
    product = parse(raw(description="Release Date: 20 September 2026 at 09:30"))
    assert product.release.precision == ReleasePrecision.EXACT_DATETIME
    assert product.release.release_date == date(2026, 9, 20)
    assert product.release.timezone == "Europe/London"


def test_anniversary_and_named_collection_are_metadata_not_release():
    product = parse(raw(description="Disneyland 70th Anniversary. Critter Chaos Collection."))
    assert product.event_collection == "Disneyland 70th Anniversary"
    assert product.series == "Critter Chaos Collection"
    assert product.release is None


@pytest.mark.asyncio
async def test_complete_pagination_and_same_product_across_collections_is_deduplicated():
    monitor = DisneyMadMonitor(FakeHttp([])); monitor.page_size = 2
    one, two = raw(1), raw(2, title="Loungefly Dalmatians Crossbody")
    non_lf = raw(3, title="Disney Backpack", tags=[], description="Official Disney")
    monitor.http.responses = [
        {"products": [one, two]}, {"products": []},
        {"products": [one, non_lf]}, {"products": []},
    ]
    products = await monitor.discover_products()
    assert len(products) == 2
    first = next(product for product in products if product.retailer_product_id == "1")
    assert first.collections == ("backpacks", "disney-parks")
    assert "page=2" in monitor.http.urls[1] and "page=2" in monitor.http.urls[3]


@pytest.mark.asyncio
async def test_cross_retailer_canonical_matching_keeps_independent_offers(tmp_path):
    path = tmp_path / "canonical.db"
    disney = parse(raw(barcode="0671803000001"))
    original = replace(disney, retailer="BoxLunch", retailer_product_id="BL-1",
                       url="https://boxlunch.com/product/BL-1", availability=Availability.OUT_OF_STOCK)
    async with Database(path) as database:
        service = ProductService(database)
        disney_id, original_id = await service.upsert(disney), await service.upsert(original)
        await database.connection.commit()
        rows = await (await database.connection.execute(
            "SELECT id, retailer, canonical_key FROM products ORDER BY id"
        )).fetchall()
        assert disney_id != original_id and rows[0][2] == rows[1][2] == "barcode:0671803000001"


@pytest.mark.parametrize(("status", "kind"), [
    (429, HttpErrorKind.RATE_LIMITED), (500, HttpErrorKind.SERVER),
])
@pytest.mark.asyncio
async def test_http_failures_are_unhealthy_and_targeted_checks_preserve_prior_state(status, kind):
    product = parse(); error = HttpClientError(kind, "failure", status)
    monitor = DisneyMadMonitor(FakeHttp([error, error]))
    assert not await monitor.health_check()
    checked = await monitor.check_product(product)
    assert checked.availability == Availability.ERROR and checked.price == product.price


@pytest.mark.asyncio
async def test_malformed_shopify_data_and_parser_failure_fail_closed():
    monitor = DisneyMadMonitor(FakeHttp([{"bad": True}]))
    with pytest.raises(ShopifyParseError):
        await monitor.discover_products()
    checked = await DisneyMadMonitor(FakeHttp([{"bad": True}])).check_product(parse())
    assert checked.availability == Availability.ERROR


async def result(value): return value


@pytest.mark.asyncio
async def test_initial_sync_new_retailer_listing_restock_and_restart_dedup(tmp_path):
    path = tmp_path / "disney-mad.db"; recorder = Recorder()
    sold = parse(raw(available=False, description="Sold Out <button>Add to Cart</button>"))
    async with Database(path) as database:
        service = MonitorService(
            type("M", (), {"discover_products": lambda self: result([sold])})(),
            database, recorder, retailer_name="Disney Mad",
            price_alerts=PriceAlertConfig(minimum_drop_percent=10, minimum_drop_value=5),
        )
        assert await service.synchronize() == []
        imported = parse(raw(2, title="Loungefly Cars Mini Backpack - BoxLunch Exclusive"))
        service.monitor = type("M", (), {"discover_products": lambda self: result([sold, imported])})()
        alerts = await service.synchronize()
        assert [a.alert_type for a in alerts] == [AlertType.NEW_PRODUCT]
        assert not alerts[0].product.new_release  # New to Disney Mad, not a claimed global release.
        restocked = replace(sold, availability=Availability.IN_STOCK)
        service.monitor = type("M", (), {"discover_products": lambda self: result([restocked, imported])})()
        assert [a.alert_type for a in await service.synchronize()] == [AlertType.RESTOCK]
    async with Database(path) as database:
        service = MonitorService(
            type("M", (), {"discover_products": lambda self: result([restocked, imported])})(),
            database, Recorder(), retailer_name="Disney Mad",
        )
        assert await service.synchronize() == []

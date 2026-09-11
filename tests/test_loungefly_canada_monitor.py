from decimal import Decimal
from pathlib import Path

import pytest

import app.monitors.loungefly_uk as loungefly_module
from app.database import Database
from app.models import Availability, Product, ReleasePrecision
from app.monitors import LoungeflyCanadaMonitor, LoungeflyUSMonitor
from app.monitors.loungefly_uk import LoungeflyParseError
from app.services.monitor_service import MonitorService

FIXTURES = Path(__file__).parent / "fixtures" / "loungefly_canada"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    async def get_text(self, url):
        self.urls.append(url)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_canada_discovery_filters_deduplicates_and_uses_regional_url(monkeypatch):
    monkeypatch.setattr(loungefly_module, "PAGE_SIZE", 4)
    http = FakeHttp([fixture("listing.html"), fixture("page2.html")])
    products = await LoungeflyCanadaMonitor(http).discover_products()

    assert [product.retailer_product_id for product in products] == ["SHARED1", "CAPRE2", "CANEW3"]
    assert "/ca/ca-shop/ca-backpacks/ca-mini-backpacks/?start=0&sz=4" in http.urls[0]
    assert all(product.url.startswith("https://loungefly.com/ca/") for product in products)


def test_canada_uses_displayed_usd_and_normalizes_exclusive_preorder_and_release():
    html = fixture("listing.html")
    raw = LoungeflyCanadaMonitor.parse_listing(html)
    flags = LoungeflyCanadaMonitor.parse_product_flags(html)
    monitor = LoungeflyCanadaMonitor(FakeHttp([]))
    exclusive = monitor.parse_product(raw[0], source_url="https://loungefly.com/ca/", flags=flags["SHARED1"])
    preorder = monitor.parse_product(raw[1], source_url="https://loungefly.com/ca/", flags=flags["CAPRE2"])
    new = monitor.parse_product(raw[2], source_url="https://loungefly.com/ca/", flags=flags["CANEW3"])

    assert (exclusive.currency, exclusive.price, exclusive.exclusive) == ("USD", Decimal("105.00"), True)
    assert (exclusive.availability, preorder.availability, preorder.preorder) == (
        Availability.OUT_OF_STOCK, Availability.PREORDER, True,
    )
    assert preorder.release.precision == ReleasePrecision.EXACT_DATETIME
    assert preorder.release.timezone == "UTC"
    assert new.new_release and new.exclusive and new.release.precision == ReleasePrecision.DATE_ONLY


def test_same_sku_has_independent_market_identity_price_stock_and_url():
    canada_raw = LoungeflyCanadaMonitor.parse_listing(fixture("listing.html"))[0]
    canada = LoungeflyCanadaMonitor(FakeHttp([])).parse_product(
        canada_raw, source_url="https://loungefly.com/ca/", flags={"web exclusive"}
    )
    us_raw = {
        **canada_raw,
        "offers": {
            **canada_raw["offers"], "url": "/shared-character-mini-backpack/SHARED1.html",
            "price": "90.00", "availability": "https://schema.org/InStock",
        },
    }
    us = LoungeflyUSMonitor(FakeHttp([])).parse_product(us_raw, source_url="https://loungefly.com/")

    assert canada.sku == us.sku == "SHARED1"
    assert (canada.retailer, canada.price, canada.availability) == (
        "Loungefly Canada", Decimal("105.00"), Availability.OUT_OF_STOCK,
    )
    assert (us.retailer, us.price, us.availability) == (
        "Loungefly US", Decimal("90.00"), Availability.IN_STOCK,
    )
    assert canada.url != us.url


@pytest.mark.asyncio
async def test_same_sku_is_persisted_as_separate_market_state(tmp_path):
    canada_raw = LoungeflyCanadaMonitor.parse_listing(fixture("listing.html"))[0]
    canada = LoungeflyCanadaMonitor(FakeHttp([])).parse_product(canada_raw, source_url="https://loungefly.com/ca/")
    us = Product(
        "Loungefly US", "SHARED1", canada.name,
        "https://loungefly.com/shared-character-mini-backpack/SHARED1.html",
        Availability.IN_STOCK, price=Decimal("90"), currency="USD", sku="SHARED1",
    )

    class Monitor:
        def __init__(self, products): self.products = products
        async def discover_products(self): return self.products

    class Notifier:
        async def send(self, alert): raise AssertionError("initial synchronization must be silent")

    async with Database(tmp_path / "markets.db") as database:
        await MonitorService(Monitor([us]), database, Notifier(), retailer_name="Loungefly US").synchronize()
        await MonitorService(Monitor([canada]), database, Notifier(), retailer_name="Loungefly Canada").synchronize()
        rows = await (await database.connection.execute(
            """SELECT p.retailer, s.availability, s.price FROM products p
               JOIN product_states s ON s.product_id=p.id WHERE p.retailer_product_id='SHARED1'
               ORDER BY p.retailer"""
        )).fetchall()
        assert rows == [("Loungefly Canada", "OUT_OF_STOCK", "105.00"),
                        ("Loungefly US", "IN_STOCK", "90")]


@pytest.mark.parametrize("html", ["", "<html>changed</html>", fixture("malformed.html")])
def test_canada_malformed_listing_fails(html):
    with pytest.raises(LoungeflyParseError):
        LoungeflyCanadaMonitor.parse_listing(html)


@pytest.mark.asyncio
async def test_canada_parser_failure_returns_error_and_preserves_market_product():
    raw = LoungeflyCanadaMonitor.parse_listing(fixture("listing.html"))[0]
    monitor = LoungeflyCanadaMonitor(FakeHttp(["<html>changed</html>"]))
    original = monitor.parse_product(raw, source_url="https://loungefly.com/ca/", flags={"web exclusive"})
    checked = await monitor.check_product(original)

    assert checked.availability == Availability.ERROR
    assert checked.retailer == "Loungefly Canada"
    assert checked.price == original.price and checked.exclusive

@pytest.mark.asyncio
async def test_canada_new_listing_alerts_only_after_silent_baseline(tmp_path):
    html = fixture("listing.html")
    raw = LoungeflyCanadaMonitor.parse_listing(html)
    flags = LoungeflyCanadaMonitor.parse_product_flags(html)
    adapter = LoungeflyCanadaMonitor(FakeHttp([]))
    products = [adapter.parse_product(item, source_url="https://loungefly.com/ca/",
                                      flags=flags.get(item["sku"], set())) for item in raw[:3]]

    class Monitor:
        def __init__(self): self.products = products[:2]
        async def discover_products(self): return self.products

    class Notifier:
        def __init__(self): self.alerts = []
        async def send(self, alert): self.alerts.append(alert); return True

    async with Database(tmp_path / "new-listings.db") as database:
        monitor = Monitor()
        notifier = Notifier()
        service = MonitorService(monitor, database, notifier, retailer_name="Loungefly Canada")
        assert await service.synchronize() == []
        monitor.products = products
        alerts = await service.synchronize()
        assert [alert.alert_type.value for alert in alerts] == ["NEW_PRODUCT"]
        assert alerts[0].product.retailer_product_id == "CANEW3"

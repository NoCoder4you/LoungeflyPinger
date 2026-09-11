from pathlib import Path
import pytest
import app.monitors.loungefly_uk as module
from app.models import Availability, ReleasePrecision
from app.monitors.loungefly_uk import LoungeflyParseError, LoungeflyUSMonitor

FIXTURES = Path(__file__).parent / "fixtures" / "loungefly_us"
def fixture(name): return (FIXTURES / name).read_text(encoding="utf-8")

class FakeHttp:
    def __init__(self, responses): self.responses=list(responses); self.urls=[]
    async def get_text(self, url): self.urls.append(url); return self.responses.pop(0)

@pytest.mark.asyncio
async def test_us_discovery_filters_deduplicates_and_uses_us_category(monkeypatch):
    monkeypatch.setattr(module, "PAGE_SIZE", 4)
    http=FakeHttp([fixture("listing.html"), fixture("page2.html")])
    products=await LoungeflyUSMonitor(http).discover_products()
    assert [p.retailer_product_id for p in products] == ["USNEW1", "USEX2", "USPRE3"]
    assert "/shop/backpacks/mini-backpacks/?start=0&sz=4" in http.urls[0]

def test_us_structured_fields_flags_stock_price_sku_and_release():
    html=fixture("listing.html"); raw=LoungeflyUSMonitor.parse_listing(html)
    flags=LoungeflyUSMonitor.parse_product_flags(html); monitor=LoungeflyUSMonitor(FakeHttp([]))
    new=monitor.parse_product(raw[0], source_url="https://loungefly.com/", flags=flags["USNEW1"])
    exclusive=monitor.parse_product(raw[1], source_url="https://loungefly.com/", flags=flags["USEX2"])
    preorder=monitor.parse_product(raw[2], source_url="https://loungefly.com/", flags=flags["USPRE3"])
    assert (new.retailer, new.currency, new.price, new.sku, new.new_release) == ("Loungefly US", "USD", 90, "USNEW1", True)
    assert new.release.precision == ReleasePrecision.EXACT_DATETIME
    assert new.release.timezone == "America/Los_Angeles"
    assert exclusive.exclusive and exclusive.availability == Availability.OUT_OF_STOCK
    assert preorder.preorder and preorder.availability == Availability.PREORDER
    assert preorder.release.precision == ReleasePrecision.DATE_ONLY

@pytest.mark.asyncio
async def test_us_product_check_preserves_release_time():
    original=LoungeflyUSMonitor(FakeHttp([])).parse_product(
        LoungeflyUSMonitor.parse_listing(fixture("listing.html"))[2], source_url="https://loungefly.com/")
    checked=await LoungeflyUSMonitor(FakeHttp([fixture("product.html")])).check_product(original)
    assert checked.release.release_time.isoformat() == "10:30:00"

@pytest.mark.parametrize("html", ["", "<html>changed</html>", fixture("malformed.html")])
def test_us_malformed_listing_fails(html):
    with pytest.raises(LoungeflyParseError): LoungeflyUSMonitor.parse_listing(html)

@pytest.mark.asyncio
async def test_us_parser_failure_is_error_and_preserves_product_metadata():
    monitor=LoungeflyUSMonitor(FakeHttp(["<html>changed</html>"]))
    raw=LoungeflyUSMonitor.parse_listing(fixture("listing.html"))[0]
    original=monitor.parse_product(raw, source_url="https://loungefly.com/", flags={"new"})
    checked=await monitor.check_product(original)
    assert checked.availability == Availability.ERROR
    assert checked.price == original.price and checked.new_release

@pytest.mark.asyncio
async def test_us_initial_sync_is_silent_and_persists_products(tmp_path):
    from app.database import Database
    from app.services.monitor_service import MonitorService
    class Notifier:
        def __init__(self): self.alerts=[]
        async def send(self, alert): self.alerts.append(alert); return True
    monitor=LoungeflyUSMonitor(FakeHttp([fixture("listing.html")]))
    async with Database(tmp_path / "us.db") as database:
        notifier=Notifier(); service=MonitorService(monitor, database, notifier, retailer_name="Loungefly US")
        assert await service.synchronize() == []
        assert notifier.alerts == []
        count=await (await database.connection.execute("SELECT COUNT(*) FROM products WHERE retailer='Loungefly US'" )).fetchone()
        assert count == (3,)

def test_us_low_stock_and_coming_soon_normalization():
    raw=LoungeflyUSMonitor.parse_listing(fixture("listing.html"))[0]
    monitor=LoungeflyUSMonitor(FakeHttp([]))
    limited={**raw, "offers": {**raw["offers"], "availability": "https://schema.org/LimitedAvailability"}}
    assert monitor.parse_product(limited, source_url="https://loungefly.com/").availability == Availability.LOW_STOCK
    assert monitor.parse_product(raw, source_url="https://loungefly.com/", flags={"coming soon"}).availability == Availability.COMING_SOON

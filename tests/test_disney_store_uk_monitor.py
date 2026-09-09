from pathlib import Path

import pytest

import app.monitors.disney_store_uk as disney_module
from app.models import Availability
from app.monitors.disney_store_uk import DisneyStoreUKMonitor, DisneyStoreUKParseError

FIXTURES = Path(__file__).parent / "fixtures" / "disney_store_uk"


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
async def test_discovery_filters_deduplicates_and_paginates(monkeypatch):
    monkeypatch.setattr(disney_module, "PAGE_SIZE", 4)
    http = FakeHttp([fixture("listing.html"), fixture("page2.html")])
    products = await DisneyStoreUKMonitor(http).discover_products()

    assert [product.retailer_product_id for product in products] == ["442001", "442002", "442005"]
    assert "start=0&sz=4" in http.urls[0]
    assert "start=4&sz=4" in http.urls[1]


def test_loungefly_identification_and_mini_backpack_filtering():
    assert DisneyStoreUKMonitor._is_mini_backpack({"name": "Loungefly Up Mini Backpack"})
    for name in (
        "Disney Mickey Mini Backpack", "Loungefly Mini Backpack Wallet",
        "Loungefly Mini Backpack Crossbody Bag", "Loungefly Backpack",
        "Loungefly Mini Backpack Pin Set", "Loungefly Mini Backpack Headband",
    ):
        assert not DisneyStoreUKMonitor._is_mini_backpack({"name": name})


def test_product_id_url_image_price_stock_character_and_exclusive():
    raw = DisneyStoreUKMonitor.parse_listing(fixture("listing.html"))[0]
    product = DisneyStoreUKMonitor(FakeHttp([])).parse_product(raw, source_url="https://www.disneystore.co.uk/brands/loungefly")

    assert product.retailer == "Disney Store UK"
    assert product.retailer_product_id == "442001"
    assert product.url == "https://www.disneystore.co.uk/disney-exclusive-loungefly-stitch-mini-backpack-442001.html"
    assert product.image_url == "https://cdn.s7.shopdisney.eu/is/image/DisneyStoreES/442001"
    assert product.price == 85
    assert product.currency == "GBP"
    assert product.availability == Availability.IN_STOCK
    assert product.product_type == "Mini Backpack"
    assert product.character == "Stitch"
    assert product.exclusive is True
    assert product.preorder is False


def test_out_of_stock_is_normalized():
    raw = DisneyStoreUKMonitor.parse_listing(fixture("listing.html"))[1]
    product = DisneyStoreUKMonitor(FakeHttp([])).parse_product(raw, source_url="https://www.disneystore.co.uk/")
    assert product.availability == Availability.OUT_OF_STOCK


@pytest.mark.asyncio
async def test_preorder_product_page_and_coming_soon_are_normalized():
    monitor = DisneyStoreUKMonitor(FakeHttp([]))
    raw = monitor.parse_product_page(fixture("preorder.html"))
    preorder = monitor.parse_product(raw, source_url="https://www.disneystore.co.uk/loungefly-alice-mini-backpack-442006.html")
    coming_raw = DisneyStoreUKMonitor.parse_listing(fixture("page2.html"))[1]
    coming = monitor.parse_product(coming_raw, source_url="https://www.disneystore.co.uk/")
    assert (preorder.availability, preorder.preorder) == (Availability.PREORDER, True)
    assert (coming.availability, coming.preorder, coming.exclusive) == (Availability.COMING_SOON, False, True)


@pytest.mark.parametrize("content", ["", "<html>changed</html>", pytest.param(None, id="non-string")])
def test_malformed_or_missing_expected_listing_data_fails(content):
    with pytest.raises(DisneyStoreUKParseError):
        DisneyStoreUKMonitor.parse_listing(content)


def test_malformed_structured_content_fails():
    with pytest.raises(DisneyStoreUKParseError):
        DisneyStoreUKMonitor.parse_listing(fixture("malformed.html"))


@pytest.mark.parametrize("field", ["id", "variant_id", "name", "price", "availability", "image_url"])
def test_missing_expected_product_data_fails(field):
    raw = DisneyStoreUKMonitor.parse_listing(fixture("listing.html"))[0]
    raw["product"].pop(field)
    with pytest.raises(DisneyStoreUKParseError):
        DisneyStoreUKMonitor(FakeHttp([])).parse_product(raw, source_url="https://www.disneystore.co.uk/")


@pytest.mark.asyncio
async def test_parser_failure_returns_error_not_out_of_stock():
    monitor = DisneyStoreUKMonitor(FakeHttp([]))
    raw = monitor.parse_listing(fixture("listing.html"))[0]
    original = monitor.parse_product(raw, source_url="https://www.disneystore.co.uk/")
    checked = await DisneyStoreUKMonitor(FakeHttp(["<html>changed</html>"])).check_product(original)
    assert checked.availability == Availability.ERROR

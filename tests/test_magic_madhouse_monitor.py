from datetime import date
from pathlib import Path

import pytest

from app.models import Availability, ReleasePrecision
from app.monitors.magic_madhouse import MagicMadhouseMonitor, MagicMadhouseParseError

FIXTURES = Path(__file__).parent / "fixtures" / "magic_madhouse"


def fixture(name):
    return (FIXTURES / name).read_text()


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    async def get_text(self, url):
        self.urls.append(url)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_isolated_search_pagination_filtering_stock_preorder_and_deduplication():
    http = FakeHttp([fixture("listing.html"), fixture("page2.html")])
    products = await MagicMadhouseMonitor(http).discover_products()
    assert [product.retailer_product_id for product in products] == ["101", "102", "103", "107"]
    assert "search_query=Mini+Backpack" in http.urls[0] and "brand=43" in http.urls[0]
    assert products[0].availability == Availability.IN_STOCK
    assert products[1].availability == Availability.OUT_OF_STOCK
    assert products[2].availability == Availability.PREORDER and products[2].preorder


def test_price_sku_release_and_eta_are_distinct():
    raw = MagicMadhouseMonitor.parse_listing(fixture("listing.html"))[0][0]
    product = MagicMadhouseMonitor(FakeHttp([])).parse_product(raw)
    assert product.sku == "LF-101"
    assert str(product.price) == "49.85" and str(product.original_price) == "65"
    assert product.release.precision == ReleasePrecision.EXACT_DATETIME
    assert product.release.release_datetime.isoformat() == "2026-09-25T09:00:00+01:00"
    assert product.estimated_arrival_date == date(2026, 9, 20)
    assert product.release.release_date != product.estimated_arrival_date


def test_delivery_month_and_generic_bigcommerce_release_date_are_not_release_dates():
    raw = MagicMadhouseMonitor.parse_listing(fixture("listing.html"))[0][2]
    raw["release_date"] = "Available: 19th Jan 2038"
    product = MagicMadhouseMonitor(FakeHttp([])).parse_product(raw)
    assert product.release is None
    assert product.estimated_arrival_date is None


@pytest.mark.parametrize("index", [3, 4, 5])
def test_reliable_mini_backpack_classification_rejects_false_positives(index):
    raw = MagicMadhouseMonitor.parse_listing(fixture("listing.html"))[0][index]
    assert not MagicMadhouseMonitor._is_loungefly_mini_backpack(raw)


@pytest.mark.parametrize("html", ["", "<html></html>", pytest.param(None),])
def test_malformed_listing_data(html):
    with pytest.raises(MagicMadhouseParseError):
        MagicMadhouseMonitor.parse_listing(html)


def test_malformed_product_data():
    raw = MagicMadhouseMonitor.parse_listing(fixture("listing.html"))[0][0]
    raw["stock_level"] = "many"
    with pytest.raises(MagicMadhouseParseError):
        MagicMadhouseMonitor(FakeHttp([])).parse_product(raw)


@pytest.mark.asyncio
async def test_check_failure_does_not_report_false_stock():
    product = MagicMadhouseMonitor(FakeHttp([])).parse_product(
        MagicMadhouseMonitor.parse_listing(fixture("listing.html"))[0][0]
    )
    checked = await MagicMadhouseMonitor(FakeHttp([fixture("malformed.html")])).check_product(product)
    assert checked.availability == Availability.ERROR

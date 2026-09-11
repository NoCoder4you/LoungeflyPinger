from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.models import Availability, ReleasePrecision
from app.monitors.entertainment_earth import (
    EntertainmentEarthMonitor, EntertainmentEarthParseError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "entertainment_earth"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeHttp:
    def __init__(self, responses): self.responses = list(responses); self.urls = []
    async def get_text(self, url): self.urls.append(url); return self.responses.pop(0)


@pytest.mark.asyncio
async def test_discovery_scans_loungefly_and_exclusive_pages_and_filters_charms():
    http = FakeHttp([fixture("listing.html"), fixture("listing.html")])
    products = await EntertainmentEarthMonitor(http).discover_products()
    assert [product.retailer_product_id for product in products] == ["LFST100"]
    assert "/s/loungefly/c?page=1" in http.urls[0]
    assert "/s/entertainment-earth-exclusives/c?page=1" in http.urls[1]


def raw_product(status="In Stock", availability="InStock", description="", price="69.99"):
    return {
        "@type": "Product", "name": "Loungefly Star Wars Mini Backpack", "sku": "LFSW123",
        "brand": {"name": "Loungefly"}, "image": "https://images.example/bag.jpg",
        "description": description, "availabilityText": status,
        "offers": {"price": price, "priceCurrency": "USD",
                   "availability": f"https://schema.org/{availability}",
                   "url": "/product/star-wars-mini-backpack/lfsw123"},
    }


def test_exclusive_in_stock_item_number_price_and_estimated_ship_date():
    raw = EntertainmentEarthMonitor.parse_listing(fixture("listing.html"))[0]
    product = EntertainmentEarthMonitor(FakeHttp([])).parse_product(raw, source_url="https://www.entertainmentearth.com/")
    assert product.retailer == "Entertainment Earth" and product.currency == "USD"
    assert product.exclusive and product.exclusive_retailer == "Entertainment Earth"
    assert product.availability == Availability.IN_STOCK
    assert product.retailer_product_id == product.sku == "LFST100"
    assert product.price == Decimal("79.99")
    assert product.estimated_ship_date == date(2026, 10, 15)
    assert product.release is None  # Shipping is not release evidence.


@pytest.mark.parametrize(("status", "expected", "preorder"), [
    ("Sold Out", Availability.OUT_OF_STOCK, False),
    ("Pre-Sold Out", Availability.OUT_OF_STOCK, True),
    ("Preorder", Availability.PREORDER, True),
    ("In Stock", Availability.IN_STOCK, False),
])
def test_retailer_availability_states(status, expected, preorder):
    product = EntertainmentEarthMonitor(FakeHttp([])).parse_product(
        raw_product(status=status), source_url="https://www.entertainmentearth.com/"
    )
    assert product.availability == expected
    assert product.preorder is preorder


def test_real_backpack_is_kept_but_mini_backpack_charm_is_filtered():
    products = EntertainmentEarthMonitor.parse_listing(fixture("listing.html"))
    assert EntertainmentEarthMonitor._is_loungefly_mini_backpack(products[0])
    assert not EntertainmentEarthMonitor._is_loungefly_mini_backpack(products[1])


def test_release_date_is_distinct_from_estimated_ship_date_and_price_parses():
    raw, text = EntertainmentEarthMonitor.parse_product_page(fixture("product.html"))
    product = EntertainmentEarthMonitor(FakeHttp([])).parse_product(raw, source_url="https://www.entertainmentearth.com/", page_text=text)
    assert product.availability == Availability.OUT_OF_STOCK and product.preorder
    assert product.price == Decimal("74.99")
    assert product.estimated_ship_date == date(2026, 10, 15)
    assert product.release.precision == ReleasePrecision.EXACT_DATETIME
    assert product.release.release_date == date(2026, 10, 1)


@pytest.mark.parametrize("html", ["", "<html>changed</html>", "<script type='application/ld+json'>{bad</script>"])
def test_listing_parser_errors(html):
    with pytest.raises(EntertainmentEarthParseError):
        EntertainmentEarthMonitor.parse_listing(html)


@pytest.mark.asyncio
async def test_check_product_parser_error_preserves_prior_metadata():
    original = EntertainmentEarthMonitor(FakeHttp([])).parse_product(
        raw_product(description="Estimated Ship Date: 12/01/2026"),
        source_url="https://www.entertainmentearth.com/",
    )
    failed = await EntertainmentEarthMonitor(FakeHttp(["<html>changed</html>"])).check_product(original)
    assert failed.availability == Availability.ERROR
    assert failed.price == original.price
    assert failed.estimated_ship_date == original.estimated_ship_date

from dataclasses import replace
from datetime import date

import pytest

from app.models import Availability, ReleasePrecision
from app.monitors.forbidden_planet import ForbiddenPlanetMonitor, ForbiddenPlanetParseError


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    async def get_json(self, url):
        self.urls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def product(product_id=10, *, title="LF Disney Castle Mini Backpack", available=True,
            price=7999, compare_at_price=None, tags=None, description="", handle=None):
    return {
        "id": product_id, "title": title, "handle": handle or f"bag-{product_id}",
        "description": description, "published_at": "2026-09-01T10:00:00+01:00",
        "vendor": "Forbidden Planet Int", "type": "Loungefly",
        "product_type": "Loungefly", "tags": tags or ["loungefly"],
        "variants": [{"id": product_id * 100, "available": available, "price": price,
                      "compare_at_price": compare_at_price, "sku": f"SKU-{product_id}",
                      "barcode": f"BAR-{product_id}"}],
        "images": [f"https://cdn.example/{product_id}.jpg"],
    }


@pytest.mark.asyncio
async def test_discovery_paginates_details_and_deduplicates_categories(monkeypatch):
    monkeypatch.setattr("app.monitors.forbidden_planet.PAGE_SIZE", 2)
    one, two = product(10), product(20, title="LF Star Wars Mini Backpack")
    duplicate = product(10, tags=["loungefly", "Disney", "new in"])
    http = FakeHttp([
        {"products": [one, two]}, one, two,
        {"products": [duplicate]}, duplicate,
    ])
    products = await ForbiddenPlanetMonitor(http).discover_products()
    assert [item.retailer_product_id for item in products] == ["10", "20"]
    assert http.urls[0].endswith("?limit=2&page=1")
    assert http.urls[3].endswith("?limit=2&page=2")
    assert all("/products/bag-" in url and url.endswith(".js") for url in (http.urls[1], http.urls[2], http.urls[4]))
    assert products[0].retailer == "Forbidden Planet International UK"
    assert products[0].currency == "GBP"
    assert products[0].variant_id == "1000"


@pytest.mark.parametrize("name,expected", [
    ("LF Disney Mini Backpack", True),
    ("LF Mini Backpack Keychain", False),
    ("LF Disney Crossbody Bag", False),
    ("LF Disney Wallet", False),
])
def test_mini_backpack_filtering(name, expected):
    assert ForbiddenPlanetMonitor._is_mini_backpack(product(title=name)) is expected


def test_filter_requires_loungefly_product_classification():
    raw = product()
    raw["product_type"] = raw["type"] = "Accessories"
    raw["tags"] = ["Disney"]
    assert not ForbiddenPlanetMonitor._is_mini_backpack(raw)


def test_in_stock_price_sale_new_product_and_ids():
    parsed = ForbiddenPlanetMonitor(FakeHttp([])).parse_product(
        product(price=5999, compare_at_price=7999, tags=["loungefly", "new arrivals"])
    )
    assert parsed.availability == Availability.IN_STOCK
    assert str(parsed.price) == "59.99"
    assert str(parsed.original_price) == "79.99"
    assert parsed.new_release is True
    assert parsed.retailer_product_id == "10" and parsed.variant_id == "1000"


def test_out_of_stock():
    parsed = ForbiddenPlanetMonitor(FakeHttp([])).parse_product(product(available=False))
    assert parsed.availability == Availability.OUT_OF_STOCK


def test_preorder_and_exact_release_date():
    parsed = ForbiddenPlanetMonitor(FakeHttp([])).parse_product(product(
        description="Pre-order now. Release date: 24 September 2026"
    ))
    assert parsed.preorder is True
    assert parsed.availability == Availability.PREORDER
    assert parsed.release is not None
    assert parsed.release.precision == ReleasePrecision.DATE_ONLY
    assert parsed.release.release_date == date(2026, 9, 24)


def test_month_release_tag_does_not_invent_a_day():
    parsed = ForbiddenPlanetMonitor(FakeHttp([])).parse_product(
        product(tags=["loungefly", "sep-26"])
    )
    assert parsed.release is not None
    assert parsed.release.precision == ReleasePrecision.MONTH_ONLY
    assert (parsed.release.release_month, parsed.release.release_year) == (9, 2026)
    assert parsed.release.release_date is None


@pytest.mark.asyncio
async def test_check_product_detects_restock_state():
    monitor = ForbiddenPlanetMonitor(FakeHttp([product(available=True)]))
    old = monitor.parse_product(product(available=False))
    checked = await monitor.check_product(old)
    assert old.availability == Availability.OUT_OF_STOCK
    assert checked.availability == Availability.IN_STOCK


@pytest.mark.parametrize("raw", [None, {}, {"products": "not a list"}])
def test_listing_parser_failure(raw):
    with pytest.raises(ForbiddenPlanetParseError):
        ForbiddenPlanetMonitor._product_list(raw)


@pytest.mark.asyncio
async def test_product_parser_failure_is_error_not_out_of_stock():
    monitor = ForbiddenPlanetMonitor(FakeHttp([{"id": 10}]))
    current = monitor.parse_product(product())
    checked = await monitor.check_product(replace(current, availability=Availability.IN_STOCK))
    assert checked.availability == Availability.ERROR

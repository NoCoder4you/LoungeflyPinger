from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.models import Availability, ReleasePrecision
from app.monitors.emp import EMPMonitor, EMPParseError, EMP_REGIONS

FIXTURES = Path(__file__).parent / "fixtures" / "emp"


class FakeHttp:
    def __init__(self, responses): self.responses = list(responses); self.urls = []
    async def get_text(self, url): self.urls.append(url); return self.responses.pop(0)


def fixture(country):
    return (FIXTURES / f"product_{country}.html").read_text(encoding="utf-8")


@pytest.mark.parametrize(("country", "article", "localized_date"), [
    ("de", "60001", "18.09.2026"), ("fr", "60002", "18/09/2026"),
    ("es", "60003", "18/9/26"), ("it", "60004", "18/09/2026"),
])
def test_each_locale_uses_structured_release_date_article_price_and_low_stock(country, article, localized_date):
    html = fixture(country)
    assert localized_date in html  # The human-readable locale format is deliberately different.
    monitor = EMPMonitor(FakeHttp([]), country)
    product = monitor.parse_product(monitor.parse_product_page(html), source_url=monitor.region.base_url)
    assert product.retailer_product_id == product.sku == article
    assert product.currency == "EUR" and product.price == Decimal("59.99")
    assert product.original_price == Decimal("74.99")
    assert product.availability == Availability.LOW_STOCK
    assert product.release.precision == ReleasePrecision.DATE_ONLY
    assert product.release.release_date == date(2026, 9, 18)
    assert product.release.release_time is None and product.release.release_datetime is None


@pytest.mark.parametrize("country", EMP_REGIONS)
def test_mini_backpack_category_is_localized(country):
    monitor = EMPMonitor(FakeHttp([]), country)
    raw = monitor.parse_product_page(fixture(country))
    assert monitor._is_mini_backpack(raw)


def test_listing_filters_non_mini_products_and_discovers_product():
    tile = fixture("fr").replace('id="pdpMain" data-pid="60002"', 'class="product-tile" data-itemid="60002" data-url="/p/loungefly/60002.html"')
    products = EMPMonitor.parse_listing(tile)
    assert len(products) == 1
    assert EMPMonitor(FakeHttp([]), "fr")._is_mini_backpack(products[0])


@pytest.mark.parametrize("html", ["", "<html>changed</html>", "not html"])
def test_malformed_product_page(html):
    with pytest.raises(EMPParseError):
        EMPMonitor.parse_product_page(html)


@pytest.mark.parametrize(("schema_state", "localized_text", "expected", "preorder"), [
    ("OutOfStock", "Ausverkauft", Availability.OUT_OF_STOCK, False),
    ("PreOrder", "Précommande", Availability.PREORDER, True),
    ("PreOrder", "Preventa", Availability.PREORDER, True),
    ("PreOrder", "Preordine", Availability.PREORDER, True),
])
def test_stock_and_preorder_normalization_across_languages(schema_state, localized_text, expected, preorder):
    html = fixture("fr").replace("InStock", schema_state).replace("Plus que 2 articles", localized_text)
    monitor = EMPMonitor(FakeHttp([]), "fr")
    product = monitor.parse_product(monitor.parse_product_page(html), source_url=monitor.region.base_url)
    assert product.availability == expected
    assert product.preorder is preorder


@pytest.mark.asyncio
async def test_product_failure_is_scoped_to_its_region():
    monitor = EMPMonitor(FakeHttp([fixture("de")]), "de")
    original = monitor.parse_product(monitor.parse_product_page(fixture("de")), source_url=monitor.region.base_url)
    failed = await EMPMonitor(FakeHttp(["<html>changed</html>"]), "de").check_product(original)
    assert failed.availability == Availability.ERROR
    assert await EMPMonitor(FakeHttp([fixture("fr")]), "fr").health_check() is False  # PDP is not a listing.
    assert failed.retailer == "EMP Germany"

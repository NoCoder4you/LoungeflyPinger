from pathlib import Path

import pytest

from app.models import Availability, ReleasePrecision
from app.monitors.merchoid_uk import MerchoidUKMonitor, MerchoidUKParseError

FIXTURES = Path(__file__).parent / "fixtures" / "merchoid_uk"


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
async def test_pagination_filtering_large_catalogue_deduplication_and_new_listing():
    http = FakeHttp([fixture("listing.html"), fixture("page2.html")])
    products = await MerchoidUKMonitor(http).discover_products()
    assert [p.retailer_product_id for p in products] == ["101", "104"]
    assert http.urls == ["https://www.merchoid.com/uk/loungefly/mini-backpacks/",
                         "https://www.merchoid.com/uk/loungefly/mini-backpacks/?p=2"]
    assert products[0].price.as_tuple().exponent == -2
    assert str(products[0].price) == "49.99"
    assert str(products[0].original_price) == "64.99"
    assert products[1].availability == Availability.PREORDER and products[1].preorder
    assert products[1].currency == "GBP"


@pytest.mark.parametrize("name", [
    "Loungefly Full Size Backpack", "Loungefly Convertible Mini Backpack",
    "Loungefly Mini Backpack Charm", "Loungefly Mini Backpack Keychain",
])
def test_mini_backpack_filter_rejects_false_positives(name):
    assert not MerchoidUKMonitor._is_mini_backpack(name)


def test_regular_and_special_price_are_separate():
    card = MerchoidUKMonitor.parse_listing(fixture("page2.html"))[0][0]
    product = MerchoidUKMonitor(FakeHttp([])).parse_card(card)
    assert str(product.price) == "49.99"
    assert str(product.original_price) == "64.99"


def test_preorder_release_metadata_and_stock_detail():
    monitor = MerchoidUKMonitor(FakeHttp([]))
    product = monitor.parse_detail(fixture("product.html"), source_url="https://www.merchoid.com/uk/coraline/")
    assert product.availability == Availability.PREORDER and product.preorder
    assert product.sku == "LF-COR-1"
    assert product.release.precision == ReleasePrecision.EXACT_DATETIME
    assert product.release.release_datetime.isoformat() == "2026-09-25T09:00:00+01:00"
    sold_out = monitor.parse_detail(fixture("out_of_stock.html"), source_url=product.url)
    assert sold_out.availability == Availability.OUT_OF_STOCK


@pytest.mark.parametrize("html", ["", "<html></html>", fixture("malformed.html")])
def test_parser_failure(html):
    with pytest.raises(MerchoidUKParseError):
        MerchoidUKMonitor.parse_listing(html)


@pytest.mark.asyncio
async def test_detail_parser_failure_is_error_not_false_stock():
    base = MerchoidUKMonitor(FakeHttp([])).parse_detail(
        fixture("product.html"), source_url="https://www.merchoid.com/uk/coraline/"
    )
    checked = await MerchoidUKMonitor(FakeHttp([fixture("malformed.html")])).check_product(base)
    assert checked.availability == Availability.ERROR

@pytest.mark.asyncio
async def test_large_catalogue_deduplicates_by_stable_magento_product_id():
    card = '<li class="product-item"><a class="product-item-link" href="/uk/stitch/" title="Loungefly Stitch Mini Backpack">Stitch</a><img class="product-image-photo" src="/stitch.jpg"><div data-product-id="999"><span data-price-type="finalPrice" data-price-amount="59.99"></span></div></li>'
    products = await MerchoidUKMonitor(FakeHttp([f"<ol>{card * 500}</ol>"])).discover_products()
    assert len(products) == 1
    assert products[0].retailer_product_id == "999"

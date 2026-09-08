from dataclasses import replace
from pathlib import Path

import pytest

from app.models import Availability
from app.monitors.truffleshuffle import TruffleShuffleMonitor, TruffleShuffleParseError

FIXTURES = Path(__file__).parent / "fixtures" / "truffleshuffle"


def fixture(name):
    return (FIXTURES / name).read_text()


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    async def get_text(self, url):
        self.urls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.asyncio
async def test_discovery_paginates_filters_and_deduplicates():
    http = FakeHttp([fixture("listing.html"), fixture("page2.html")])
    products = await TruffleShuffleMonitor(http).discover_products()
    assert [product.retailer_product_id for product in products] == ["100", "300"]
    assert http.urls == ["https://www.truffleshuffle.co.uk/loungefly",
                         "https://www.truffleshuffle.co.uk/loungefly?page=2"]
    first = products[0]
    assert first.retailer == "TruffleShuffle"
    assert first.name == "Loungefly Disney Stitch Mini Backpack"
    assert first.url == "https://www.truffleshuffle.co.uk/product/100/loungefly-disney-stitch-mini-backpack"
    assert first.image_url == "https://cdn.example/stitch-new.jpg"
    assert str(first.price) == "69.99"
    assert first.currency == "GBP"
    assert first.availability == Availability.OUT_OF_STOCK
    assert first.product_type == "Mini Backpack"
    assert first.franchise is None and first.character is None
    assert first.exclusive is True


@pytest.mark.parametrize("name", [
    "Loungefly Minnie Wallet", "Loungefly Mini Backpack Keychain", "Loungefly Mini Backpack Charm",
    "Loungefly Minnie Crossbody Bag", "Loungefly Mystery Box Pin", "Loungefly Logo T-Shirt",
])
def test_product_filter_excludes_non_backpacks(name):
    assert not TruffleShuffleMonitor._is_mini_backpack({"name": name})


def test_name_requires_loungefly_and_mini_backpack():
    assert TruffleShuffleMonitor._is_mini_backpack(
        {"name": "Loungefly Disney Alice Mini-Backpack"}
    )
    assert not TruffleShuffleMonitor._is_mini_backpack({"name": "Disney Alice Mini Backpack"})


def test_preorder_and_detail_url_parsing():
    raw = TruffleShuffleMonitor.parse_listing(fixture("preorder.html"))[0][0][0]
    product = TruffleShuffleMonitor(FakeHttp([])).parse_product(
        raw, source_url="https://www.truffleshuffle.co.uk/product/200/coraline"
    )
    assert product.retailer_product_id == "200"
    assert product.url.endswith("/product/200/coraline")
    assert product.image_url == "https://cdn.example/coraline.jpg"
    assert product.price.as_tuple().exponent == -2
    assert product.availability == Availability.PREORDER
    assert product.preorder is True


@pytest.mark.parametrize("html", ["", "<html></html>", fixture("malformed.html")])
def test_malformed_or_missing_structured_data_is_parser_error(html):
    with pytest.raises(TruffleShuffleParseError):
        TruffleShuffleMonitor.parse_listing(html)


@pytest.mark.parametrize("field", ["sku", "name", "image"])
def test_missing_required_product_data_is_parser_error(field):
    raw = TruffleShuffleMonitor.parse_listing(fixture("preorder.html"))[0][0][0]
    raw.pop(field)
    with pytest.raises(TruffleShuffleParseError):
        TruffleShuffleMonitor(FakeHttp([])).parse_product(
            raw, source_url="https://www.truffleshuffle.co.uk/product/200/coraline"
        )


@pytest.mark.parametrize("field", ["price", "priceCurrency", "availability"])
def test_missing_offer_data_is_parser_error(field):
    raw = TruffleShuffleMonitor.parse_listing(fixture("preorder.html"))[0][0][0]
    raw["offers"].pop(field)
    with pytest.raises(TruffleShuffleParseError):
        TruffleShuffleMonitor(FakeHttp([])).parse_product(
            raw, source_url="https://www.truffleshuffle.co.uk/product/200/coraline"
        )


@pytest.mark.asyncio
async def test_check_parser_failure_is_error_not_out_of_stock():
    monitor = TruffleShuffleMonitor(FakeHttp([fixture("malformed.html")]))
    raw = TruffleShuffleMonitor.parse_listing(fixture("preorder.html"))[0][0][0]
    original = monitor.parse_product(
        raw, source_url="https://www.truffleshuffle.co.uk/product/200/coraline"
    )
    checked = await monitor.check_product(replace(original, availability=Availability.IN_STOCK))
    assert checked.availability == Availability.ERROR

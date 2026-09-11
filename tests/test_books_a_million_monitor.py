import pytest

from app.models import Availability, Product
from app.monitors.books_a_million import BooksAMillionMonitor, BooksAMillionParseError


class FakeHttp:
    def __init__(self, responses): self.responses = list(responses); self.urls = []
    async def get_text(self, url): self.urls.append(url); return self.responses.pop(0)


CLOUDFLARE = """<!doctype html><title>Just a moment...</title>
<script src="https://challenges.cloudflare.com/orchestrate/chl_page/v1"></script>"""


@pytest.mark.asyncio
async def test_discovery_classifies_cloudflare_instead_of_returning_empty_catalogue():
    monitor = BooksAMillionMonitor(FakeHttp([CLOUDFLARE]))
    with pytest.raises(BooksAMillionParseError, match="Cloudflare browser challenge"):
        await monitor.discover_products()


@pytest.mark.parametrize("content", ["", None, "<html>unverified product markup</html>"])
def test_empty_malformed_or_unverified_content_fails_closed(content):
    with pytest.raises(BooksAMillionParseError):
        BooksAMillionMonitor.parse_listing(content)


@pytest.mark.asyncio
async def test_parser_failure_preserves_previous_product_as_error():
    product = Product(
        "Books-A-Million", "123", "Loungefly Example Mini Backpack",
        "https://www.booksamillion.com/p/example/123", Availability.IN_STOCK,
        price="79.99", currency="USD", exclusive=True,
        exclusive_retailer="Books-A-Million", sku="123",
    )
    checked = await BooksAMillionMonitor(FakeHttp([CLOUDFLARE])).check_product(product)
    assert checked.availability == Availability.ERROR
    assert checked.price == product.price
    assert checked.exclusive_retailer == "Books-A-Million"


@pytest.mark.asyncio
async def test_health_check_reports_blocked_storefront():
    assert not await BooksAMillionMonitor(FakeHttp([CLOUDFLARE])).health_check()

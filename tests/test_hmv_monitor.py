from pathlib import Path

import pytest

from app.browser import BrowserPage, PageStatus, classify_page
from app.models import Availability
from app.monitors.hmv import HMVMonitor, HMVMonitorError, HMVParseError

FIXTURES = Path(__file__).parent / "fixtures" / "hmv"


class Browser:
    def __init__(self, result): self.result = result
    async def fetch(self, _url): return self.result


def html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_product_parsing_filtering_price_stock_preorder_and_exclusive() -> None:
    monitor = HMVMonitor(Browser(None))
    raw = monitor.parse_products(html("listing.html"))
    selected = [monitor.parse_product(item, source_url="https://hmv.com/search") for item in raw if monitor.is_mini_backpack(item)]

    assert len(selected) == 2
    assert selected[0].retailer == "HMV"
    assert selected[0].price.__str__() == "74.99"
    assert selected[0].availability == Availability.IN_STOCK
    assert selected[0].exclusive is True
    assert selected[1].availability == Availability.PREORDER
    assert selected[1].preorder is True
    assert all(item.currency == "GBP" for item in selected)


@pytest.mark.parametrize("name", [
    "Loungefly Mini Backpack Wallet", "Loungefly Mini Backpack Crossbody Bag",
    "Loungefly Mini Backpack Pin", "Generic Mini Backpack",
])
def test_non_target_products_are_excluded(name: str) -> None:
    assert HMVMonitor.is_mini_backpack({"name": name}) is False


def test_malformed_rendered_html_is_parser_failure() -> None:
    with pytest.raises(HMVParseError, match="no Product"):
        HMVMonitor.parse_products(html("malformed.html"))


def test_cloudflare_and_access_denied_are_distinct() -> None:
    assert classify_page(html("challenge.html"), http_status=403) == PageStatus.CLOUDFLARE_CHALLENGE
    assert classify_page(html("access_denied.html"), http_status=403) == PageStatus.ACCESS_DENIED
    assert classify_page("<html>products</html>", http_status=200) == PageStatus.PAGE_LOADED


@pytest.mark.asyncio
async def test_challenge_prevents_discovery_instead_of_reporting_stock() -> None:
    monitor = HMVMonitor(Browser(BrowserPage(PageStatus.CLOUDFLARE_CHALLENGE, "https://hmv.com")))
    with pytest.raises(HMVMonitorError) as error:
        await monitor.discover_products()
    assert error.value.status == PageStatus.CLOUDFLARE_CHALLENGE


@pytest.mark.asyncio
async def test_parser_error_is_explicit() -> None:
    monitor = HMVMonitor(Browser(BrowserPage(PageStatus.PAGE_LOADED, "https://hmv.com", html("malformed.html"))))
    with pytest.raises(HMVMonitorError) as error:
        await monitor.discover_products()
    assert error.value.status == PageStatus.PARSER_ERROR

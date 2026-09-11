from datetime import date
from decimal import Decimal

import pytest

from app.models import Availability, ReleasePrecision
from app.monitors.popcultcha import PopcultchaMonitor, PopcultchaParseError


class FakeHttp:
    def __init__(self, responses): self.responses = list(responses); self.urls = []
    async def get_text(self, url): self.urls.append(url); return self.responses.pop(0)


def page(*, name="Loungefly Disney Stitch Mini Backpack", sku="LF-STITCH", price="129.99",
         status="InStock", body="In Stock", manufacturer="Loungefly", barcode="671803999999",
         description=""):
    return f"""<html><script type="application/ld+json">{{
      "@type":"Product", "name":"{name}", "sku":"{sku}",
      "brand":{{"@type":"Brand","name":"{manufacturer}"}},
      "gtin13":"{barcode}", "image":"https://cdn.example/bag.jpg",
      "url":"/stitch-mini-backpack.html", "description":"{description}",
      "offers":{{"price":"{price}","priceCurrency":"AUD",
                 "availability":"https://schema.org/{status}"}}
    }}</script><main>{body} Manufacturer: {manufacturer} SKU: {sku} Barcode: {barcode}</main></html>"""


def listing():
    return """<a href="/stitch-mini-backpack.html">Loungefly Stitch Mini Backpack</a>
      <a href="/wallet.html">Loungefly Stitch Wallet</a>"""


def parse(**kwargs):
    return PopcultchaMonitor(FakeHttp([])).parse_product_page(page(**kwargs), "https://www.popcultcha.com.au/item.html")


def test_normal_in_stock_product_captures_aud_sku_barcode_and_manufacturer():
    product = parse()
    assert product.availability == Availability.IN_STOCK
    assert product.price == Decimal("129.99") and product.currency == "AUD"
    assert product.retailer_product_id == product.sku == "LF-STITCH"
    assert product.barcode == "671803999999"
    assert product.vendor == "Loungefly"


def test_explicit_preorder_overrides_coexisting_magento_in_stock_text():
    product = parse(body="In Stock PRE-ORDER")
    assert product.availability == Availability.PREORDER
    assert product.preorder is True


def test_preorder_eta_is_stored_as_estimate_and_not_as_release_date():
    product = parse(body="In Stock PREORDER ETA: 30/11/2026")
    assert product.availability == Availability.PREORDER
    assert product.estimated_ship_date == date(2026, 11, 30)
    assert product.release is None


def test_actual_release_is_captured_separately_from_eta():
    product = parse(body="PREORDER ETA: 30/11/2026. Releases 15 November 2026")
    assert product.estimated_ship_date == date(2026, 11, 30)
    assert product.release.precision == ReleasePrecision.DATE_ONLY
    assert product.release.release_date == date(2026, 11, 15)


def test_sold_out_preorder_is_not_reported_available():
    product = parse(body="PRE-ORDER Sold Out ETA: 30/11/2026", status="OutOfStock")
    assert product.availability == Availability.OUT_OF_STOCK
    assert product.preorder is True


def test_non_loungefly_and_other_merchandise_are_filtered():
    monitor = PopcultchaMonitor(FakeHttp([]))
    assert monitor._is_loungefly_mini_backpack(parse())
    assert not monitor._is_loungefly_mini_backpack(parse(name="Loungefly Stitch Wallet"))
    assert not monitor._is_loungefly_mini_backpack(parse(manufacturer="Funko"))


@pytest.mark.asyncio
async def test_discovery_uses_catalogue_and_product_pages():
    http = FakeHttp([listing(), page()])
    products = await PopcultchaMonitor(http).discover_products()
    assert [product.sku for product in products] == ["LF-STITCH"]
    assert http.urls[0].endswith("/shop-by/manufacturer/loungefly.html")


@pytest.mark.asyncio
async def test_stock_check_supports_restock_observation():
    monitor = PopcultchaMonitor(FakeHttp([page(body="In Stock", status="InStock")]))
    old = parse(body="Out of Stock", status="OutOfStock")
    refreshed = await monitor.check_product(old)
    assert old.availability == Availability.OUT_OF_STOCK
    assert refreshed.availability == Availability.IN_STOCK


@pytest.mark.parametrize("html", ["", "<html>changed</html>",
                                   "<script type='application/ld+json'>{bad</script>"])
def test_malformed_product_page_is_rejected(html):
    with pytest.raises(PopcultchaParseError):
        PopcultchaMonitor(FakeHttp([])).parse_product_page(html, "https://www.popcultcha.com.au/x.html")


@pytest.mark.asyncio
async def test_malformed_check_is_error_not_false_stock_change():
    original = parse()
    checked = await PopcultchaMonitor(FakeHttp(["<html>changed</html>"])).check_product(original)
    assert checked.availability == Availability.ERROR
    assert checked.price == original.price

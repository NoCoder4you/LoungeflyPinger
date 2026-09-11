from datetime import date
from decimal import Decimal

import pytest

from app.models import Availability, ReleasePrecision
from app.monitors import lf_lovers
from app.monitors.lf_lovers import LFLoversMonitor


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    async def get_json(self, url):
        self.urls.append(url)
        return self.responses.pop(0)


def raw(product_id=101, *, title="Loungefly Disney Hero Mini Backpack", available=True,
        price="79.99", compare=None, body="", tags=None):
    return {
        "id": product_id, "title": title, "handle": f"bag-{product_id}",
        "body_html": body, "vendor": "Loungefly", "product_type": "LF",
        "tags": tags or ["Backpack"], "published_at": "2026-09-01T09:00:00Z",
        "images": [{"src": "//cdn.shopify.com/bag.jpg"}],
        "variants": [{"id": product_id * 10, "sku": f"LF-{product_id}",
                      "barcode": f"500{product_id}", "available": available,
                      "price": price, "compare_at_price": compare}],
    }


def parse(value):
    return LFLoversMonitor(FakeHttp([])).parse_product(value)


def test_preorder_title_normalization_is_not_immediate_stock():
    for spelling in ("Preorder", "Pre-order"):
        product = parse(raw(title=f"Loungefly Disney Hero Mini Backpack - {spelling}"))
        assert product.preorder is True
        assert product.availability == Availability.PREORDER


def test_normal_stock_and_structured_product_identity():
    product = parse(raw())
    assert product.availability == Availability.IN_STOCK
    assert product.retailer_product_id == "101"
    assert product.variant_id == "1010"
    assert product.sku == "LF-101" and product.barcode == "500101"
    assert product.currency == "GBP"


def test_sold_out_and_sale_price():
    product = parse(raw(102, available=False, price="54.99", compare="74.99"))
    assert product.availability == Availability.OUT_OF_STOCK
    assert product.price == Decimal("54.99")
    assert product.original_price == Decimal("74.99")


def test_release_metadata_and_new_monthly_release():
    product = parse(raw(tags=["Backpack", "Loungefly New", "sept26"]))
    assert product.new_release is True
    assert product.release is not None
    assert product.release.precision == ReleasePrecision.MONTH_ONLY
    assert (product.release.release_month, product.release.release_year) == (9, 2026)


def test_eta_dispatch_and_release_date_remain_distinct():
    product = parse(raw(body=(
        "Releases 10 September 2026. Estimated arrival 18 September 2026. "
        "Expected to dispatch 21 September 2026."
    )))
    assert product.release is not None
    assert product.release.release_date == date(2026, 9, 10)
    assert product.estimated_arrival_date == date(2026, 9, 18)
    assert product.estimated_dispatch_date == date(2026, 9, 21)


def test_exclusive_metadata():
    product = parse(raw(body="An LF Lovers exclusive mini backpack."))
    assert product.exclusive is True
    assert product.exclusive_retailer == "LF Lovers"


@pytest.mark.asyncio
async def test_collection_pagination_filtering_and_duplicates(monkeypatch):
    monkeypatch.setattr(lf_lovers, "PAGE_SIZE", 2)
    first = raw(1)
    duplicate = raw(1, price="69.99")
    accessory = raw(2, title="Loungefly Mini Backpack Keychain Charm")
    second = raw(3)
    http = FakeHttp([
        {"products": [first, accessory]},
        {"products": [duplicate, second]},
        {"products": []},
    ])
    products = await LFLoversMonitor(http).discover_products()
    assert [product.retailer_product_id for product in products] == ["1", "3"]
    assert products[0].price == Decimal("69.99")
    assert http.urls == [
        "https://www.lflovers.com/collections/backpacks/products.json?limit=2&page=1",
        "https://www.lflovers.com/collections/backpacks/products.json?limit=2&page=2",
        "https://www.lflovers.com/collections/backpacks/products.json?limit=2&page=3",
    ]

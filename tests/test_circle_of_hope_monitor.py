from copy import deepcopy

import pytest

from app.models import Availability, ReleasePrecision
from app.monitors.circle_of_hope import CircleOfHopeMonitor


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    async def get_json(self, url):
        self.urls.append(url)
        return self.responses.pop(0)


def raw_product(product_id="101", **changes):
    value = {
        "id": product_id,
        "title": "Loungefly Disney Moon Mini Backpack",
        "handle": "loungefly-disney-moon-mini-backpack",
        "body_html": "<p>Available September 20, 2026 at 9:00 AM MT.</p>",
        "published_at": "2026-08-01T10:00:00-06:00",
        "vendor": "Loungefly",
        "product_type": "Mini Backpack",
        "tags": ["September 2026 Catalog"],
        "variants": [{"id": "501", "sku": "LF-MOON", "available": True,
                      "price": "72.00", "compare_at_price": "90.00"}],
        "images": [{"src": "//cdn.shopify.com/moon.jpg"}],
    }
    value.update(changes)
    return value


def feed(*products):
    return {"products": list(products)}


def test_structured_product_stock_sale_shopify_metadata_and_release():
    product = CircleOfHopeMonitor(FakeHttp([])).parse_product(raw_product())
    assert product.availability == Availability.IN_STOCK
    assert (str(product.price), str(product.original_price)) == ("72.00", "90.00")
    assert product.retailer_product_id == "101" and product.variant_id == "501"
    assert product.sku == "LF-MOON" and product.vendor == "Loungefly"
    assert product.tags == ("September 2026 Catalog",)
    assert product.release.precision == ReleasePrecision.EXACT_DATETIME
    assert product.release.release_datetime.isoformat() == "2026-09-20T09:00:00-06:00"


def test_sold_out_and_preorder_are_normalized():
    monitor = CircleOfHopeMonitor(FakeHttp([]))
    sold_out = monitor.parse_product(raw_product(variants=[{
        "id": "501", "sku": "LF-MOON", "available": False, "price": "90.00"
    }]))
    preorder = monitor.parse_product(raw_product(
        body_html="<p>Pre-order now. Releases October 2, 2026.</p>"
    ))
    assert sold_out.availability == Availability.OUT_OF_STOCK
    assert preorder.availability == Availability.PREORDER and preorder.preorder
    assert preorder.release.release_date.isoformat() == "2026-10-02"


@pytest.mark.asyncio
async def test_collection_signals_merge_without_duplicate_products():
    item = raw_product()
    monitor = CircleOfHopeMonitor(FakeHttp([
        feed(item), feed(item), feed(deepcopy(item)), feed(item), feed(item), feed(item)
    ]))
    products = await monitor.discover_products()
    assert len(products) == 1
    assert products[0].new_release is True
    assert products[0].availability == Availability.COMING_SOON
    assert products[0].exclusive is True
    assert products[0].exclusive_retailer == "Circle Of Hope Boutique"


@pytest.mark.asyncio
async def test_mini_backpack_filter_excludes_accessories_and_other_vendors():
    mini = raw_product()
    charm = raw_product("102", title="Loungefly Mystery Mini Backpack Bag Charm",
                        handle="charm", product_type="Bag Charm")
    other = raw_product("103", title="Other Moon Mini Backpack", handle="other", vendor="Other")
    monitor = CircleOfHopeMonitor(FakeHttp([
        feed(mini, charm, other), feed(), feed(), feed(), feed(), feed()
    ]))
    assert [p.retailer_product_id for p in await monitor.discover_products()] == ["101"]


def test_published_at_is_listing_metadata_not_release_date():
    product = CircleOfHopeMonitor(FakeHttp([])).parse_product(
        raw_product(body_html="<p>A lovely backpack.</p>", tags=[])
    )
    assert product.listing_published_at.isoformat() == "2026-08-01T10:00:00-06:00"
    assert product.release is None


def test_explicit_circle_of_hope_exclusive_copy_is_preserved():
    product = CircleOfHopeMonitor(FakeHttp([])).parse_product(raw_product(
        title="Loungefly Swan Princess Mini Backpack",
        body_html="<p>A Circle Of Hope Boutique Exclusive.</p>", tags=[],
    ))
    assert product.exclusive and product.exclusive_retailer == "Circle Of Hope Boutique"


@pytest.mark.asyncio
async def test_check_uses_canonical_handle_and_preserves_collection_metadata():
    original = CircleOfHopeMonitor(FakeHttp([])).parse_product(
        raw_product(), new_release=True, collection_exclusive=True
    )
    http = FakeHttp([raw_product(variants=[{
        "id": "501", "sku": "LF-MOON", "available": False, "price": "90.00"
    }])])
    checked = await CircleOfHopeMonitor(http).check_product(original)
    assert http.urls == ["https://circleofhopeboutique.com/products/loungefly-disney-moon-mini-backpack.js"]
    assert checked.availability == Availability.OUT_OF_STOCK
    assert checked.new_release and checked.exclusive

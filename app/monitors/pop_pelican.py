"""Pop Pelican Australia adapter using public Shopify collection feeds."""

from app.monitors.shopify import ShopifyRetailerMonitor


class PopPelicanMonitor(ShopifyRetailerMonitor):
    """Discover every enabled Loungefly bag without crawling product pages."""

    retailer_name = "Pop Pelican"
    base_url = "https://poppelican.com.au"
    currency = "AUD"
    local_timezone = "Australia/Sydney"
    date_order = "DMY"
    loungefly_collection_handles = frozenset({"loungefly-bags"})
    collections = {
        "loungefly-bags": None,
        "new-arrivals": "new_arrival",
    }
    # A generic "exclusive" or another retailer's exclusive must never assign
    # Pop Pelican as the originating exclusive retailer.
    retailer_exclusive_pattern = (
        r"\b(?:(?:Pop Pelican)\s+exclusive|exclusive\s+(?:to\s+)?Pop Pelican)\b"
    )

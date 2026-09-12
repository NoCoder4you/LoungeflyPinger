"""WORLD 1-1 GAMES US adapter using public Shopify collection feeds."""

from app.monitors.shopify import ShopifyRetailerMonitor


class World11GamesMonitor(ShopifyRetailerMonitor):
    retailer_name = "WORLD 1-1 GAMES"
    base_url = "https://world-1-1games.com"
    currency = "USD"
    local_timezone = "America/Los_Angeles"
    loungefly_collection_handles = frozenset({"loungefly", "loungefly-pre-orders", "rare-loungefly"})
    collections = {
        "loungefly": None,
        "new-arrival-1": "new_arrival",
        "loungefly-pre-orders": "preorder",
        "exclusives": "exclusive_collection",
        "exclusive-retail-only": "exclusive_collection",
        "rare-loungefly": "rare",
        "sale-1": "sale",
    }
    retailer_exclusive_pattern = (
        r"\b(?:(?:WORLD\s*1-1\s*GAMES|W11G)\s+exclusive|"
        r"exclusive\s+(?:WORLD\s*1-1\s*GAMES|W11G))\b"
    )

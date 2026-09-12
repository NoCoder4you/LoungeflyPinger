"""Gwen's Mermaid Cove US adapter using public Shopify collection feeds."""

import re

from app.monitors.shopify import ShopifyRetailerMonitor


class GwensMermaidCoveMonitor(ShopifyRetailerMonitor):
    """Discover enabled Loungefly bags and reconcile their public stock state."""

    retailer_name = "Gwen's Mermaid Cove"
    base_url = "https://gwensmermaidcove.myshopify.com"
    currency = "USD"
    local_timezone = "America/Los_Angeles"
    collections = {
        "backpacks": None,
        "crossbody-bags": None,
        "handbags": None,
        "totes": None,
        "exclusive": "exclusive_collection",
        "shop-all": "new_arrival",
        "70-clearance": "last_chance",
    }
    # New Arrivals is currently the enormous `shop-all` collection. Sampling
    # its newest default-sorted page catches fresh listings while the complete
    # bag collections remain the authoritative reconciliation lanes.
    collection_page_limits = {"shop-all": 1, "70-clearance": 3}
    retailer_exclusive_pattern = (
        r"\b(?:Gwen(?:'s|’s)? Mermaid Cove exclusive|"
        r"exclusive\s+(?:to\s+)?Gwen(?:'s|’s)? Mermaid Cove)\b"
    )

    @classmethod
    def classify_product_type(cls, raw: object) -> str | None:
        """Use Gwen's descriptions to refine its mostly-empty Shopify taxonomy."""
        product_type = super().classify_product_type(raw)
        if product_type == "Backpack" and isinstance(raw, dict):
            description = cls._plain(raw.get("body_html") or raw.get("description") or "")
            if re.search(r"\bmini[ -]+backpacks?\b", description, re.I):
                return "Mini Backpack"
        return product_type

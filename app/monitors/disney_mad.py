"""Disney Mad UK integration using public, structured Shopify collection feeds."""

from dataclasses import replace
import re

from app.models import Product
from app.monitors.shopify import ShopifyRetailerMonitor


class DisneyMadMonitor(ShopifyRetailerMonitor):
    """Discover Loungefly bags while retaining imported-exclusive provenance."""

    retailer_name = "Disney Mad"
    base_url = "https://disneymad.com"
    currency = "GBP"
    local_timezone = "Europe/London"
    date_order = "DMY"
    collections = {"backpacks": None, "disney-parks": "parks_collection"}
    page_size = 250
    max_pages = 10

    _EXCLUSIVE_RETAILERS = (
        (r"\bbox\s*lunch\s+exclusive\b", "BoxLunch"),
        (r"\bhot\s+topic\s+exclusive\b", "Hot Topic"),
        (r"\b707\s+street\s+exclusive\b", "707 Street"),
        (r"\bretrofacts?\s+exclusive\b", "Retrofacts"),
        (r"\bd23(?:\s+member)?\s+exclusive\b", "D23"),
    )

    @classmethod
    def _parks_metadata(cls, evidence: str) -> tuple[bool, str | None]:
        # Incidental suggestions to carry a bag "to the Disney Parks" are not
        # evidence that Disney Parks created or exclusively sold it.
        parks = bool(re.search(
            r"\b(?:disney parks? (?:exclusive|loungefly)|loungefly[^.]{0,80}[-–—]\s*disney parks|"
            r"exclusive to disney parks|direct from the most magical place on earth)\b",
            evidence, re.I,
        ))
        origins = []
        if re.search(r"\bwalt disney world(?: resort)?\b|\bmagic kingdom\b", evidence, re.I):
            origins.append("Walt Disney World")
        if re.search(r"\bdisneyland(?: resort)?\b|\bthe happiest place on earth\b", evidence, re.I):
            origins.append("Disneyland Resort")
        return parks or bool(origins), " / ".join(origins) or None

    def parse_product(self, raw: object, *, product_type: str,
                      collections: set[str], signals: set[str]) -> Product:
        product = super().parse_product(
            raw, product_type=product_type, collections=collections, signals=signals
        )
        assert isinstance(raw, dict)
        description = self._plain(raw.get("body_html") or raw.get("description") or "")
        evidence = " ".join((product.name, description, *product.tags))
        exclusive_retailer = next(
            (name for pattern, name in self._EXCLUSIVE_RETAILERS if re.search(pattern, evidence, re.I)),
            None,
        )
        parks, parks_origin = self._parks_metadata(evidence)
        parks_exclusive = bool(re.search(
            r"\b(?:disney parks? exclusive|exclusive to disney parks(?: and shopdisney)?)\b",
            evidence, re.I,
        ))
        bundle = bool(re.search(
            r"\b(?:backpack|crossbody|satchel|tote)\b[^.]{0,80}\b(?:and|with|&)\s+(?:a\s+)?coin purse\b",
            product.name, re.I,
        ))
        event = re.search(r"\b((?:Disneyland\s+)?\d{2,3}(?:st|nd|rd|th)\s+Anniversary)\b", evidence, re.I)
        series = re.search(r"\b([A-Z][\w'’& -]{2,60}\s+Collection)\b", evidence)
        code = re.search(r"\b(?:Loungefly\s+)?(?:style|item|product)\s*(?:no\.?|number|code|#)?\s*[:#-]?\s*([A-Z]{2,}[A-Z0-9-]{3,})\b", evidence, re.I)
        return replace(
            product,
            exclusive=product.exclusive or exclusive_retailer is not None or parks_exclusive,
            exclusive_retailer=("Disney Parks" if parks_exclusive else exclusive_retailer),
            exclusive_type=("DISNEY_PARKS" if parks_exclusive else
                            "RETAILER_EXCLUSIVE" if exclusive_retailer else None),
            disney_parks=parks,
            parks_origin=parks_origin,
            franchise="Disney Parks" if parks else product.franchise,
            bundle=bundle,
            included_items=("COIN_PURSE",) if bundle else (),
            event_collection=event.group(1) if event else None,
            series=series.group(1) if series else None,
            loungefly_product_code=code.group(1).upper() if code else None,
            discovery_sources=tuple(sorted(collections)),
        )

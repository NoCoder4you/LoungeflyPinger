"""The Bag Dude US adapter using public Shopify storefront data."""

from __future__ import annotations

from dataclasses import replace
import re
from urllib.parse import quote

from app.models import Product
from app.monitors.shopify import ShopifyRetailerMonitor


class BagDudeMonitor(ShopifyRetailerMonitor):
    """Discover current and vaulted bags without catalogue-wide detail crawling."""

    retailer_name = "The Bag Dude"
    base_url = "https://www.bagdude.com"
    currency = "USD"
    local_timezone = "America/New_York"
    collections = {
        "loungefly-bags": None,
        "backpack": None,
        "crossbody-totes": None,
        "the-bag-dude-vault": "vault",
    }
    # Collection membership supports discovery, but is deliberately not sufficient
    # brand evidence: these collections currently contain several other bag brands.
    loungefly_collection_handles = frozenset()
    retailer_exclusive_pattern = r"\b(?:the\s+)?bag\s*dude\s+exclusive\b"
    homepage_new_arrivals_handle = "homepage-new-arrivals"

    @classmethod
    def is_loungefly(cls, raw: dict[str, object]) -> bool:
        """Require positive brand evidence and reject known conflicting bag makers."""
        vendor = str(raw.get("vendor") or "")
        title = str(raw.get("title") or "")
        if re.search(r"\b(?:Rock Rebel|Dooney\s*&\s*Bourke|Harveys?)\b", f"{vendor} {title}", re.I):
            return False
        if re.search(r"\bloungefly\b", vendor, re.I):
            return True
        tags = " ".join(cls._tags(raw))
        if re.search(r"\bloungefly\b", tags, re.I) or re.search(r"\bloungefly\b", title, re.I):
            return True
        # Descriptions are the weakest accepted signal and only apply when the
        # structured vendor does not identify a different manufacturer.
        description = cls._plain(raw.get("body_html") or raw.get("description") or "")
        return not vendor.strip() and bool(re.search(r"\bloungefly\b", description, re.I))

    @classmethod
    def classify_product_type(cls, raw: object) -> str | None:
        if isinstance(raw, dict):
            evidence = " ".join((
                str(raw.get("title") or ""),
                str(raw.get("product_type") or raw.get("type") or ""),
                " ".join(cls._tags(raw)),
            ))
            # Multi-function and sling products need to win over the individual
            # "backpack", "tote", and "crossbody" words in their titles.
            if re.search(r"\bconvertible\b.*\b(?:backpack|tote|crossbody|bag)\b", evidence, re.I):
                return "Convertible Bag"
            if re.search(r"\bsling\b.*\b(?:crossbody|bag)\b", evidence, re.I):
                return "Sling Bag"
        return super().classify_product_type(raw)

    @classmethod
    def _homepage_handles(cls, html: str) -> tuple[str, ...]:
        """Return only links in the storefront's curated New Arrivals component."""
        heading = re.search(r">\s*New Arrivals\s*</h2\s*>", html, re.I)
        if not heading:
            return ()
        end = html.find("customElements.define", heading.end())
        section = html[heading.end(): end if end >= 0 else len(html)]
        return tuple(dict.fromkeys(re.findall(
            r'href=["\'](?:https?://www\.bagdude\.com)?/products/([^"\'?#/]+)', section, re.I
        )))

    async def discover_products(self) -> list[Product]:
        products = await super().discover_products()
        by_handle = {product.url.rstrip("/").rsplit("/", 1)[-1]: product for product in products}
        for handle in self._homepage_handles(await self.http.get_text(self.base_url + "/")):
            existing = by_handle.get(handle)
            if existing is not None:
                collections = (*existing.collections, self.homepage_new_arrivals_handle)
                by_handle[handle] = replace(existing, collections=collections)
                continue
            raw = await self.http.get_json(f"{self.base_url}/products/{quote(handle)}.js")
            product_type = self.classify_product_type(raw)
            if isinstance(raw, dict) and product_type and self.is_loungefly(raw):
                product = self.parse_product(
                    raw, product_type=product_type,
                    collections={self.homepage_new_arrivals_handle}, signals=set(),
                )
                by_handle[handle] = product
        # Shopify IDs remain the canonical identity even when a homepage link and
        # several collections all expose the same item.
        return list({product.retailer_product_id: product for product in by_handle.values()}.values())

    def parse_product(self, raw: object, *, product_type: str,
                      collections: set[str], signals: set[str]) -> Product:
        product = super().parse_product(
            raw, product_type=product_type, collections=collections, signals=signals
        )
        evidence = product.exclusivity_text or " ".join((product.name, *product.tags))
        external_retailer = None
        for pattern, retailer in (
            (r"\bUniversal(?:\s+(?:Studios|Parks?))?\s+Exclusive\b", "Universal"),
            (r"\bDisney\s+Parks?\s+Exclusive\b", "Disney Parks"),
        ):
            if re.search(pattern, evidence, re.I):
                external_retailer = retailer
                break
        is_vault = "the-bag-dude-vault" in collections or bool(
            re.search(r"\bLoungefly\s+Vault\b", product.name, re.I)
        )
        is_new = bool(re.search(r"\bLoungefly\s+New\b", product.name, re.I))
        return replace(
            product,
            vaulted=is_vault,
            collection_type="VAULT" if is_vault else "NEW" if is_new else product.collection_type,
            # Vault and the retailer's "New" title convention are listing metadata,
            # never evidence of an official new release.
            new_release=False,
            exclusive_retailer=external_retailer or product.exclusive_retailer,
        )

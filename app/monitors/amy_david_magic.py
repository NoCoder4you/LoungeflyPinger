"""AmyDavidMagic collector catalogue and incoming-product discovery."""

from __future__ import annotations

from dataclasses import replace
from html import unescape
import re

from app.models import Availability, Product
from app.monitors.shopify import ShopifyRetailerMonitor
from app.release import parse_release_text


class AmyDavidMagicMonitor(ShopifyRetailerMonitor):
    """Reuse Shopify commerce data and enrich AmyDavidMagic's collector fields."""

    retailer_name = "AmyDavidMagic"
    base_url = "https://amydavidmagic.com"
    currency = "USD"
    local_timezone = "America/New_York"
    collections = {
        "mini-backpacks-all": None,
        "full-size-backpacks": None,
        "crossbodys": None,
        "satchel-bags": None,
        "tote-bags": None,
        "limited-drops": "limited_drop",
        "new-arrivals-1": "new_arrival",
    }
    collection_page_limits = {"new-arrivals-1": 1}
    retailer_exclusive_pattern = (
        r"\b(?:AmyDavidMagic exclusive|exclusive (?:to|for) AmyDavidMagic)\b"
    )
    incoming_articles = ("d23-2026-loungefly-exclusives",)

    @staticmethod
    def _detail(description: str, *labels: str) -> str | None:
        names = "|".join(re.escape(label) for label in labels)
        match = re.search(rf"\b(?:{names})\s*:\s*([^\n|•]+?)(?=\s+(?:Brand|License|"
                          r"Characters?|Film|Property|Edition|Style|Special Features|Barcode|"
                          r"Dimensions?)\s*:|$)", description, re.I)
        return match.group(1).strip(" .") if match else None

    def parse_product(self, raw: object, *, product_type: str,
                      collections: set[str], signals: set[str]) -> Product:
        product = super().parse_product(
            raw, product_type=product_type, collections=collections, signals=signals
        )
        assert isinstance(raw, dict)
        description = self._plain(raw.get("body_html") or raw.get("description") or "")
        edition = self._detail(description, "Edition")
        license_value = self._detail(description, "License")
        property_value = self._detail(description, "Property", "Film")
        character_text = self._detail(description, "Character", "Characters")
        characters = tuple(part.strip() for part in re.split(r"[,/&]", character_text or "")
                           if part.strip())
        style = self._detail(description, "Style")
        evidence = " ".join((product.name, description, edition or ""))
        external = None
        for pattern, retailer in (
            (r"\bGrotto Treasures Exclusive\b", "Grotto Treasures"),
            (r"\bDisney Parks? Exclusive\b", "Disney Parks"),
            (r"\bLoungefly Exclusive\b", "Loungefly"),
            (r"\bBoxLunch Exclusive\b", "BoxLunch"),
            (r"\bHot Topic Exclusive\b", "Hot Topic"),
        ):
            if re.search(pattern, evidence, re.I):
                external = retailer
                break
        d23 = bool(re.search(r"\bD23(?:\s+\d{4})?\s+Exclusive\b", evidence, re.I))
        year = re.search(r"\bD23\s+(20\d{2})\b", evidence, re.I)
        incoming = None
        if re.search(r"\bin transit\b", evidence, re.I):
            incoming = "IN_TRANSIT"
        elif re.search(r"\bordered\b", evidence, re.I):
            incoming = "ORDERED"
        elif re.search(r"\bcoming soon\b", evidence, re.I):
            incoming = "COMING_SOON"
        release = product.release
        # Shopify publication and merchandising status are not release evidence.
        if not re.search(r"\b(?:release|launch)\s+(?:date|time|on|at)\b", description, re.I):
            release = None
        return replace(
            product,
            franchise=license_value or product.franchise,
            character=characters[0] if characters else product.character,
            characters=characters,
            license=license_value,
            property=property_value,
            edition=edition,
            style=style,
            limited_edition=product.limited_edition or bool(
                edition and re.search(r"limited edition", edition, re.I)
            ),
            exclusive=product.exclusive or d23,
            exclusive_retailer=None if d23 else external or product.exclusive_retailer,
            event_exclusive=d23,
            event_name="D23" if d23 else None,
            event_year=int(year.group(1)) if year else None,
            incoming_status=incoming,
            availability=Availability.COMING_SOON if incoming else product.availability,
            release=release,
            discovery_sources=tuple(f"collection:{value}" for value in sorted(collections)),
        )

    @classmethod
    def _article_items(cls, html: str) -> list[tuple[str, str]]:
        content = re.search(r'article-template__content[^>]*>(.*?)</div>', html, re.I | re.S)
        if not content:
            return []
        blocks = re.findall(r"<h3[^>]*>(.*?)</h3>(.*?)(?=<h[23][^>]*>|$)",
                            content.group(1), re.I | re.S)
        return [(cls._plain(unescape(title)), cls._plain(unescape(body)))
                for title, body in blocks]

    async def discover_products(self) -> list[Product]:
        products = await super().discover_products()
        by_exact_name = {product.name.casefold(): product for product in products}
        result = {product.retailer_product_id: product for product in products}
        for slug in self.incoming_articles:
            url = f"{self.base_url}/blogs/news/{slug}"
            html = await self.http.get_text(url)
            for title, body in self._article_items(html):
                kind = self.classify_product_type({"title": title, "body_html": body})
                if not kind:
                    continue
                existing = by_exact_name.get(title.casefold())
                if existing:
                    result[existing.retailer_product_id] = replace(
                        existing, availability=Availability.COMING_SOON,
                        incoming_status="IN_TRANSIT" if "in transit" in body.casefold()
                        else "ORDERED", event_exclusive=True, event_name="D23", event_year=2026,
                        exclusive=True, exclusive_retailer=None,
                        discovery_sources=(*existing.discovery_sources, f"blog:{slug}"),
                    )
                    continue
                result[f"blog:{slug}:{title.casefold()}"] = Product(
                    retailer=self.retailer_name,
                    retailer_product_id=f"blog:{slug}:{title.casefold()}", name=title, url=url,
                    availability=Availability.COMING_SOON, product_type=kind,
                    incoming_status="IN_TRANSIT" if "in transit" in body.casefold() else "ORDERED",
                    exclusive=True, event_exclusive=True, event_name="D23", event_year=2026,
                    exclusivity_text="D23 Exclusive", discovery_sources=(f"blog:{slug}",),
                    # An article's publication date is intentionally not stored as a release.
                    release=None,
                )
        return list(result.values())

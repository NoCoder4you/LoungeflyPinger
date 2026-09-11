"""Cool-Merch UK adapter for its public Shopify product feed."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any
from urllib.parse import quote, urljoin

from app.http import AsyncHttpClient, HttpClientError
from app.models import Availability, Product
from app.monitors.base import RetailerMonitor
from app.release import parse_release_text

BASE_URL = "https://www.cool-merch.com"
COLLECTION_HANDLE = "loungefly"
PAGE_SIZE = 250
MAX_PAGES = 10


class CoolMerchParseError(ValueError):
    """The structured Cool-Merch Shopify response was malformed."""


class CoolMerchMonitor(RetailerMonitor):
    """Monitor Loungefly mini backpacks without depending on theme markup."""

    def __init__(self, http: AsyncHttpClient, *, base_url: str = BASE_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def discover_products(self) -> list[Product]:
        found: dict[str, Product] = {}
        for page in range(1, MAX_PAGES + 1):
            payload = await self.http.get_json(
                f"{self.base_url}/collections/{COLLECTION_HANDLE}/products.json"
                f"?limit={PAGE_SIZE}&page={page}"
            )
            products = self._product_list(payload)
            for raw in products:
                if self._is_loungefly_mini_backpack(raw):
                    product = self.parse_product(raw)
                    found[product.retailer_product_id] = product
            if len(products) < PAGE_SIZE:
                return list(found.values())
        raise CoolMerchParseError("Cool-Merch collection exceeded the pagination safety limit")

    async def check_product(self, product: Product) -> Product:
        handle = product.url.rstrip("/").rsplit("/", 1)[-1]
        if not handle:
            return replace(product, availability=Availability.ERROR)
        try:
            raw = await self.http.get_json(f"{self.base_url}/products/{quote(handle)}.js")
            checked = self.parse_product(raw)
            if not self._is_loungefly_mini_backpack(raw):
                return replace(product, availability=Availability.UNAVAILABLE)
            return checked
        except (HttpClientError, CoolMerchParseError, ValueError, TypeError):
            # A broken/changed response is unknown inventory, never a sell-out.
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            payload = await self.http.get_json(
                f"{self.base_url}/collections/{COLLECTION_HANDLE}/products.json?limit=1&page=1"
            )
            self._product_list(payload)
            return True
        except (HttpClientError, CoolMerchParseError):
            return False

    @staticmethod
    def _product_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise CoolMerchParseError("Cool-Merch response has no product list")
        products = payload["products"]
        if not all(isinstance(item, dict) for item in products):
            raise CoolMerchParseError("Cool-Merch product list contains invalid entries")
        return products

    @staticmethod
    def _tags(raw: dict[str, Any]) -> list[str]:
        tags = raw.get("tags", [])
        if isinstance(tags, str):
            return [tag.strip() for tag in tags.split(",") if tag.strip()]
        if isinstance(tags, list) and all(isinstance(tag, str) for tag in tags):
            return [tag.strip() for tag in tags if tag.strip()]
        return []

    @classmethod
    def _is_loungefly_mini_backpack(cls, raw: dict[str, Any]) -> bool:
        """Use Shopify taxonomy first, then a narrowly-scoped title assertion."""
        title = raw.get("title")
        product_type = raw.get("product_type") or raw.get("type")
        vendor = raw.get("vendor")
        if not all(isinstance(value, str) for value in (title, product_type, vendor)):
            return False
        tags = cls._tags(raw)
        tag_text = " ".join(tags).casefold()
        title_text = " ".join(title.casefold().replace("-", " ").split())
        structured_text = f"{product_type} {tag_text}".casefold()
        exclusions = (
            "keychain", "key chain", "keyring", "bag charm", "wallet", "purse",
            "crossbody", "tote", "cardholder", "card holder", "pin", "organiser", "insert",
        )
        excluded = any(
            re.search(rf"\b{re.escape(term)}\b", f"{title_text} {structured_text}")
            for term in exclusions
        )
        return (
            product_type.casefold() == "bags"
            and "loungefly" in {tag.casefold() for tag in tags}
            and "bags" in {tag.casefold() for tag in tags}
            and re.search(r"\bmini backpack\b", title_text) is not None
            and not excluded
        )

    def parse_product(self, raw: object) -> Product:
        if not isinstance(raw, dict):
            raise CoolMerchParseError("Product is not an object")
        try:
            product_id = str(raw["id"]).strip()
            title = raw["title"].strip()
            handle = raw["handle"].strip()
            variants = raw["variants"]
        except (KeyError, AttributeError) as exc:
            raise CoolMerchParseError("Product identity is incomplete") from exc
        if not product_id or not title or not handle or not isinstance(variants, list) or not variants:
            raise CoolMerchParseError("Product identity or variants are incomplete")
        if not all(isinstance(v, dict) and isinstance(v.get("available"), bool) for v in variants):
            raise CoolMerchParseError("Variant availability is missing")

        tags = self._tags(raw)
        evidence = " ".join([title, str(raw.get("body_html", "")), *tags])
        preorder = re.search(r"\bpre[ -]?order(?:ed)?\b", evidence, re.I) is not None
        coming_soon = re.search(r"\bcoming soon\b", evidence, re.I) is not None
        selected = next((variant for variant in variants if variant["available"]), variants[0])
        availability = (
            Availability.PREORDER if preorder else
            Availability.COMING_SOON if coming_soon else
            Availability.IN_STOCK if any(v["available"] for v in variants) else
            Availability.OUT_OF_STOCK
        )
        try:
            price = Decimal(str(selected["price"]))
            compare = selected.get("compare_at_price")
            original_price = Decimal(str(compare)) if compare not in (None, "") else None
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise CoolMerchParseError("Variant pricing is invalid") from exc

        images = raw.get("images", [])
        image = images[0].get("src") if images and isinstance(images[0], dict) else (
            images[0] if images else raw.get("featured_image")
        )
        if isinstance(image, str) and image.startswith("//"):
            image = "https:" + image
        if image is not None and not isinstance(image, str):
            raise CoolMerchParseError("Product image is invalid")

        published_at = None
        if raw.get("published_at") not in (None, ""):
            try:
                published_at = datetime.fromisoformat(str(raw["published_at"]).replace("Z", "+00:00"))
                if published_at.tzinfo is None:
                    raise ValueError
            except ValueError as exc:
                raise CoolMerchParseError("Product publication timestamp is invalid") from exc
        # Publication metadata is retained separately and is deliberately excluded
        # from release parsing. Only retailer-authored descriptive evidence is used.
        release = parse_release_text(
            " ".join([str(raw.get("body_html", "")), *tags]),
            source="Cool-Merch Shopify product data", local_timezone="Europe/London",
        )
        exclusive = re.search(r"\b(?:cool[ -]merch exclusive|exclusive to cool[ -]merch)\b", evidence, re.I) is not None
        variant_id = selected.get("id")
        sku = selected.get("sku")
        barcode = selected.get("barcode")
        return Product(
            retailer="Cool-Merch UK", retailer_product_id=product_id, name=title,
            url=urljoin(self.base_url + "/", f"products/{handle}"), image_url=image,
            price=price, original_price=original_price, currency="GBP", availability=availability,
            product_type="Mini Backpack", exclusive=exclusive,
            exclusive_retailer="Cool-Merch UK" if exclusive else None,
            preorder=preorder, sku=str(sku).strip() if sku not in (None, "") else None,
            variant_id=str(variant_id).strip() if variant_id not in (None, "") else None,
            barcode=str(barcode).strip() if barcode not in (None, "") else None,
            vendor=str(raw.get("vendor")).strip() if raw.get("vendor") else None,
            tags=tuple(tags), listing_published_at=published_at, release=release,
        )

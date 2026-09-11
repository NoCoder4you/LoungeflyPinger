"""Pink a la Mode US adapter using the storefront's public Shopify feeds."""

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

BASE_URL = "https://pinkalamode.com"
BACKPACK_COLLECTION = "mini-backpacks"
NEW_COLLECTION = "new-arrivals"
PAGE_SIZE = 250
MAX_PAGES = 10


class PinkALaModeParseError(ValueError):
    """The public Pink a la Mode Shopify representation was malformed."""


class PinkALaModeMonitor(RetailerMonitor):
    """Discover Loungefly mini backpacks from structured Shopify product data."""

    def __init__(self, http: AsyncHttpClient, *, base_url: str = BASE_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def discover_products(self) -> list[Product]:
        found: dict[str, Product] = {}
        for collection, is_new in ((BACKPACK_COLLECTION, False), (NEW_COLLECTION, True)):
            for raw in await self._collection_products(collection):
                if not self._is_loungefly_mini_backpack(raw):
                    continue
                product = self.parse_product(raw, new_release=is_new)
                previous = found.get(product.retailer_product_id)
                # A product present in both feeds is one product, with New Arrivals
                # membership preserved regardless of collection traversal order.
                if previous is not None and previous.new_release:
                    product = replace(product, new_release=True)
                found[product.retailer_product_id] = product
        return list(found.values())

    async def _collection_products(self, handle: str) -> list[dict[str, Any]]:
        products: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            payload = await self.http.get_json(
                f"{self.base_url}/collections/{quote(handle)}/products.json"
                f"?limit={PAGE_SIZE}&page={page}"
            )
            batch = self._product_list(payload)
            products.extend(batch)
            if len(batch) < PAGE_SIZE:
                return products
        raise PinkALaModeParseError(f"Pink a la Mode {handle} collection exceeded safety limit")

    async def check_product(self, product: Product) -> Product:
        try:
            products = await self._collection_products(BACKPACK_COLLECTION)
            raw = next(
                (item for item in products if str(item.get("id", "")).strip() == product.retailer_product_id),
                None,
            )
            if raw is None:
                return replace(product, availability=Availability.UNAVAILABLE)
            return self.parse_product(raw, new_release=product.new_release)
        except (HttpClientError, PinkALaModeParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            payload = await self.http.get_json(
                f"{self.base_url}/collections/{BACKPACK_COLLECTION}/products.json?limit=1&page=1"
            )
            self._product_list(payload)
            return True
        except (HttpClientError, PinkALaModeParseError):
            return False

    @staticmethod
    def _product_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise PinkALaModeParseError("Pink a la Mode response has no product list")
        products = payload["products"]
        if not all(isinstance(item, dict) for item in products):
            raise PinkALaModeParseError("Pink a la Mode product list contains invalid entries")
        return products

    @staticmethod
    def _tags(raw: dict[str, Any]) -> list[str]:
        value = raw.get("tags", [])
        if isinstance(value, str):
            return [tag.strip() for tag in value.split(",") if tag.strip()]
        if isinstance(value, list) and all(isinstance(tag, str) for tag in value):
            return [tag.strip() for tag in value if tag.strip()]
        return []

    @classmethod
    def _is_loungefly_mini_backpack(cls, raw: dict[str, Any]) -> bool:
        title, vendor = raw.get("title"), raw.get("vendor")
        product_type = raw.get("product_type") or raw.get("type")
        if not all(isinstance(value, str) for value in (title, vendor, product_type)):
            return False
        normalized = " ".join(title.casefold().replace("-", " ").split())
        tags = {tag.casefold() for tag in cls._tags(raw)}
        excluded = (
            "wallet", "crossbody", "sling bag", "charm", "keychain", "key chain",
            "card holder", "cardholder", "coin bag", "pin",
        )
        return (
            vendor.casefold() == "loungefly"
            and product_type.casefold() in {"backpack", "backpacks"}
            and ("mini backpack" in normalized or "mini backpack" in tags)
            and not any(re.search(rf"\b{re.escape(term)}\b", normalized) for term in excluded)
        )

    def parse_product(self, raw: object, *, new_release: bool = False) -> Product:
        if not isinstance(raw, dict):
            raise PinkALaModeParseError("Product is not an object")
        try:
            product_id = str(raw["id"]).strip()
            title = raw["title"].strip()
            handle = raw["handle"].strip()
            variants = raw["variants"]
        except (KeyError, AttributeError) as exc:
            raise PinkALaModeParseError("Product identity is incomplete") from exc
        if not product_id or not title or not handle or not isinstance(variants, list) or not variants:
            raise PinkALaModeParseError("Product identity or variants are incomplete")
        if not all(isinstance(v, dict) and isinstance(v.get("available"), bool) for v in variants):
            raise PinkALaModeParseError("Variant availability is missing")

        tags = self._tags(raw)
        evidence = " ".join([title, str(raw.get("body_html", "")), *tags])
        lowered_tags = {tag.casefold() for tag in tags}
        preorder = bool(re.search(r"\bpre[ -]?order\b", evidence, re.I))
        coming_soon = any(tag in {"coming soon", "coming-soon"} for tag in lowered_tags)
        available_variant = next((variant for variant in variants if variant["available"]), variants[0])
        availability = (
            Availability.PREORDER if preorder else
            Availability.COMING_SOON if coming_soon else
            Availability.IN_STOCK if any(variant["available"] for variant in variants) else
            Availability.OUT_OF_STOCK
        )
        try:
            price = Decimal(str(available_variant["price"]))
            compare = available_variant.get("compare_at_price")
            original_price = Decimal(str(compare)) if compare not in (None, "") else None
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise PinkALaModeParseError("Variant pricing is invalid") from exc

        images = raw.get("images", [])
        image = images[0].get("src") if images and isinstance(images[0], dict) else (
            images[0] if images else raw.get("featured_image")
        )
        if isinstance(image, str) and image.startswith("//"):
            image = "https:" + image
        if image is not None and not isinstance(image, str):
            raise PinkALaModeParseError("Product image is invalid")

        # Shopify publication is merchandising metadata, not a promised release.
        published_at = raw.get("published_at")
        if published_at is not None:
            try:
                datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
            except ValueError as exc:
                raise PinkALaModeParseError("Product publication timestamp is invalid") from exc
        release = parse_release_text(
            " ".join([str(raw.get("body_html", "")), *tags]),
            source="Pink a la Mode Shopify product data",
            local_timezone="America/Los_Angeles",
            date_order="MDY",
        )
        exclusive = bool(re.search(r"\b(?:pink a la mode|palm) exclusive\b", evidence, re.I))
        sku = available_variant.get("sku")
        return Product(
            retailer="Pink a la Mode", retailer_product_id=product_id, name=title,
            url=urljoin(self.base_url + "/", f"products/{handle}"), image_url=image,
            price=price, original_price=original_price, currency="USD", availability=availability,
            product_type="Mini Backpack", exclusive=exclusive,
            exclusive_retailer="Pink a la Mode" if exclusive else None,
            new_release=new_release, preorder=preorder,
            sku=str(sku).strip() if sku not in (None, "") else None, release=release,
        )

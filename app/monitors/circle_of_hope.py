"""Circle Of Hope Boutique US adapter for the store's public Shopify feeds."""

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

BASE_URL = "https://circleofhopeboutique.com"
COLLECTIONS = {
    "backpacks-1": None,
    "coming-soon-1": "coming_soon",
    "aristocats-marie-exclusives": "exclusive",
    "new-arrivals": "new_release",
    "loungefly": None,
    "all-store-products": None,
}
PAGE_SIZE = 250
MAX_PAGES = 10


class CircleOfHopeParseError(ValueError):
    """The public Shopify representation was incomplete or malformed."""


class CircleOfHopeMonitor(RetailerMonitor):
    """Discover Loungefly mini backpacks across the store's merchandising collections."""

    def __init__(self, http: AsyncHttpClient, *, base_url: str = BASE_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def discover_products(self) -> list[Product]:
        found: dict[str, Product] = {}
        flags: dict[str, set[str]] = {}
        raw_by_id: dict[str, dict[str, Any]] = {}
        for handle, signal in COLLECTIONS.items():
            for raw in await self._collection_products(handle):
                if not self._is_loungefly_mini_backpack(raw):
                    continue
                product_id = str(raw.get("id", "")).strip()
                if not product_id:
                    raise CircleOfHopeParseError("Shopify product ID is missing")
                raw_by_id[product_id] = raw
                if signal:
                    flags.setdefault(product_id, set()).add(signal)
        for product_id, raw in raw_by_id.items():
            signals = flags.get(product_id, set())
            found[product_id] = self.parse_product(
                raw, new_release="new_release" in signals,
                coming_soon="coming_soon" in signals,
                collection_exclusive="exclusive" in signals,
            )
        return list(found.values())

    async def _collection_products(self, handle: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            payload = await self.http.get_json(
                f"{self.base_url}/collections/{quote(handle)}/products.json"
                f"?limit={PAGE_SIZE}&page={page}"
            )
            batch = self._product_list(payload)
            result.extend(batch)
            if len(batch) < PAGE_SIZE:
                return result
        raise CircleOfHopeParseError(f"Collection {handle} exceeded the pagination safety limit")

    async def check_product(self, product: Product) -> Product:
        handle = product.url.rstrip("/").rsplit("/", 1)[-1]
        try:
            if not handle:
                raise CircleOfHopeParseError("Canonical handle is missing")
            return self.parse_product(
                await self.http.get_json(f"{self.base_url}/products/{quote(handle)}.js"),
                new_release=product.new_release,
                coming_soon=product.availability == Availability.COMING_SOON,
                collection_exclusive=product.exclusive,
            )
        except (HttpClientError, CircleOfHopeParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            self._product_list(await self.http.get_json(
                f"{self.base_url}/collections/backpacks-1/products.json?limit=1&page=1"
            ))
            return True
        except (HttpClientError, CircleOfHopeParseError):
            return False

    @staticmethod
    def _product_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise CircleOfHopeParseError("Circle Of Hope response has no product list")
        products = payload["products"]
        if not all(isinstance(item, dict) for item in products):
            raise CircleOfHopeParseError("Circle Of Hope product list contains invalid entries")
        return products

    @staticmethod
    def _tags(raw: dict[str, Any]) -> tuple[str, ...]:
        value = raw.get("tags", [])
        if isinstance(value, str):
            return tuple(tag.strip() for tag in value.split(",") if tag.strip())
        if isinstance(value, list) and all(isinstance(tag, str) for tag in value):
            return tuple(tag.strip() for tag in value if tag.strip())
        return ()

    @classmethod
    def _is_loungefly_mini_backpack(cls, raw: dict[str, Any]) -> bool:
        title, vendor = raw.get("title"), raw.get("vendor")
        product_type = raw.get("product_type") or raw.get("type")
        if not all(isinstance(value, str) for value in (title, vendor, product_type)):
            return False
        normalized = " ".join(title.casefold().replace("-", " ").split())
        excluded = ("wallet", "bag charm", "keychain", "crossbody", "card holder", "coin bag")
        return (
            vendor.casefold() == "loungefly"
            and product_type.casefold() in {"mini backpack", "backpack", "backpacks"}
            and "mini backpack" in normalized
            and not any(term in normalized for term in excluded)
        )

    def parse_product(self, raw: object, *, new_release: bool = False,
                      coming_soon: bool = False, collection_exclusive: bool = False) -> Product:
        if not isinstance(raw, dict):
            raise CircleOfHopeParseError("Product is not an object")
        try:
            product_id = str(raw["id"]).strip()
            title = raw["title"].strip()
            handle = raw["handle"].strip()
            variants = raw["variants"]
        except (KeyError, AttributeError) as exc:
            raise CircleOfHopeParseError("Product identity is incomplete") from exc
        if not product_id or not title or not handle or not isinstance(variants, list) or not variants:
            raise CircleOfHopeParseError("Product identity or variants are incomplete")
        if not all(isinstance(v, dict) and isinstance(v.get("available"), bool) for v in variants):
            raise CircleOfHopeParseError("Variant availability is missing")

        tags = self._tags(raw)
        evidence = " ".join((title, str(raw.get("body_html", "")), *tags))
        preorder = bool(re.search(r"\bpre[ -]?orders?\b", evidence, re.I))
        selected = next((variant for variant in variants if variant["available"]), variants[0])
        variant_id = str(selected.get("id", "")).strip()
        if not variant_id:
            raise CircleOfHopeParseError("Shopify variant ID is missing")
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
            raise CircleOfHopeParseError("Variant pricing is invalid") from exc

        published_at = None
        if raw.get("published_at") is not None:
            try:
                published_at = datetime.fromisoformat(str(raw["published_at"]).replace("Z", "+00:00"))
            except ValueError as exc:
                raise CircleOfHopeParseError("Product publication timestamp is invalid") from exc
            if published_at.tzinfo is None:
                raise CircleOfHopeParseError("Product publication timestamp has no timezone")
        images = raw.get("images", [])
        image = images[0].get("src") if images and isinstance(images[0], dict) else (
            images[0] if images else raw.get("featured_image")
        )
        if isinstance(image, str) and image.startswith("//"):
            image = "https:" + image
        if image is not None and not isinstance(image, str):
            raise CircleOfHopeParseError("Product image is invalid")

        # Publication controls storefront visibility and is never release evidence.
        release = parse_release_text(
            " ".join((str(raw.get("body_html", "")), *tags)),
            source="Circle Of Hope Boutique Shopify product data",
            local_timezone="America/Denver", date_order="MDY",
        )
        explicit_exclusive = collection_exclusive or bool(re.search(
            r"\bcircle\s+of\s+hope(?:\s+boutique)?\s+exclusive\b|\|\s*exclusive\b",
            evidence, re.I,
        ))
        sku = selected.get("sku")
        vendor = raw.get("vendor")
        return Product(
            retailer="Circle Of Hope Boutique", retailer_product_id=product_id,
            variant_id=variant_id, sku=str(sku).strip() if sku not in (None, "") else None,
            name=title, url=urljoin(self.base_url + "/", f"products/{handle}"), image_url=image,
            price=price, original_price=original_price, currency="USD", availability=availability,
            product_type="Mini Backpack", vendor=vendor.strip() if isinstance(vendor, str) else None,
            tags=tags, listing_published_at=published_at, release=release,
            new_release=new_release, preorder=preorder, exclusive=explicit_exclusive,
            exclusive_retailer="Circle Of Hope Boutique" if explicit_exclusive else None,
        )

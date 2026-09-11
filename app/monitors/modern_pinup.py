"""Modern PinUp US adapter using the storefront's public Shopify feeds."""

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

BASE_URL = "https://www.modernpinup.com"
COLLECTION_HANDLE = "backpacks"
PAGE_SIZE = 250
MAX_PAGES = 10


class ModernPinUpParseError(ValueError):
    """The public Shopify representation was incomplete or malformed."""


class ModernPinUpMonitor(RetailerMonitor):
    """Discover Loungefly mini backpacks through stable Shopify product fields."""

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
                if self._is_mini_backpack(raw):
                    product = self.parse_product(raw)
                    found[product.retailer_product_id] = product
            if len(products) < PAGE_SIZE:
                return list(found.values())
        raise ModernPinUpParseError("Modern PinUp collection exceeded the pagination safety limit")

    async def check_product(self, product: Product) -> Product:
        handle = product.url.rstrip("/").rsplit("/", 1)[-1]
        try:
            if not handle:
                raise ModernPinUpParseError("Product handle is missing")
            return self.parse_product(
                await self.http.get_json(f"{self.base_url}/products/{quote(handle)}.js")
            )
        except (HttpClientError, ModernPinUpParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            payload = await self.http.get_json(
                f"{self.base_url}/collections/{COLLECTION_HANDLE}/products.json?limit=1"
            )
            self._product_list(payload)
            return True
        except (HttpClientError, ModernPinUpParseError):
            return False

    @staticmethod
    def _product_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise ModernPinUpParseError("Modern PinUp response has no product list")
        products = payload["products"]
        if not all(isinstance(item, dict) for item in products):
            raise ModernPinUpParseError("Modern PinUp product list contains invalid entries")
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
    def _is_mini_backpack(cls, raw: dict[str, Any]) -> bool:
        title = raw.get("title")
        product_type = raw.get("product_type") or raw.get("type")
        if not isinstance(title, str) or not isinstance(product_type, str):
            return False
        normalized = " ".join(title.casefold().replace("-", " ").split())
        tags = {tag.casefold() for tag in cls._tags(raw)}
        excluded = ("wallet", "crossbody", "charm", "keychain", "key chain", "card holder",
                    "cardholder", "coin bag", "handbag", "pin")
        # The collection's structured product type is primary evidence; the title narrows
        # full-size backpacks and defensively excludes incorrectly classified accessories.
        return (
            product_type.casefold() in {"backpack", "backpacks"}
            and ("loungefly" in tags or "loungefly" in normalized)
            and "mini backpack" in normalized
            and not any(re.search(rf"\b{re.escape(term)}\b", normalized) for term in excluded)
        )

    def parse_product(self, raw: object) -> Product:
        if not isinstance(raw, dict):
            raise ModernPinUpParseError("Product is not an object")
        try:
            product_id = str(raw["id"]).strip()
            title = raw["title"].strip()
            handle = raw["handle"].strip()
            variants = raw["variants"]
        except (KeyError, AttributeError) as exc:
            raise ModernPinUpParseError("Product identity is incomplete") from exc
        if not product_id or not title or not handle or not isinstance(variants, list) or not variants:
            raise ModernPinUpParseError("Product identity or variants are incomplete")
        if not all(isinstance(v, dict) and isinstance(v.get("available"), bool) for v in variants):
            raise ModernPinUpParseError("Variant availability is missing")

        tags = self._tags(raw)
        lowered_tags = {tag.casefold() for tag in tags}
        preorder = any(tag.startswith(("preorder", "pre-order", "pre order")) for tag in lowered_tags)
        coming_soon = any(tag.startswith(("coming soon", "coming-soon")) for tag in lowered_tags)
        available_variant = next((v for v in variants if v["available"]), variants[0])
        if preorder:
            availability = Availability.PREORDER
        elif coming_soon:
            availability = Availability.COMING_SOON
        elif any(v["available"] for v in variants):
            availability = Availability.IN_STOCK
        else:
            availability = Availability.OUT_OF_STOCK
        try:
            price = Decimal(str(available_variant["price"]))
            compare = available_variant.get("compare_at_price")
            original_price = Decimal(str(compare)) if compare not in (None, "") else None
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise ModernPinUpParseError("Variant pricing is invalid") from exc

        images = raw.get("images", [])
        image = images[0].get("src") if images and isinstance(images[0], dict) else (
            images[0] if images else raw.get("featured_image")
        )
        if isinstance(image, str) and image.startswith("//"):
            image = "https:" + image
        if image is not None and not isinstance(image, str):
            raise ModernPinUpParseError("Product image is invalid")

        # published_at is validated and retained only as publication metadata. It is
        # deliberately never supplied to the release parser.
        published_at = raw.get("published_at")
        if published_at is not None:
            try:
                datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ModernPinUpParseError("Product publication timestamp is invalid") from exc
        release = parse_release_text(
            " ".join([str(raw.get("body_html", "")), *tags]),
            source="Modern PinUp Shopify product data", local_timezone="America/Los_Angeles",
            date_order="MDY",
        )
        exclusive = "exclusives" in lowered_tags or "modern pinup exclusive" in lowered_tags
        return Product(
            retailer="Modern PinUp", retailer_product_id=product_id, name=title,
            url=urljoin(self.base_url + "/", f"products/{handle}"), image_url=image,
            price=price, original_price=original_price, currency="USD", availability=availability,
            product_type="Mini Backpack", exclusive=exclusive,
            exclusive_retailer="Modern PinUp" if exclusive else None,
            new_release=any(tag.casefold() in {"new", "new arrival", "new arrivals"} for tag in tags),
            preorder=preorder,
            sku=str(available_variant.get("sku")).strip() or None,
            release=release,
        )

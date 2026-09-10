"""GeekCore UK adapter using Shopify's public, structured product feeds."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urljoin

from app.http import AsyncHttpClient, HttpClientError
from app.models import Availability, Product
from app.release import parse_release_text
from app.monitors.base import RetailerMonitor

BASE_URL = "https://www.geekcore.co.uk"
COLLECTION_HANDLES = ("loungefly-mini-backpacks", "loungefly-coming-soon")
PAGE_SIZE = 250
MAX_PAGES = 10


class GeekCoreParseError(ValueError):
    """The upstream Shopify representation was incomplete or malformed."""


class GeekCoreMonitor(RetailerMonitor):
    """Discover and check GeekCore mini backpacks without parsing theme CSS."""

    def __init__(self, http: AsyncHttpClient, *, base_url: str = BASE_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def discover_products(self) -> list[Product]:
        found: dict[str, Product] = {}
        for collection in COLLECTION_HANDLES:
            for page in range(1, MAX_PAGES + 1):
                url = (
                    f"{self.base_url}/collections/{collection}/products.json"
                    f"?limit={PAGE_SIZE}&page={page}"
                )
                payload = await self.http.get_json(url)
                products = self._product_list(payload)
                for raw in products:
                    if self._is_mini_backpack(raw):
                        product = self.parse_product(raw)
                        found[product.retailer_product_id] = product
                if len(products) < PAGE_SIZE:
                    break
            else:
                raise GeekCoreParseError("GeekCore collection exceeded the pagination safety limit")
        return list(found.values())

    async def check_product(self, product: Product) -> Product:
        handle = product.url.rstrip("/").rsplit("/", 1)[-1]
        if not handle:
            return replace(product, availability=Availability.ERROR)
        try:
            payload = await self.http.get_json(f"{self.base_url}/products/{quote(handle)}.js")
            return self.parse_product(payload)
        except (HttpClientError, GeekCoreParseError, ValueError, TypeError):
            # An unparseable response says nothing about inventory. It is never a sell-out.
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            payload = await self.http.get_json(
                f"{self.base_url}/collections/{COLLECTION_HANDLES[0]}/products.json?limit=1"
            )
            self._product_list(payload)
            return True
        except (HttpClientError, GeekCoreParseError):
            return False

    @staticmethod
    def _product_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise GeekCoreParseError("GeekCore response has no product list")
        if not all(isinstance(item, dict) for item in payload["products"]):
            raise GeekCoreParseError("GeekCore product list contains invalid entries")
        return payload["products"]

    @staticmethod
    def _is_mini_backpack(raw: dict[str, Any]) -> bool:
        title = raw.get("title")
        if not isinstance(title, str):
            return False
        normalized = " ".join(title.lower().replace("-", " ").split())
        excluded = ("keychain", "key chain", "charm", "wallet", "cardholder", "card holder")
        return "mini backpack" in normalized and not any(term in normalized for term in excluded)

    def parse_product(self, raw: object) -> Product:
        if not isinstance(raw, dict):
            raise GeekCoreParseError("Product is not an object")
        try:
            product_id = str(raw["id"])
            title = raw["title"].strip()
            handle = raw["handle"].strip()
            variants = raw["variants"]
        except (KeyError, AttributeError) as exc:
            raise GeekCoreParseError("Product identity is incomplete") from exc
        if not product_id or not title or not handle or not isinstance(variants, list) or not variants:
            raise GeekCoreParseError("Product identity or variants are incomplete")
        if not all(isinstance(variant, dict) and isinstance(variant.get("available"), bool) for variant in variants):
            raise GeekCoreParseError("Variant availability is missing")

        tags_raw = raw.get("tags", [])
        tags = [str(tag).strip() for tag in tags_raw] if isinstance(tags_raw, list) else []
        lowered_tags = {tag.lower() for tag in tags}
        preorder = any(tag.startswith(("pre-order", "preorder")) for tag in lowered_tags)
        coming_soon = any(tag.startswith("coming-soon") or tag.startswith("coming soon") for tag in lowered_tags)
        if preorder:
            availability = Availability.PREORDER
        elif coming_soon:
            availability = Availability.COMING_SOON
        elif any(variant["available"] for variant in variants):
            availability = Availability.IN_STOCK
        else:
            availability = Availability.OUT_OF_STOCK

        available_variant = next((variant for variant in variants if variant["available"]), variants[0])
        try:
            price = Decimal(str(available_variant["price"]))
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise GeekCoreParseError("Variant price is invalid") from exc
        images = raw.get("images")
        image_url: str | None = None
        if isinstance(images, list) and images:
            first_image = images[0]
            image_url = first_image.get("src") if isinstance(first_image, dict) else str(first_image)
        image_url = image_url or raw.get("featured_image")
        if isinstance(image_url, str) and image_url.startswith("//"):
            image_url = "https:" + image_url
        if image_url is not None and not isinstance(image_url, str):
            raise GeekCoreParseError("Product image is invalid")

        def tagged(prefix: str) -> str | None:
            return next((tag.split(":", 1)[1].strip() for tag in tags if tag.lower().startswith(prefix)), None)

        release = parse_release_text(
            " ".join([str(raw.get("body_html", "")), *tags]),
            source="GeekCore Shopify product feed", local_timezone="Europe/London",
        )

        return Product(
            retailer="GeekCore",
            retailer_product_id=product_id,
            name=title,
            url=urljoin(self.base_url + "/", f"products/{handle}"),
            image_url=image_url,
            price=price,
            currency="GBP",
            availability=availability,
            product_type="Mini Backpack",
            franchise=tagged("franchise:"),
            character=tagged("character:"),
            exclusive="geekcore exclusives" in lowered_tags,
            preorder=preorder,
            sku=str(available_variant.get("sku")).strip() if available_variant.get("sku") else None,
            release=release,
        )

"""Infinity Collectables UK adapter backed by its public Shopify product feeds."""

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

BASE_URL = "https://infinitycollectables.com"
COLLECTION = "loungefly"
PAGE_SIZE = 250
MAX_PAGES = 10


class InfinityCollectablesParseError(ValueError):
    """Infinity Collectables returned incomplete or malformed Shopify data."""


class InfinityCollectablesMonitor(RetailerMonitor):
    """Discover actual Loungefly mini backpacks in the retailer's Loungefly collection."""

    def __init__(self, http: AsyncHttpClient, *, base_url: str = BASE_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def discover_products(self) -> list[Product]:
        found: dict[str, Product] = {}
        for page in range(1, MAX_PAGES + 1):
            payload = await self.http.get_json(
                f"{self.base_url}/collections/{COLLECTION}/products.json"
                f"?limit={PAGE_SIZE}&page={page}"
            )
            products = self._product_list(payload)
            for raw in products:
                if self._is_loungefly_mini_backpack(raw):
                    product = self.parse_product(raw)
                    found[product.retailer_product_id] = product
            if len(products) < PAGE_SIZE:
                break
        else:
            raise InfinityCollectablesParseError(
                "Infinity Collectables Loungefly collection exceeded the pagination safety limit"
            )
        return list(found.values())

    async def check_product(self, product: Product) -> Product:
        handle = product.url.rstrip("/").rsplit("/", 1)[-1]
        try:
            if not handle:
                raise InfinityCollectablesParseError("Product handle is missing")
            return self.parse_product(
                await self.http.get_json(f"{self.base_url}/products/{quote(handle)}.js")
            )
        except HttpClientError as exc:
            state = Availability.UNAVAILABLE if exc.status == 404 else Availability.ERROR
            return replace(product, availability=state)
        except (InfinityCollectablesParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            self._product_list(await self.http.get_json(
                f"{self.base_url}/collections/{COLLECTION}/products.json?limit=1&page=1"
            ))
            return True
        except (HttpClientError, InfinityCollectablesParseError):
            return False

    @staticmethod
    def _product_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise InfinityCollectablesParseError("Infinity Collectables response has no product list")
        products = payload["products"]
        if not all(isinstance(item, dict) for item in products):
            raise InfinityCollectablesParseError("Infinity Collectables product list has invalid entries")
        return products

    @staticmethod
    def _tags(raw: dict[str, Any]) -> tuple[str, ...]:
        tags = raw.get("tags", [])
        if isinstance(tags, str):
            return tuple(tag.strip() for tag in tags.split(",") if tag.strip())
        if isinstance(tags, list) and all(isinstance(tag, str) for tag in tags):
            return tuple(tag.strip() for tag in tags if tag.strip())
        raise InfinityCollectablesParseError("Product tags are invalid")

    @classmethod
    def _is_loungefly_mini_backpack(cls, raw: dict[str, Any]) -> bool:
        title = raw.get("title")
        if not isinstance(title, str):
            return False
        normalized = " ".join(title.casefold().replace("-", " ").split())
        excluded = (
            "mystery", "keychain", "key chain", "charm", "wallet", "card holder",
            "cardholder", "crossbody", "cross body", "tote", "full size", "full sized",
            "bundle", "pin", "pins",
        )
        return (
            "loungefly" in normalized
            and "mini backpack" in normalized
            and not any(re.search(rf"\b{re.escape(term)}\b", normalized) for term in excluded)
        )

    def parse_product(self, raw: object) -> Product:
        if not isinstance(raw, dict):
            raise InfinityCollectablesParseError("Product is not an object")
        try:
            product_id = str(raw["id"]).strip()
            title = raw["title"].strip()
            handle = raw["handle"].strip()
            variants = raw["variants"]
        except (KeyError, AttributeError) as exc:
            raise InfinityCollectablesParseError("Product identity is incomplete") from exc
        if not all((product_id, title, handle)) or not isinstance(variants, list) or not variants:
            raise InfinityCollectablesParseError("Product identity or variants are incomplete")
        if not all(isinstance(v, dict) and isinstance(v.get("available"), bool) for v in variants):
            raise InfinityCollectablesParseError("Variant availability is missing")

        selected = next((variant for variant in variants if variant["available"]), variants[0])
        variant_id = str(selected.get("id", "")).strip()
        if not variant_id:
            raise InfinityCollectablesParseError("Variant identity is missing")
        try:
            price = self._money(selected["price"])
            compare = selected.get("compare_at_price")
            original_price = None if compare in (None, "") else self._money(compare)
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise InfinityCollectablesParseError("Variant pricing is invalid") from exc

        tags = self._tags(raw)
        description = raw.get("body_html", raw.get("description", "")) or ""
        if not isinstance(description, str):
            raise InfinityCollectablesParseError("Product description is invalid")
        evidence = " ".join((title, description, *tags))
        preorder = bool(re.search(r"\bpre[ -]?order(?:ed|ing)?\b", evidence, re.I))
        coming_soon = bool(re.search(r"\bcoming soon\b", evidence, re.I))
        available = any(variant["available"] for variant in variants)
        availability = (Availability.PREORDER if preorder and available else
                        Availability.COMING_SOON if coming_soon and not available else
                        Availability.IN_STOCK if available else Availability.OUT_OF_STOCK)

        published = raw.get("published_at")
        try:
            listing_published_at = (datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                                    if published is not None else None)
        except ValueError as exc:
            raise InfinityCollectablesParseError("Product publication timestamp is invalid") from exc
        if listing_published_at is not None and listing_published_at.tzinfo is None:
            raise InfinityCollectablesParseError("Product publication timestamp needs a timezone")

        images = raw.get("images", [])
        if not isinstance(images, list):
            raise InfinityCollectablesParseError("Product images are invalid")
        image = images[0].get("src") if images and isinstance(images[0], dict) else (
            images[0] if images else raw.get("featured_image")
        )
        if isinstance(image, str) and image.startswith("//"):
            image = "https:" + image
        if image is not None and not isinstance(image, str):
            raise InfinityCollectablesParseError("Product image is invalid")

        lowered_tags = {tag.casefold() for tag in tags}
        exclusive = any("exclusive" in tag for tag in lowered_tags) or bool(
            re.search(r"\binfinity(?: collectables)? exclusive\b", evidence, re.I)
        )
        vendor = raw.get("vendor")
        if vendor is not None and not isinstance(vendor, str):
            raise InfinityCollectablesParseError("Product vendor is invalid")
        return Product(
            retailer="Infinity Collectables", retailer_product_id=product_id,
            variant_id=variant_id,
            sku=str(selected["sku"]).strip() if selected.get("sku") not in (None, "") else None,
            name=title, url=urljoin(self.base_url + "/", f"products/{handle}"), image_url=image,
            price=price, original_price=original_price, currency="GBP", availability=availability,
            vendor=vendor, product_type="Mini Backpack", tags=tags,
            listing_published_at=listing_published_at, preorder=preorder,
            new_release=bool(lowered_tags & {"new", "new arrival", "new arrivals", "new product"}),
            exclusive=exclusive, exclusive_retailer="Infinity Collectables" if exclusive else None,
            release=parse_release_text(description, source="Infinity Collectables Shopify product data",
                                       local_timezone="Europe/London", date_order="DMY"),
        )

    @staticmethod
    def _money(value: object) -> Decimal:
        amount = (Decimal(value) / 100 if isinstance(value, int) and not isinstance(value, bool)
                  else Decimal(str(value)))
        if not amount.is_finite() or amount < 0:
            raise InvalidOperation
        return amount

"""707 Street US adapter backed by the retailer's public Shopify data."""

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

BASE_URL = "https://707street.com"
COLLECTION_HANDLE = "mini-backpacks"
PAGE_SIZE = 250
MAX_PAGES = 10


class Street707ParseError(ValueError):
    """The public Shopify representation was incomplete or malformed."""


class Street707Monitor(RetailerMonitor):
    """Discover Loungefly mini backpacks using Shopify's structured feeds."""

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
        raise Street707ParseError("707 Street collection exceeded the pagination safety limit")

    async def check_product(self, product: Product) -> Product:
        handle = product.url.rstrip("/").rsplit("/", 1)[-1]
        try:
            if not handle:
                raise Street707ParseError("Product handle is missing")
            return self.parse_product(
                await self.http.get_json(f"{self.base_url}/products/{quote(handle)}.js")
            )
        except HttpClientError as exc:
            if exc.status == 404:
                return replace(product, availability=Availability.UNAVAILABLE)
            return replace(product, availability=Availability.ERROR)
        except (Street707ParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            self._product_list(await self.http.get_json(
                f"{self.base_url}/collections/{COLLECTION_HANDLE}/products.json?limit=1&page=1"
            ))
            return True
        except (HttpClientError, Street707ParseError):
            return False

    @staticmethod
    def _product_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise Street707ParseError("707 Street response has no product list")
        products = payload["products"]
        if not all(isinstance(item, dict) for item in products):
            raise Street707ParseError("707 Street product list contains invalid entries")
        return products

    @staticmethod
    def _tags(raw: dict[str, Any]) -> tuple[str, ...]:
        value = raw.get("tags", [])
        if isinstance(value, str):
            return tuple(tag.strip() for tag in value.split(",") if tag.strip())
        if isinstance(value, list) and all(isinstance(tag, str) for tag in value):
            return tuple(tag.strip() for tag in value if tag.strip())
        raise Street707ParseError("Product tags are invalid")

    @classmethod
    def _is_loungefly_mini_backpack(cls, raw: dict[str, Any]) -> bool:
        title, vendor = raw.get("title"), raw.get("vendor")
        product_type = raw.get("product_type") or raw.get("type")
        if not all(isinstance(value, str) for value in (title, vendor, product_type)):
            return False
        title = " ".join(title.casefold().replace("-", " ").split())
        excluded = ("full size", "wallet", "crossbody", "sling", "keychain", "cardholder", "pin")
        return (
            vendor.casefold() == "loungefly"
            and product_type.casefold() == "backpack"
            and "mini backpack" in title
            and not any(re.search(rf"\b{re.escape(term)}\b", title) for term in excluded)
        )

    def parse_product(self, raw: object) -> Product:
        if not isinstance(raw, dict):
            raise Street707ParseError("Product is not an object")
        try:
            product_id = str(raw["id"]).strip()
            title = raw["title"].strip()
            handle = raw["handle"].strip()
            vendor = raw["vendor"].strip()
            product_type = (raw.get("product_type") or raw["type"]).strip()
            variants = raw["variants"]
        except (KeyError, AttributeError) as exc:
            raise Street707ParseError("Product identity is incomplete") from exc
        if not all((product_id, title, handle, vendor, product_type)) or not isinstance(variants, list):
            raise Street707ParseError("Product identity is incomplete")

        unique_variants: dict[str, dict[str, Any]] = {}
        for variant in variants:
            if not isinstance(variant, dict) or not isinstance(variant.get("available"), bool):
                raise Street707ParseError("Variant availability is missing")
            variant_id = str(variant.get("id", "")).strip()
            if not variant_id:
                raise Street707ParseError("Variant identity is missing")
            unique_variants.setdefault(variant_id, variant)
        if not unique_variants:
            raise Street707ParseError("Product has no variants")
        normalized_variants = list(unique_variants.values())
        selected = next((variant for variant in normalized_variants if variant["available"]), normalized_variants[0])
        try:
            # Collection JSON uses decimal strings; product.js uses integer cents.
            raw_price = selected["price"]
            price = (Decimal(raw_price) / 100 if isinstance(raw_price, int)
                     and not isinstance(raw_price, bool) else Decimal(str(raw_price)))
            compare = selected.get("compare_at_price")
            original_price = None if compare in (None, "") else (
                Decimal(compare) / 100 if isinstance(compare, int) and not isinstance(compare, bool)
                else Decimal(str(compare))
            )
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise Street707ParseError("Variant pricing is invalid") from exc
        if not price.is_finite() or price < 0:
            raise Street707ParseError("Variant pricing is invalid")

        tags = self._tags(raw)
        description = str(raw.get("body_html", raw.get("description", "")))
        preorder = bool(re.search(r"\bpre[ -]?order\b", " ".join((title, description)), re.I))
        availability = (Availability.PREORDER if preorder else Availability.IN_STOCK
                        if any(variant["available"] for variant in normalized_variants)
                        else Availability.OUT_OF_STOCK)
        published = raw.get("published_at")
        try:
            listing_published_at = (datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                                    if published is not None else None)
        except ValueError as exc:
            raise Street707ParseError("Product publication timestamp is invalid") from exc
        if listing_published_at is not None and listing_published_at.tzinfo is None:
            raise Street707ParseError("Product publication timestamp must include a timezone")

        images = raw.get("images", [])
        image = images[0].get("src") if images and isinstance(images[0], dict) else (
            images[0] if images else raw.get("featured_image")
        )
        if isinstance(image, str) and image.startswith("//"):
            image = "https:" + image
        if image is not None and not isinstance(image, str):
            raise Street707ParseError("Product image is invalid")
        sku, barcode = selected.get("sku"), selected.get("barcode")
        exclusive = "exclusive" in {tag.casefold() for tag in tags} or "707 street exclusive" in title.casefold()
        return Product(
            retailer="707 Street", retailer_product_id=product_id, variant_id=str(selected["id"]),
            sku=str(sku).strip() if sku not in (None, "") else None,
            barcode=str(barcode).strip() if barcode not in (None, "") else None,
            name=title, url=urljoin(self.base_url + "/", f"products/{handle}"), image_url=image,
            price=price, original_price=original_price, currency="USD", availability=availability,
            vendor=vendor, product_type="Mini Backpack", tags=tags,
            listing_published_at=listing_published_at, preorder=preorder,
            exclusive=exclusive, exclusive_retailer="707 Street" if exclusive else None,
            # Collection wave tags remain metadata. Only explicit prose is release evidence.
            release=parse_release_text(description, source="707 Street Shopify product data",
                                       local_timezone="America/Los_Angeles", date_order="MDY"),
        )

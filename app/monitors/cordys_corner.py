"""Cordy's Corner US adapter backed by the storefront's public Shopify feeds."""

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

BASE_URL = "https://cordyscorner.com"
COLLECTIONS = ("loungefly-backpack", "shop-exclusive-loungefly")
PAGE_SIZE = 250
MAX_PAGES = 10


class CordysCornerParseError(ValueError):
    """Cordy's Corner returned incomplete or malformed Shopify data."""


class CordysCornerMonitor(RetailerMonitor):
    """Discover Loungefly mini backpacks, including the exclusive collection."""

    def __init__(self, http: AsyncHttpClient, *, base_url: str = BASE_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def discover_products(self) -> list[Product]:
        found: dict[str, Product] = {}
        for collection in COLLECTIONS:
            for page in range(1, MAX_PAGES + 1):
                payload = await self.http.get_json(
                    f"{self.base_url}/collections/{collection}/products.json"
                    f"?limit={PAGE_SIZE}&page={page}"
                )
                products = self._product_list(payload)
                for raw in products:
                    if self._is_loungefly_mini_backpack(raw):
                        product = self.parse_product(
                            raw, exclusive_collection=collection == "shop-exclusive-loungefly"
                        )
                        previous = found.get(product.retailer_product_id)
                        # An item can occur in both collections. Preserve the exclusive
                        # collection's stronger classification regardless of traversal order.
                        if previous is None or product.exclusive or not previous.exclusive:
                            found[product.retailer_product_id] = product
                if len(products) < PAGE_SIZE:
                    break
            else:
                raise CordysCornerParseError(
                    f"Cordy's Corner collection {collection} exceeded the pagination safety limit"
                )
        return list(found.values())

    async def check_product(self, product: Product) -> Product:
        handle = product.url.rstrip("/").rsplit("/", 1)[-1]
        try:
            if not handle:
                raise CordysCornerParseError("Product handle is missing")
            checked = self.parse_product(
                await self.http.get_json(f"{self.base_url}/products/{quote(handle)}.js")
            )
            if product.exclusive and not checked.exclusive:
                checked = replace(checked, exclusive=True, exclusive_retailer="Cordy's Corner")
            return checked
        except HttpClientError as exc:
            state = Availability.UNAVAILABLE if exc.status == 404 else Availability.ERROR
            return replace(product, availability=state)
        except (CordysCornerParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            self._product_list(await self.http.get_json(
                f"{self.base_url}/collections/{COLLECTIONS[0]}/products.json?limit=1&page=1"
            ))
            return True
        except (HttpClientError, CordysCornerParseError):
            return False

    @staticmethod
    def _product_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise CordysCornerParseError("Cordy's Corner response has no product list")
        products = payload["products"]
        if not all(isinstance(item, dict) for item in products):
            raise CordysCornerParseError("Cordy's Corner product list contains invalid entries")
        return products

    @staticmethod
    def _tags(raw: dict[str, Any]) -> tuple[str, ...]:
        tags = raw.get("tags", [])
        if isinstance(tags, str):
            return tuple(tag.strip() for tag in tags.split(",") if tag.strip())
        if isinstance(tags, list) and all(isinstance(tag, str) for tag in tags):
            return tuple(tag.strip() for tag in tags if tag.strip())
        raise CordysCornerParseError("Product tags are invalid")

    @classmethod
    def _is_loungefly_mini_backpack(cls, raw: dict[str, Any]) -> bool:
        title, vendor = raw.get("title"), raw.get("vendor")
        product_type = raw.get("product_type") or raw.get("type")
        if not all(isinstance(value, str) for value in (title, vendor, product_type)):
            return False
        normalized = " ".join(title.casefold().replace("-", " ").split())
        excluded = (
            "mystery", "bundle", "wallet", "crossbody", "apparel", "shirt", "hoodie",
            "accessory", "accessories", "charm", "keychain", "cardholder", "full size",
        )
        return (
            vendor.casefold() == "loungefly"
            and product_type.casefold() == "backpack"
            and "mini backpack" in normalized
            and not any(re.search(rf"\b{re.escape(term)}\b", normalized) for term in excluded)
        )

    def parse_product(self, raw: object, *, exclusive_collection: bool = False) -> Product:
        if not isinstance(raw, dict):
            raise CordysCornerParseError("Product is not an object")
        try:
            product_id = str(raw["id"]).strip()
            title = raw["title"].strip()
            handle = raw["handle"].strip()
            vendor = raw["vendor"].strip()
            variants = raw["variants"]
        except (KeyError, AttributeError) as exc:
            raise CordysCornerParseError("Product identity is incomplete") from exc
        if not all((product_id, title, handle, vendor)) or not isinstance(variants, list) or not variants:
            raise CordysCornerParseError("Product identity or variants are incomplete")
        if not all(isinstance(v, dict) and isinstance(v.get("available"), bool) for v in variants):
            raise CordysCornerParseError("Variant availability is missing")

        selected = next((variant for variant in variants if variant["available"]), variants[0])
        variant_id = str(selected.get("id", "")).strip()
        if not variant_id:
            raise CordysCornerParseError("Variant identity is missing")
        try:
            raw_price = selected["price"]
            price = (Decimal(raw_price) / 100 if isinstance(raw_price, int)
                     and not isinstance(raw_price, bool) else Decimal(str(raw_price)))
            compare = selected.get("compare_at_price")
            original_price = None if compare in (None, "") else (
                Decimal(compare) / 100 if isinstance(compare, int) and not isinstance(compare, bool)
                else Decimal(str(compare))
            )
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise CordysCornerParseError("Variant pricing is invalid") from exc
        if not price.is_finite() or price < 0 or (
            original_price is not None and (not original_price.is_finite() or original_price < 0)
        ):
            raise CordysCornerParseError("Variant pricing is invalid")

        tags = self._tags(raw)
        lowered_tags = {tag.casefold() for tag in tags}
        description = raw.get("body_html", raw.get("description", "")) or ""
        if not isinstance(description, str):
            raise CordysCornerParseError("Product description is invalid")
        preorder = bool(re.search(r"\bpre[ -]?order\b", " ".join((title, description, *tags)), re.I))
        availability = (Availability.PREORDER if preorder else Availability.IN_STOCK
                        if any(variant["available"] for variant in variants)
                        else Availability.OUT_OF_STOCK)
        published = raw.get("published_at")
        try:
            listing_published_at = (datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                                    if published is not None else None)
        except ValueError as exc:
            raise CordysCornerParseError("Product publication timestamp is invalid") from exc
        if listing_published_at is not None and listing_published_at.tzinfo is None:
            raise CordysCornerParseError("Product publication timestamp must include a timezone")

        images = raw.get("images", [])
        image = images[0].get("src") if images and isinstance(images[0], dict) else (
            images[0] if images else raw.get("featured_image")
        )
        if isinstance(image, str) and image.startswith("//"):
            image = "https:" + image
        if image is not None and not isinstance(image, str):
            raise CordysCornerParseError("Product image is invalid")
        exclusive = exclusive_collection or "shop exclusives" in lowered_tags or bool(
            re.search(r"\b(?:cordy's corner|shop) exclusive\b", title, re.I)
        )
        return Product(
            retailer="Cordy's Corner", retailer_product_id=product_id,
            variant_id=variant_id,
            sku=str(selected["sku"]).strip() if selected.get("sku") not in (None, "") else None,
            name=title, url=urljoin(self.base_url + "/", f"products/{handle}"), image_url=image,
            price=price, original_price=original_price, currency="USD", availability=availability,
            vendor=vendor, product_type="Mini Backpack", tags=tags,
            listing_published_at=listing_published_at, preorder=preorder,
            new_release=bool(lowered_tags & {"new", "new-arrival", "new arrival", "new arrivals"}),
            exclusive=exclusive, exclusive_retailer="Cordy's Corner" if exclusive else None,
            release=parse_release_text(description, source="Cordy's Corner Shopify product data",
                                       local_timezone="America/Chicago", date_order="MDY"),
        )

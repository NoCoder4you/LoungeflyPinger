"""Geek Garage UK adapter using the retailer's public Shopify commerce data."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import unescape
import re
from typing import Any
from urllib.parse import quote, urljoin

from app.http import AsyncHttpClient, HttpClientError
from app.models import Availability, Product
from app.monitors.base import RetailerMonitor
from app.release import parse_release_text

BASE_URL = "https://geekgarage.co.uk"
COLLECTION = "loungefly"
PAGE_SIZE = 250
MAX_PAGES = 10


class GeekGarageParseError(ValueError):
    """Geek Garage returned incomplete or malformed Shopify data."""


class GeekGarageMonitor(RetailerMonitor):
    """Discover real Loungefly mini backpacks, excluding charms and accessories."""

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
            raise GeekGarageParseError("Geek Garage collection exceeded pagination limit")
        return list(found.values())

    async def check_product(self, product: Product) -> Product:
        handle = product.url.rstrip("/").rsplit("/", 1)[-1]
        try:
            if not handle:
                raise GeekGarageParseError("Product handle is missing")
            return self.parse_product(
                await self.http.get_json(f"{self.base_url}/products/{quote(handle)}.js")
            )
        except HttpClientError as exc:
            return replace(product, availability=(
                Availability.UNAVAILABLE if exc.status == 404 else Availability.ERROR
            ))
        except (GeekGarageParseError, ValueError, TypeError):
            # Invalid commerce data must never be interpreted as a sell-out.
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            self._product_list(await self.http.get_json(
                f"{self.base_url}/collections/{COLLECTION}/products.json?limit=1&page=1"
            ))
            return True
        except (HttpClientError, GeekGarageParseError):
            return False

    @staticmethod
    def _product_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise GeekGarageParseError("Geek Garage response has no product list")
        products = payload["products"]
        if not all(isinstance(item, dict) for item in products):
            raise GeekGarageParseError("Geek Garage product list has invalid entries")
        return products

    @staticmethod
    def _plain_description(raw: dict[str, Any]) -> str:
        value = raw.get("body_html", raw.get("description", "")) or ""
        if not isinstance(value, str):
            raise GeekGarageParseError("Product description is invalid")
        return " ".join(unescape(re.sub(r"<[^>]*>", " ", value)).split())

    @classmethod
    def _is_loungefly_mini_backpack(cls, raw: dict[str, Any]) -> bool:
        # Shopify's vendor and product type are stronger brand/category evidence than the title.
        vendor = raw.get("vendor")
        product_type = raw.get("product_type")
        if not isinstance(vendor, str) or not isinstance(product_type, str):
            return False
        if "loungefly" not in vendor.casefold() or "loungefly" not in product_type.casefold():
            return False
        title = raw.get("title")
        if not isinstance(title, str):
            return False
        evidence = f"{title} {cls._plain_description(raw)}".casefold().replace("-", " ")
        excluded = (
            "keychain", "key chain", "bag charm", "mystery mini backpack", "insert organiser",
            "wallet", "card holder", "cardholder", "pin", "crossbody", "tote",
        )
        return "mini backpack" in evidence and not any(term in evidence for term in excluded)

    def parse_product(self, raw: object) -> Product:
        if not isinstance(raw, dict):
            raise GeekGarageParseError("Product is not an object")
        try:
            product_id = str(raw["id"]).strip()
            title = raw["title"].strip()
            handle = raw["handle"].strip()
            variants = raw["variants"]
        except (KeyError, AttributeError) as exc:
            raise GeekGarageParseError("Product identity is incomplete") from exc
        if not all((product_id, title, handle)) or not isinstance(variants, list) or not variants:
            raise GeekGarageParseError("Product identity or variants are incomplete")
        if not all(isinstance(v, dict) and isinstance(v.get("available"), bool) for v in variants):
            raise GeekGarageParseError("Variant availability is missing")
        selected = next((variant for variant in variants if variant["available"]), variants[0])
        variant_id = str(selected.get("id", "")).strip()
        if not variant_id:
            raise GeekGarageParseError("Variant identity is missing")
        try:
            price = self._money(selected["price"])
            compare = selected.get("compare_at_price")
            original_price = None if compare in (None, "") else self._money(compare)
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise GeekGarageParseError("Variant pricing is invalid") from exc

        description = self._plain_description(raw)
        tags_raw = raw.get("tags", [])
        if not isinstance(tags_raw, list) or not all(isinstance(tag, str) for tag in tags_raw):
            raise GeekGarageParseError("Product tags are invalid")
        tags = tuple(tag.strip() for tag in tags_raw if tag.strip())
        evidence = " ".join((title, description, *tags))
        preorder = bool(re.search(r"\bpre[ -]?order(?:ed|ing)?\b", evidence, re.I))
        available = any(variant["available"] for variant in variants)
        availability = (Availability.PREORDER if preorder else
                        Availability.IN_STOCK if available else Availability.OUT_OF_STOCK)

        emea_exclusive = bool(re.search(r"\bEMEA\s+EXCLUSIVE\b", evidence, re.I))
        retailer_exclusive = bool(re.search(r"\bonly available at Geek Garage\b", evidence, re.I))

        published = raw.get("published_at")
        try:
            listing_published_at = (datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                                    if published is not None else None)
        except ValueError as exc:
            raise GeekGarageParseError("Product publication timestamp is invalid") from exc
        if listing_published_at is not None and listing_published_at.tzinfo is None:
            raise GeekGarageParseError("Product publication timestamp needs a timezone")

        images = raw.get("images", [])
        if not isinstance(images, list):
            raise GeekGarageParseError("Product images are invalid")
        image = images[0].get("src") if images and isinstance(images[0], dict) else (
            images[0] if images else raw.get("featured_image")
        )
        if isinstance(image, str) and image.startswith("//"):
            image = "https:" + image
        if image is not None and not isinstance(image, str):
            raise GeekGarageParseError("Product image is invalid")

        release_text = re.sub(r"\b(?:stock\s+)?due\b", "release", description, flags=re.I)
        vendor = raw.get("vendor")
        if vendor is not None and not isinstance(vendor, str):
            raise GeekGarageParseError("Product vendor is invalid")
        return Product(
            retailer="Geek Garage", retailer_product_id=product_id, variant_id=variant_id,
            sku=str(selected["sku"]).strip() if selected.get("sku") not in (None, "") else None,
            name=title, url=urljoin(self.base_url + "/", f"products/{handle}"), image_url=image,
            price=price, original_price=original_price, currency="GBP", availability=availability,
            vendor=vendor, product_type="Mini Backpack", tags=tags,
            listing_published_at=listing_published_at, preorder=preorder,
            exclusive=emea_exclusive or retailer_exclusive,
            exclusive_region="EMEA" if emea_exclusive else None,
            exclusive_retailer="Geek Garage" if retailer_exclusive else None,
            release=parse_release_text(release_text, source="Geek Garage Shopify product data",
                                       local_timezone="Europe/London", date_order="DMY"),
        )

    @staticmethod
    def _money(value: object) -> Decimal:
        amount = (Decimal(value) / 100 if isinstance(value, int) and not isinstance(value, bool)
                  else Decimal(str(value)))
        if not amount.is_finite() or amount < 0:
            raise InvalidOperation
        return amount

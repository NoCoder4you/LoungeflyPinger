"""Razmatazz UK adapter using the retailer's public Shopify catalogue."""

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

BASE_URL = "https://www.razmatazz.co.uk"
COLLECTION = "loungefly"
PAGE_SIZE = 250
MAX_PAGES = 10


class RazmatazzParseError(ValueError):
    """Razmatazz returned incomplete or malformed Shopify data."""


class RazmatazzMonitor(RetailerMonitor):
    """Discover Loungefly mini backpacks from the retailer's brand collection."""

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
                return list(found.values())
        raise RazmatazzParseError("Razmatazz collection exceeded the pagination safety limit")

    async def check_product(self, product: Product) -> Product:
        handle = product.url.rstrip("/").rsplit("/", 1)[-1]
        try:
            if not handle:
                raise RazmatazzParseError("Product handle is missing")
            return self.parse_product(
                await self.http.get_json(f"{self.base_url}/products/{quote(handle)}.js")
            )
        except HttpClientError as exc:
            return replace(product, availability=(
                Availability.UNAVAILABLE if exc.status == 404 else Availability.ERROR
            ))
        except (RazmatazzParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            self._product_list(await self.http.get_json(
                f"{self.base_url}/collections/{COLLECTION}/products.json?limit=1&page=1"
            ))
            return True
        except (HttpClientError, RazmatazzParseError):
            return False

    @staticmethod
    def _product_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise RazmatazzParseError("Razmatazz response has no product list")
        products = payload["products"]
        if not all(isinstance(item, dict) for item in products):
            raise RazmatazzParseError("Razmatazz product list has invalid entries")
        return products

    @staticmethod
    def _description(raw: dict[str, Any]) -> str:
        value = raw.get("body_html", raw.get("description", "")) or ""
        if not isinstance(value, str):
            raise RazmatazzParseError("Product description is invalid")
        return " ".join(unescape(re.sub(r"<[^>]*>", " ", value)).split())

    @classmethod
    def _is_loungefly_mini_backpack(cls, raw: dict[str, Any]) -> bool:
        title = raw.get("title")
        if not isinstance(title, str):
            return False
        # The current feed leaves product_type blank and uses supplier names as vendor;
        # collection membership plus the explicit brand/category title is the strongest data.
        normalized_title = title.casefold().replace("-", " ")
        excluded = ("keychain", "key chain", "charm", "micro mini backpack", "wallet",
                    "purse", "crossbody", "tote", "cardholder", "card holder")
        return ("loungefly" in normalized_title and "mini backpack" in normalized_title
                and not any(term in normalized_title for term in excluded))

    def parse_product(self, raw: object) -> Product:
        if not isinstance(raw, dict):
            raise RazmatazzParseError("Product is not an object")
        try:
            product_id = str(raw["id"]).strip()
            title = raw["title"].strip()
            handle = raw["handle"].strip()
            variants = raw["variants"]
        except (KeyError, AttributeError) as exc:
            raise RazmatazzParseError("Product identity is incomplete") from exc
        if not all((product_id, title, handle)) or not isinstance(variants, list) or not variants:
            raise RazmatazzParseError("Product identity or variants are incomplete")
        if not all(isinstance(v, dict) and isinstance(v.get("available"), bool) for v in variants):
            raise RazmatazzParseError("Variant availability is missing")
        selected = next((variant for variant in variants if variant["available"]), variants[0])
        variant_id = str(selected.get("id", "")).strip()
        if not variant_id:
            raise RazmatazzParseError("Variant identity is missing")
        try:
            price = self._money(selected["price"])
            compare_value = selected.get("compare_at_price")
            compare = None if compare_value in (None, "") else self._money(compare_value)
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise RazmatazzParseError("Variant pricing is invalid") from exc
        original_price = compare if compare is not None and compare > price else None

        tags_raw = raw.get("tags", [])
        if isinstance(tags_raw, str):
            tags = tuple(tag.strip() for tag in tags_raw.split(",") if tag.strip())
        elif isinstance(tags_raw, list) and all(isinstance(tag, str) for tag in tags_raw):
            tags = tuple(tag.strip() for tag in tags_raw if tag.strip())
        else:
            raise RazmatazzParseError("Product tags are invalid")
        description = self._description(raw)
        evidence = " ".join((title, description, *tags))
        preorder = bool(re.search(r"\bpre[ -]?order(?:ed|ing)?\b", evidence, re.I))
        available = any(variant["available"] for variant in variants)
        availability = (Availability.PREORDER if preorder else
                        Availability.IN_STOCK if available else Availability.OUT_OF_STOCK)

        published = raw.get("published_at")
        try:
            published_at = (datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                            if published is not None else None)
        except ValueError as exc:
            raise RazmatazzParseError("Product publication timestamp is invalid") from exc
        if published_at is not None and published_at.tzinfo is None:
            raise RazmatazzParseError("Product publication timestamp needs a timezone")
        images = raw.get("images", [])
        if not isinstance(images, list):
            raise RazmatazzParseError("Product images are invalid")
        image = images[0].get("src") if images and isinstance(images[0], dict) else (
            images[0] if images else raw.get("featured_image")
        )
        if isinstance(image, str) and image.startswith("//"):
            image = "https:" + image
        if image is not None and not isinstance(image, str):
            raise RazmatazzParseError("Product image is invalid")
        vendor = raw.get("vendor")
        if vendor is not None and not isinstance(vendor, str):
            raise RazmatazzParseError("Product vendor is invalid")

        release_text = re.sub(r"\b(?:stock\s+)?due\b", "release", evidence, flags=re.I)
        return Product(
            retailer="Razmatazz UK", retailer_product_id=product_id, variant_id=variant_id,
            sku=str(selected["sku"]).strip() if selected.get("sku") not in (None, "") else None,
            name=title, url=urljoin(self.base_url + "/", f"products/{handle}"), image_url=image,
            price=price, original_price=original_price, currency="GBP", availability=availability,
            vendor=(vendor.strip() or None) if vendor is not None else None,
            product_type="Mini Backpack", tags=tags, listing_published_at=published_at,
            preorder=preorder,
            release=parse_release_text(release_text, source="Razmatazz Shopify product data",
                                       local_timezone="Europe/London", date_order="DMY"),
        )

    @staticmethod
    def _money(value: object) -> Decimal:
        # Shopify's collection feed uses decimal strings while product.js uses integer pence.
        amount = (Decimal(value) / 100 if isinstance(value, int) and not isinstance(value, bool)
                  else Decimal(str(value)))
        if not amount.is_finite() or amount < 0:
            raise InvalidOperation
        return amount

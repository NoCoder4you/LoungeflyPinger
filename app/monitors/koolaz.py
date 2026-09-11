"""Koolaz UK adapter using the shop's public Shopify product feeds."""

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

BASE_URL = "https://koolaz.co.uk"
COLLECTION = "loungefly"
PAGE_SIZE = 250
MAX_PAGES = 10


class KoolazParseError(ValueError):
    """Koolaz returned incomplete or malformed Shopify data."""


class KoolazMonitor(RetailerMonitor):
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
                # Collection membership and Shopify vendor/type are stronger evidence
                # than promotional wording in a product title.
                if self._is_loungefly_mini_backpack(raw):
                    product = self.parse_product(raw)
                    found[product.retailer_product_id] = product
            if len(products) < PAGE_SIZE:
                return list(found.values())
        raise KoolazParseError("Koolaz collection exceeded the pagination safety limit")

    async def check_product(self, product: Product) -> Product:
        handle = product.url.rstrip("/").rsplit("/", 1)[-1]
        try:
            if not handle:
                raise KoolazParseError("Product handle is missing")
            return self.parse_product(
                await self.http.get_json(f"{self.base_url}/products/{quote(handle)}.js")
            )
        except HttpClientError as exc:
            return replace(product, availability=(
                Availability.UNAVAILABLE if exc.status == 404 else Availability.ERROR
            ))
        except (KoolazParseError, ValueError, TypeError):
            # Broken product data is not evidence that a product sold out.
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            self._product_list(await self.http.get_json(
                f"{self.base_url}/collections/{COLLECTION}/products.json?limit=1&page=1"
            ))
            return True
        except (HttpClientError, KoolazParseError):
            return False

    @staticmethod
    def _product_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise KoolazParseError("Koolaz response has no product list")
        products = payload["products"]
        if not all(isinstance(item, dict) for item in products):
            raise KoolazParseError("Koolaz product list has invalid entries")
        return products

    @staticmethod
    def _description(raw: dict[str, Any]) -> str:
        value = raw.get("body_html", raw.get("description", "")) or ""
        if not isinstance(value, str):
            raise KoolazParseError("Product description is invalid")
        return " ".join(unescape(re.sub(r"<[^>]*>", " ", value)).split())

    @classmethod
    def _is_loungefly_mini_backpack(cls, raw: dict[str, Any]) -> bool:
        vendor, product_type, title = raw.get("vendor"), raw.get("product_type"), raw.get("title")
        if not all(isinstance(value, str) for value in (vendor, product_type, title)):
            return False
        structured_brand = "loungefly" in vendor.casefold()
        structured_category = product_type.casefold() == "loungefly backpack"
        evidence = f"{title} {cls._description(raw)}".casefold().replace("-", " ")
        excluded = ("wallet", "purse", "crossbody", "tote", "pin", "cardholder",
                    "card holder", "keychain", "key chain", "charm")
        return (structured_brand and structured_category and "mini backpack" in evidence
                and not any(term in evidence for term in excluded))

    def parse_product(self, raw: object) -> Product:
        if not isinstance(raw, dict):
            raise KoolazParseError("Product is not an object")
        try:
            product_id = str(raw["id"]).strip()
            title = raw["title"].strip()
            handle = raw["handle"].strip()
            variants = raw["variants"]
        except (KeyError, AttributeError) as exc:
            raise KoolazParseError("Product identity is incomplete") from exc
        if not all((product_id, title, handle)) or not isinstance(variants, list) or not variants:
            raise KoolazParseError("Product identity or variants are incomplete")
        if not all(isinstance(v, dict) and isinstance(v.get("available"), bool) for v in variants):
            raise KoolazParseError("Variant availability is missing")

        selected = next((variant for variant in variants if variant["available"]), variants[0])
        variant_id = str(selected.get("id", "")).strip()
        if not variant_id:
            raise KoolazParseError("Variant identity is missing")
        try:
            price = Decimal(str(selected["price"]))
            compare_value = selected.get("compare_at_price")
            compare = None if compare_value in (None, "") else Decimal(str(compare_value))
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise KoolazParseError("Variant pricing is invalid") from exc
        if not price.is_finite() or price < 0 or (compare is not None and
                                                   (not compare.is_finite() or compare < 0)):
            raise KoolazParseError("Variant pricing is invalid")
        # A compare-at value only represents a sale when it is above the actual price.
        original_price = compare if compare is not None and compare > price else None

        tags_raw = raw.get("tags", [])
        if isinstance(tags_raw, str):
            tags = tuple(tag.strip() for tag in tags_raw.split(",") if tag.strip())
        elif isinstance(tags_raw, list) and all(isinstance(tag, str) for tag in tags_raw):
            tags = tuple(tag.strip() for tag in tags_raw if tag.strip())
        else:
            raise KoolazParseError("Product tags are invalid")
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
            raise KoolazParseError("Product publication timestamp is invalid") from exc
        if published_at is not None and published_at.tzinfo is None:
            raise KoolazParseError("Product publication timestamp needs a timezone")
        vendor = raw.get("vendor")
        if vendor is not None and not isinstance(vendor, str):
            raise KoolazParseError("Product vendor is invalid")
        image = raw.get("featured_image")
        images = raw.get("images", [])
        if not isinstance(images, list):
            raise KoolazParseError("Product images are invalid")
        if images:
            image = images[0].get("src") if isinstance(images[0], dict) else images[0]
        if isinstance(image, str) and image.startswith("//"):
            image = "https:" + image
        if image is not None and not isinstance(image, str):
            raise KoolazParseError("Product image is invalid")

        return Product(
            retailer="Koolaz UK", retailer_product_id=product_id, variant_id=variant_id,
            sku=str(selected["sku"]).strip() if selected.get("sku") not in (None, "") else None,
            name=title, url=urljoin(self.base_url + "/", f"products/{handle}"), image_url=image,
            price=price, original_price=original_price, currency="GBP", availability=availability,
            vendor=(vendor.strip() or None) if vendor is not None else None,
            product_type="Mini Backpack", tags=tags,
            listing_published_at=published_at, preorder=preorder,
            new_release=any(tag.casefold() in {"new", "new loungefly", "new arrival",
                                               "new arrivals"} for tag in tags),
            release=parse_release_text(evidence, source="Koolaz Shopify product data",
                                       local_timezone="Europe/London", date_order="DMY"),
        )

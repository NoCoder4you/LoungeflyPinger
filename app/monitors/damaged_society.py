"""Damaged Society UK adapter using the store's public Shopify data."""

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

BASE_URL = "https://damagedsociety.co.uk"
COLLECTION = "loungefly"
PAGE_SIZE = 250
MAX_PAGES = 10

MINI_BACKPACK = "MINI_BACKPACK"
FULL_SIZE_BACKPACK = "FULL_SIZE_BACKPACK"
CROSSBODY = "CROSSBODY"
WALLET = "WALLET"
CARD_HOLDER = "CARD_HOLDER"


class DamagedSocietyParseError(ValueError):
    """Damaged Society returned incomplete or malformed Shopify data."""


class DamagedSocietyMonitor(RetailerMonitor):
    """Discover only mini backpacks while retaining explicit bag classification."""

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
                if self.classify_product(raw) == MINI_BACKPACK:
                    product = self.parse_product(raw)
                    found[product.retailer_product_id] = product
            if len(products) < PAGE_SIZE:
                return list(found.values())
        raise DamagedSocietyParseError("collection exceeded the pagination safety limit")

    async def check_product(self, product: Product) -> Product:
        handle = product.url.rstrip("/").rsplit("/", 1)[-1]
        try:
            if not handle:
                raise DamagedSocietyParseError("product handle is missing")
            raw = await self.http.get_json(f"{self.base_url}/products/{quote(handle)}.js")
            if not isinstance(raw, dict):
                raise DamagedSocietyParseError("product is not an object")
            if not isinstance(raw.get("title"), str) or not isinstance(
                raw.get("product_type") or raw.get("type"), str
            ):
                raise DamagedSocietyParseError("product classification is missing")
            if self.classify_product(raw) != MINI_BACKPACK:
                return replace(product, availability=Availability.UNAVAILABLE)
            return self.parse_product(raw)
        except HttpClientError as exc:
            return replace(product, availability=(
                Availability.UNAVAILABLE if exc.status == 404 else Availability.ERROR
            ))
        except (DamagedSocietyParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            self._product_list(await self.http.get_json(
                f"{self.base_url}/collections/{COLLECTION}/products.json?limit=1&page=1"
            ))
            return True
        except (HttpClientError, DamagedSocietyParseError):
            return False

    @staticmethod
    def _product_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise DamagedSocietyParseError("response has no product list")
        products = payload["products"]
        if not all(isinstance(item, dict) for item in products):
            raise DamagedSocietyParseError("product list contains invalid entries")
        return products

    @staticmethod
    def classify_product(raw: dict[str, Any]) -> str | None:
        """Classify the collection using Shopify type first and precise title cues."""
        title = raw.get("title")
        product_type = raw.get("product_type") or raw.get("type")
        if not isinstance(title, str) or not isinstance(product_type, str):
            return None
        normalized_title = " ".join(title.casefold().replace("-", " ").split())
        normalized_type = product_type.casefold().strip()
        if re.search(r"\bcard\s*holder\b", normalized_title) or normalized_type == "card holders":
            return CARD_HOLDER
        if "crossbody" in normalized_title or normalized_type == "crossbody bags":
            return CROSSBODY
        if (re.search(r"\b(?:wallet|purse)\b", normalized_title)
                or normalized_type in {"wallets", "purses"}):
            return WALLET
        if normalized_type == "backpacks":
            if re.search(r"\bmini backpack\b", normalized_title):
                return MINI_BACKPACK
            return FULL_SIZE_BACKPACK
        return None

    @staticmethod
    def _description(raw: dict[str, Any]) -> str:
        value = raw.get("body_html", raw.get("description", "")) or ""
        if not isinstance(value, str):
            raise DamagedSocietyParseError("product description is invalid")
        return " ".join(unescape(re.sub(r"<[^>]*>", " ", value)).split())

    @staticmethod
    def _money(value: object, *, cents: bool) -> Decimal:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError) as exc:
            raise DamagedSocietyParseError("variant pricing is invalid") from exc
        if cents:
            amount /= 100
        if not amount.is_finite() or amount < 0:
            raise DamagedSocietyParseError("variant pricing is invalid")
        return amount

    def parse_product(self, raw: object) -> Product:
        if not isinstance(raw, dict):
            raise DamagedSocietyParseError("product is not an object")
        try:
            product_id = str(raw["id"]).strip()
            title = raw["title"].strip()
            handle = raw["handle"].strip()
            variants = raw["variants"]
        except (KeyError, AttributeError) as exc:
            raise DamagedSocietyParseError("product identity is incomplete") from exc
        if not all((product_id, title, handle)) or not isinstance(variants, list) or not variants:
            raise DamagedSocietyParseError("product identity or variants are incomplete")
        if not all(isinstance(v, dict) and isinstance(v.get("available"), bool) for v in variants):
            raise DamagedSocietyParseError("variant availability is missing")

        selected = next((variant for variant in variants if variant["available"]), variants[0])
        variant_id = str(selected.get("id", "")).strip()
        if not variant_id or "price" not in selected:
            raise DamagedSocietyParseError("variant identity or pricing is missing")
        cents = isinstance(selected["price"], int)
        price = self._money(selected["price"], cents=cents)
        compare_value = selected.get("compare_at_price")
        compare = None if compare_value in (None, "") else self._money(compare_value, cents=cents)
        original_price = compare if compare is not None and compare > price else None

        raw_tags = raw.get("tags", [])
        if isinstance(raw_tags, str):
            tags = tuple(tag.strip() for tag in raw_tags.split(",") if tag.strip())
        elif isinstance(raw_tags, list) and all(isinstance(tag, str) for tag in raw_tags):
            tags = tuple(tag.strip() for tag in raw_tags if tag.strip())
        else:
            raise DamagedSocietyParseError("product tags are invalid")
        description = self._description(raw)
        evidence = " ".join((title, description, *tags))
        preorder = bool(re.search(r"\bpre[ -]?order(?:ed|ing)?\b", evidence, re.I))
        coming_soon = bool(re.search(r"\bcoming soon\b", evidence, re.I))
        available = any(variant["available"] for variant in variants)
        availability = (Availability.PREORDER if preorder else
                        Availability.COMING_SOON if coming_soon else
                        Availability.IN_STOCK if available else Availability.OUT_OF_STOCK)

        published = raw.get("published_at")
        try:
            published_at = (datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                            if published not in (None, "") else None)
        except ValueError as exc:
            raise DamagedSocietyParseError("publication timestamp is invalid") from exc
        if published_at is not None and published_at.tzinfo is None:
            raise DamagedSocietyParseError("publication timestamp needs a timezone")

        images = raw.get("images", [])
        if not isinstance(images, list):
            raise DamagedSocietyParseError("product images are invalid")
        image = images[0].get("src") if images and isinstance(images[0], dict) else (
            images[0] if images else raw.get("featured_image")
        )
        if isinstance(image, str) and image.startswith("//"):
            image = "https:" + image
        if image is not None and not isinstance(image, str):
            raise DamagedSocietyParseError("product image is invalid")

        vendor = raw.get("vendor")
        if vendor is not None and not isinstance(vendor, str):
            raise DamagedSocietyParseError("product vendor is invalid")
        return Product(
            retailer="Damaged Society UK", retailer_product_id=product_id, variant_id=variant_id,
            sku=str(selected["sku"]).strip() if selected.get("sku") not in (None, "") else None,
            barcode=(str(selected["barcode"]).strip()
                     if selected.get("barcode") not in (None, "") else None),
            name=title, url=urljoin(self.base_url + "/", f"products/{handle}"), image_url=image,
            price=price, original_price=original_price, currency="GBP", availability=availability,
            vendor=(vendor.strip() or None) if vendor is not None else None,
            product_type="Mini Backpack", tags=tags, listing_published_at=published_at,
            preorder=preorder,
            new_release=any(tag.casefold() in {"new", "new arrival", "new arrivals"} for tag in tags),
            release=parse_release_text(evidence, source="Damaged Society Shopify product data",
                                       local_timezone="Europe/London", date_order="DMY"),
        )

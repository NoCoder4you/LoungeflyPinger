"""CM POP UK adapter backed by the storefront's public Shopify product feeds."""

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

BASE_URL = "https://www.cmpop.co.uk"
COLLECTION = "loungefly-2"
PAGE_SIZE = 250
MAX_PAGES = 10


class CMPopParseError(ValueError):
    """CM POP returned incomplete or malformed Shopify commerce data."""


class CMPopMonitor(RetailerMonitor):
    """Monitor only Loungefly mini backpacks sold by CM POP UK."""

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
                if self._is_mini_backpack(raw):
                    product = self.parse_product(raw)
                    # Collections can overlap or repeat records; a Shopify product is one listing,
                    # irrespective of how many variants it currently exposes.
                    found[product.retailer_product_id] = product
            if len(products) < PAGE_SIZE:
                break
        else:
            raise CMPopParseError("CM POP collection exceeded the pagination safety limit")
        return list(found.values())

    async def check_product(self, product: Product) -> Product:
        handle = product.url.rstrip("/").rsplit("/", 1)[-1]
        try:
            if not handle:
                raise CMPopParseError("Product handle is missing")
            return self.parse_product(
                await self.http.get_json(f"{self.base_url}/products/{quote(handle)}.js")
            )
        except HttpClientError as exc:
            return replace(product, availability=(
                Availability.UNAVAILABLE if exc.status == 404 else Availability.ERROR
            ))
        except (CMPopParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            self._product_list(await self.http.get_json(
                f"{self.base_url}/collections/{COLLECTION}/products.json?limit=1&page=1"
            ))
            return True
        except (HttpClientError, CMPopParseError):
            return False

    @staticmethod
    def _product_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise CMPopParseError("CM POP response has no product list")
        products = payload["products"]
        if not all(isinstance(item, dict) for item in products):
            raise CMPopParseError("CM POP product list contains invalid entries")
        return products

    @staticmethod
    def _plain_description(raw: dict[str, Any]) -> str:
        value = raw.get("body_html", raw.get("description", "")) or ""
        if not isinstance(value, str):
            raise CMPopParseError("Product description is invalid")
        return " ".join(unescape(re.sub(r"<[^>]*>", " ", value)).split())

    @classmethod
    def _is_mini_backpack(cls, raw: dict[str, Any]) -> bool:
        title, vendor = raw.get("title"), raw.get("vendor")
        if not isinstance(title, str) or not isinstance(vendor, str):
            return False
        evidence = " ".join((title, str(raw.get("product_type", "")))).casefold().replace("-", " ")
        excluded = (
            "wallet", "crossbody", "cross body", "tote", "pin", "cardholder",
            "card holder", "keychain", "key chain", "charm", "coin purse",
        )
        return ("loungefly" in vendor.casefold() and "mini backpack" in evidence
                and not any(term in evidence for term in excluded))

    def parse_product(self, raw: object) -> Product:
        if not isinstance(raw, dict):
            raise CMPopParseError("Product is not an object")
        try:
            product_id = str(raw["id"]).strip()
            title = raw["title"].strip()
            handle = raw["handle"].strip()
            variants = raw["variants"]
        except (KeyError, AttributeError) as exc:
            raise CMPopParseError("Product identity is incomplete") from exc
        if not all((product_id, title, handle)) or not isinstance(variants, list) or not variants:
            raise CMPopParseError("Product identity or variants are incomplete")
        if not all(isinstance(v, dict) and isinstance(v.get("available"), bool) for v in variants):
            raise CMPopParseError("Variant availability is missing")
        selected = next((variant for variant in variants if variant["available"]), variants[0])
        variant_id = str(selected.get("id", "")).strip()
        if not variant_id:
            raise CMPopParseError("Variant identity is missing")
        try:
            price = self._money(selected["price"])
            compare = selected.get("compare_at_price")
            original_price = None if compare in (None, "") else self._money(compare)
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise CMPopParseError("Variant pricing is invalid") from exc

        tags_raw = raw.get("tags", [])
        if not isinstance(tags_raw, list) or not all(isinstance(tag, str) for tag in tags_raw):
            raise CMPopParseError("Product tags are invalid")
        tags = tuple(tag.strip() for tag in tags_raw if tag.strip())
        description = self._plain_description(raw)
        evidence = " ".join((title, description, *tags))
        preorder = bool(re.search(r"\bpre[ -]?order(?:ed|ing)?\b", evidence, re.I))
        coming_soon = bool(re.search(r"\bcoming\s+soon\b", evidence, re.I))
        availability = (Availability.PREORDER if preorder else
                        Availability.COMING_SOON if coming_soon else
                        Availability.IN_STOCK if any(v["available"] for v in variants)
                        else Availability.OUT_OF_STOCK)

        cm_pop_exclusive = bool(re.search(r"\bCM\s*POP\s+EXCLUSIVE\b", evidence, re.I))
        emea_exclusive = bool(re.search(r"\b(?:CM\s*POP\s+)?EMEA\s+EXCLUSIVE\b", evidence, re.I))
        explicit_exclusive = bool(re.search(r"\bexclusive\b", evidence, re.I))
        published = raw.get("published_at")
        try:
            listing_published_at = (datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                                    if published is not None else None)
        except ValueError as exc:
            raise CMPopParseError("Product publication timestamp is invalid") from exc
        if listing_published_at is not None and listing_published_at.tzinfo is None:
            raise CMPopParseError("Product publication timestamp needs a timezone")

        images = raw.get("images", [])
        if not isinstance(images, list):
            raise CMPopParseError("Product images are invalid")
        image = images[0].get("src") if images and isinstance(images[0], dict) else (
            images[0] if images else raw.get("featured_image")
        )
        if isinstance(image, str) and image.startswith("//"):
            image = "https:" + image
        if image is not None and not isinstance(image, str):
            raise CMPopParseError("Product image is invalid")
        vendor = raw.get("vendor")
        product_type = raw.get("product_type")
        if not isinstance(vendor, str) or not isinstance(product_type, str):
            raise CMPopParseError("Product vendor or type is invalid")

        return Product(
            retailer="CM POP UK", retailer_product_id=product_id, variant_id=variant_id,
            sku=str(selected["sku"]).strip() if selected.get("sku") not in (None, "") else None,
            name=title, url=urljoin(self.base_url + "/", f"products/{handle}"), image_url=image,
            price=price, original_price=original_price, currency="GBP", availability=availability,
            vendor=vendor, product_type="Mini Backpack", tags=tags, preorder=preorder,
            listing_published_at=listing_published_at, exclusive=explicit_exclusive,
            exclusive_retailer="CM POP UK" if cm_pop_exclusive or emea_exclusive else None,
            exclusive_region="EMEA" if emea_exclusive else None,
            # published_at records listing publication, never a product release date.
            release=parse_release_text(evidence, source="CM POP Shopify product data",
                                       local_timezone="Europe/London", date_order="DMY"),
        )

    @staticmethod
    def _money(value: object) -> Decimal:
        amount = (Decimal(value) / 100 if isinstance(value, int) and not isinstance(value, bool)
                  else Decimal(str(value)))
        if not amount.is_finite() or amount < 0:
            raise InvalidOperation
        return amount

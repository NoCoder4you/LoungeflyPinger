"""Forbidden Planet International UK Loungefly collection adapter."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any
from urllib.parse import quote, urljoin

from app.http import AsyncHttpClient, HttpClientError
from app.models import Availability, Product, ReleaseInfo, ReleasePrecision
from app.monitors.base import RetailerMonitor
from app.release import MONTHS, parse_release_text

BASE_URL = "https://shop.forbiddenplanet.co.uk"
COLLECTION = "loungefly"
PAGE_SIZE = 250
MAX_PAGES = 20


class ForbiddenPlanetParseError(ValueError):
    """The storefront returned incomplete or malformed product data."""


class ForbiddenPlanetMonitor(RetailerMonitor):
    """Monitor Loungefly mini backpacks on Forbidden Planet International."""

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
            listed = self._product_list(payload)
            candidates = [item for item in listed if self._is_mini_backpack(item)]
            # The collection's availability facet is only aggregate metadata.  Read each
            # canonical product endpoint so every variant determines the actual state.
            details = await asyncio.gather(*(
                self.http.get_json(f"{self.base_url}/products/{quote(str(item['handle']))}.js")
                for item in candidates
            ))
            for detail in details:
                product = self.parse_product(detail)
                found[product.retailer_product_id] = product
            if len(listed) < PAGE_SIZE:
                break
        else:
            raise ForbiddenPlanetParseError("Loungefly collection exceeded pagination limit")
        return list(found.values())

    async def check_product(self, product: Product) -> Product:
        handle = product.url.rstrip("/").rsplit("/", 1)[-1]
        try:
            return self.parse_product(await self.http.get_json(
                f"{self.base_url}/products/{quote(handle)}.js"
            ))
        except HttpClientError as exc:
            return replace(product, availability=(
                Availability.UNAVAILABLE if exc.status == 404 else Availability.ERROR
            ))
        except (ForbiddenPlanetParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            payload = await self.http.get_json(
                f"{self.base_url}/collections/{COLLECTION}/products.json?limit=1&page=1"
            )
            return bool(self._product_list(payload))
        except (HttpClientError, ForbiddenPlanetParseError):
            return False

    @staticmethod
    def _product_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise ForbiddenPlanetParseError("response has no product list")
        products = payload["products"]
        if not all(isinstance(item, dict) for item in products):
            raise ForbiddenPlanetParseError("product list contains invalid entries")
        return products

    @staticmethod
    def _tags(raw: dict[str, Any]) -> tuple[str, ...]:
        tags = raw.get("tags", [])
        if isinstance(tags, str):
            return tuple(tag.strip() for tag in tags.split(",") if tag.strip())
        if isinstance(tags, list) and all(isinstance(tag, str) for tag in tags):
            return tuple(tag.strip() for tag in tags if tag.strip())
        raise ForbiddenPlanetParseError("product tags are invalid")

    @classmethod
    def _is_mini_backpack(cls, raw: dict[str, Any]) -> bool:
        title = raw.get("title")
        if not isinstance(title, str):
            return False
        text = " ".join(title.casefold().replace("-", " ").split())
        excluded = ("keychain", "key chain", "charm", "wallet", "card holder", "crossbody", "tote")
        product_type = raw.get("product_type", raw.get("type"))
        tags = cls._tags(raw)
        is_loungefly = (
            isinstance(product_type, str) and product_type.casefold() == "loungefly"
        ) or "loungefly" in {tag.casefold() for tag in tags}
        return is_loungefly and "mini backpack" in text and not any(term in text for term in excluded)

    def parse_product(self, raw: object) -> Product:
        if not isinstance(raw, dict):
            raise ForbiddenPlanetParseError("product is not an object")
        try:
            product_id = str(raw["id"]).strip()
            title = raw["title"].strip()
            handle = raw["handle"].strip()
            variants = raw["variants"]
        except (KeyError, AttributeError) as exc:
            raise ForbiddenPlanetParseError("product identity is incomplete") from exc
        if not product_id or not title or not handle or not isinstance(variants, list) or not variants:
            raise ForbiddenPlanetParseError("product identity or variants are incomplete")
        if not all(isinstance(v, dict) and isinstance(v.get("available"), bool) for v in variants):
            raise ForbiddenPlanetParseError("variant availability is missing")
        selected = next((variant for variant in variants if variant["available"]), variants[0])
        variant_id = str(selected.get("id", "")).strip()
        if not variant_id:
            raise ForbiddenPlanetParseError("variant identity is missing")
        try:
            price = self._money(selected["price"])
            compare = selected.get("compare_at_price")
            original_price = None if compare in (None, "") else self._money(compare)
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise ForbiddenPlanetParseError("variant pricing is invalid") from exc

        tags = self._tags(raw)
        description = raw.get("description", raw.get("body_html", "")) or ""
        if not isinstance(description, str):
            raise ForbiddenPlanetParseError("product description is invalid")
        evidence = " ".join((title, description, *tags))
        preorder = bool(re.search(r"\bpre[ -]?order(?:ed|ing)?\b", evidence, re.I))
        availability = (Availability.PREORDER if preorder and any(v["available"] for v in variants)
                        else Availability.IN_STOCK if any(v["available"] for v in variants)
                        else Availability.OUT_OF_STOCK)
        release = parse_release_text(
            description, source="Forbidden Planet product page", local_timezone="Europe/London",
            date_order="DMY",
        ) or self._release_from_tags(tags)
        images = raw.get("images", [])
        if not isinstance(images, list):
            raise ForbiddenPlanetParseError("product images are invalid")
        image = images[0] if images else raw.get("featured_image")
        if isinstance(image, dict):
            image = image.get("src")
        if isinstance(image, str) and image.startswith("//"):
            image = "https:" + image
        if image is not None and not isinstance(image, str):
            raise ForbiddenPlanetParseError("product image is invalid")
        published = raw.get("published_at")
        try:
            published_at = datetime.fromisoformat(str(published).replace("Z", "+00:00")) if published else None
        except ValueError as exc:
            raise ForbiddenPlanetParseError("publication timestamp is invalid") from exc
        if published_at is not None and published_at.tzinfo is None:
            raise ForbiddenPlanetParseError("publication timestamp needs a timezone")
        new_tags = {"new", "new in", "new arrival", "new arrivals"}
        return Product(
            retailer="Forbidden Planet International UK", retailer_product_id=product_id,
            name=title, url=urljoin(self.base_url + "/", f"products/{handle}"), image_url=image,
            price=price, original_price=original_price, currency="GBP", availability=availability,
            product_type="Mini Backpack", variant_id=variant_id,
            sku=str(selected["sku"]).strip() if selected.get("sku") not in (None, "") else None,
            barcode=str(selected["barcode"]).strip() if selected.get("barcode") not in (None, "") else None,
            vendor=raw.get("vendor") if isinstance(raw.get("vendor"), str) else None,
            tags=tags, listing_published_at=published_at, preorder=preorder,
            new_release=bool(new_tags & {tag.casefold() for tag in tags}), release=release,
        )

    @staticmethod
    def _release_from_tags(tags: tuple[str, ...]) -> ReleaseInfo | None:
        short_months = {name[:3]: number for name, number in MONTHS.items()}
        for tag in tags:
            match = re.fullmatch(r"([a-z]{3})-(\d{2}|\d{4})", tag.strip(), re.I)
            if match and match.group(1).casefold() in short_months:
                year = int(match.group(2))
                if year < 100:
                    year += 2000
                return ReleaseInfo(
                    ReleasePrecision.MONTH_ONLY, release_month=short_months[match.group(1).casefold()],
                    release_year=year, text=tag,
                    source="Forbidden Planet collection release tag (month only)",
                )
        return None

    @staticmethod
    def _money(value: object) -> Decimal:
        amount = (Decimal(value) / 100 if isinstance(value, int) and not isinstance(value, bool)
                  else Decimal(str(value)))
        if not amount.is_finite() or amount < 0:
            raise InvalidOperation
        return amount

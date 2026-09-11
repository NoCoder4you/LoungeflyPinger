"""Something Different Gift Shop UK adapter for its public Shopify catalogue."""

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

BASE_URL = "https://www.somethingdifferentuk.co.uk"
BACKPACK_COLLECTION = "loungefly-backpacks"
PAGE_SIZE = 250
MAX_PAGES = 10


class SomethingDifferentParseError(ValueError):
    """The retailer returned incomplete or malformed commerce data."""


class SomethingDifferentMonitor(RetailerMonitor):
    """Monitor genuine Loungefly mini backpacks, including collection-wave metadata."""

    def __init__(self, http: AsyncHttpClient, *, base_url: str = BASE_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def discover_products(self) -> list[Product]:
        wave_by_id = await self._monthly_collection_memberships()
        found: dict[str, Product] = {}
        for page in range(1, MAX_PAGES + 1):
            payload = await self.http.get_json(
                f"{self.base_url}/collections/{BACKPACK_COLLECTION}/products.json"
                f"?limit={PAGE_SIZE}&page={page}"
            )
            raw_products = self._product_list(payload)
            candidates = [raw for raw in raw_products if self._is_mini_backpack(raw)]
            details = await asyncio.gather(*(
                self.http.get_text(f"{self.base_url}/products/{quote(str(raw['handle']))}")
                for raw in candidates
            ))
            for raw, html in zip(candidates, details, strict=True):
                product_id = str(raw.get("id", "")).strip()
                product = self.parse_product(raw, html=html, collection_wave=wave_by_id.get(product_id))
                found[product.retailer_product_id] = product
            if len(raw_products) < PAGE_SIZE:
                break
        else:
            raise SomethingDifferentParseError("backpack collection exceeded pagination limit")
        return list(found.values())

    async def check_product(self, product: Product) -> Product:
        handle = product.url.rstrip("/").rsplit("/", 1)[-1]
        try:
            raw, html = await asyncio.gather(
                self.http.get_json(f"{self.base_url}/products/{quote(handle)}.js"),
                self.http.get_text(f"{self.base_url}/products/{quote(handle)}"),
            )
            return self.parse_product(raw, html=html, collection_wave=self._wave_from_release(product.release))
        except HttpClientError as exc:
            return replace(product, availability=(
                Availability.UNAVAILABLE if exc.status == 404 else Availability.ERROR
            ))
        except (SomethingDifferentParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            return bool(self._product_list(await self.http.get_json(
                f"{self.base_url}/collections/{BACKPACK_COLLECTION}/products.json?limit=1&page=1"
            )))
        except (HttpClientError, SomethingDifferentParseError):
            return False

    async def _monthly_collection_memberships(self) -> dict[str, tuple[int, int, str]]:
        collections = self._collection_list(await self.http.get_json(
            f"{self.base_url}/collections.json?limit={PAGE_SIZE}"
        ))
        waves: dict[str, tuple[int, int, str]] = {}
        for collection in collections:
            title, handle = collection.get("title"), collection.get("handle")
            if not isinstance(title, str) or not isinstance(handle, str):
                continue
            match = re.fullmatch(
                rf"Loungefly\s+({'|'.join(MONTHS)})\s+(\d{{4}})", title.strip(), re.I
            )
            if not match:
                continue
            month, year = MONTHS[match.group(1).casefold()], int(match.group(2))
            for page in range(1, MAX_PAGES + 1):
                products = self._product_list(await self.http.get_json(
                    f"{self.base_url}/collections/{quote(handle)}/products.json"
                    f"?limit={PAGE_SIZE}&page={page}"
                ))
                for raw in products:
                    product_id = str(raw.get("id", "")).strip()
                    if product_id:
                        waves[product_id] = (month, year, title.strip())
                if len(products) < PAGE_SIZE:
                    break
            else:
                raise SomethingDifferentParseError("monthly collection exceeded pagination limit")
        return waves

    @staticmethod
    def _product_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise SomethingDifferentParseError("response has no product list")
        products = payload["products"]
        if not all(isinstance(item, dict) for item in products):
            raise SomethingDifferentParseError("product list contains invalid entries")
        return products

    @staticmethod
    def _collection_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("collections"), list):
            raise SomethingDifferentParseError("response has no collection list")
        collections = payload["collections"]
        if not all(isinstance(item, dict) for item in collections):
            raise SomethingDifferentParseError("collection list contains invalid entries")
        return collections

    @staticmethod
    def _tags(raw: dict[str, Any]) -> tuple[str, ...]:
        tags = raw.get("tags", [])
        if isinstance(tags, str):
            return tuple(tag.strip() for tag in tags.split(",") if tag.strip())
        if isinstance(tags, list) and all(isinstance(tag, str) for tag in tags):
            return tuple(tag.strip() for tag in tags if tag.strip())
        raise SomethingDifferentParseError("product tags are invalid")

    @classmethod
    def _is_mini_backpack(cls, raw: dict[str, Any]) -> bool:
        title = raw.get("title")
        if not isinstance(title, str):
            return False
        text = " ".join(title.casefold().replace("-", " ").split())
        excluded = (
            "mystery", "keychain", "key chain", "bag charm", "charm blind box", "wallet",
            "cardholder", "card holder", "crossbody", "cross body", "purse", "pin",
        )
        return "mini backpack" in text and not any(
            re.search(rf"\b{re.escape(term)}\b", text) for term in excluded
        ) and "loungefly" in {tag.casefold() for tag in cls._tags(raw)}

    def parse_product(
        self, raw: object, *, html: object, collection_wave: tuple[int, int, str] | None = None
    ) -> Product:
        if not isinstance(raw, dict):
            raise SomethingDifferentParseError("product is not an object")
        try:
            product_id = str(raw["id"]).strip()
            title, handle, variants = raw["title"].strip(), raw["handle"].strip(), raw["variants"]
        except (KeyError, AttributeError) as exc:
            raise SomethingDifferentParseError("product identity is incomplete") from exc
        if not product_id or not title or not handle or not isinstance(variants, list) or not variants:
            raise SomethingDifferentParseError("product identity or variants are incomplete")
        if not all(isinstance(v, dict) and isinstance(v.get("available"), bool) for v in variants):
            raise SomethingDifferentParseError("variant availability is missing")
        selected = next((v for v in variants if v["available"]), variants[0])
        variant_id = str(selected.get("id", "")).strip()
        if not variant_id:
            raise SomethingDifferentParseError("variant identity is missing")
        try:
            price = self._money(selected["price"])
            compare = selected.get("compare_at_price")
            original_price = None if compare in (None, "") else self._money(compare)
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise SomethingDifferentParseError("variant pricing is invalid") from exc

        tags = self._tags(raw)
        description = raw.get("body_html", raw.get("description", "")) or ""
        if not isinstance(description, str):
            raise SomethingDifferentParseError("product description is invalid")
        evidence = " ".join((title, description, *tags))
        preorder = bool(re.search(r"\bpre[ -]?order(?:ed|ing)?\b", evidence, re.I))
        available = any(v["available"] for v in variants)
        availability = self._availability(html, available=available, preorder=preorder)
        release = parse_release_text(
            description, source="Something Different Shopify product data",
            local_timezone="Europe/London", date_order="DMY",
        )
        if release is None and collection_wave:
            month, year, title_wave = collection_wave
            release = ReleaseInfo(
                ReleasePrecision.MONTH_ONLY, text=title_wave,
                source="Something Different monthly collection (discovery metadata; not an exact date)",
                release_month=month, release_year=year,
            )
        images = raw.get("images", [])
        if not isinstance(images, list):
            raise SomethingDifferentParseError("product images are invalid")
        image = images[0].get("src") if images and isinstance(images[0], dict) else (
            images[0] if images else raw.get("featured_image")
        )
        if isinstance(image, str) and image.startswith("//"):
            image = "https:" + image
        if image is not None and not isinstance(image, str):
            raise SomethingDifferentParseError("product image is invalid")
        published = raw.get("published_at")
        try:
            published_at = datetime.fromisoformat(str(published).replace("Z", "+00:00")) if published else None
        except ValueError as exc:
            raise SomethingDifferentParseError("publication timestamp is invalid") from exc
        if published_at is not None and published_at.tzinfo is None:
            raise SomethingDifferentParseError("publication timestamp needs a timezone")
        return Product(
            retailer="Something Different Gift Shop UK", retailer_product_id=product_id,
            name=title, url=urljoin(self.base_url + "/", f"products/{handle}"), image_url=image,
            price=price, original_price=original_price, currency="GBP", availability=availability,
            product_type="Mini Backpack", variant_id=variant_id,
            sku=str(selected["sku"]).strip() if selected.get("sku") not in (None, "") else None,
            barcode=str(selected["barcode"]).strip() if selected.get("barcode") not in (None, "") else None,
            vendor=raw.get("vendor") if isinstance(raw.get("vendor"), str) else None,
            tags=tags, listing_published_at=published_at, preorder=preorder,
            new_release=any(tag.casefold() == "new in" for tag in tags), release=release,
        )

    @staticmethod
    def _availability(html: object, *, available: bool, preorder: bool) -> Availability:
        if not isinstance(html, str) or not html.strip():
            raise SomethingDifferentParseError("product HTML is empty")
        if preorder and available:
            return Availability.PREORDER
        # The live theme publishes exact variant inventory in this structured app payload.
        quantities = re.findall(r"variantsInventoryQuantity\s*=\s*\{([^}]+)\}", html)
        if not quantities:
            raise SomethingDifferentParseError("inventory payload is missing")
        values = [int(value) for block in quantities for value in re.findall(r'parseInt\("(-?\d+)"\)', block)]
        if not values:
            raise SomethingDifferentParseError("inventory quantity is missing")
        if not available or max(values) <= 0:
            return Availability.OUT_OF_STOCK
        return Availability.LOW_STOCK if max(values) == 1 else Availability.IN_STOCK

    @staticmethod
    def _wave_from_release(release: ReleaseInfo | None) -> tuple[int, int, str] | None:
        if release and release.precision == ReleasePrecision.MONTH_ONLY:
            assert release.release_month and release.release_year
            return release.release_month, release.release_year, release.text or "Monthly collection"
        return None

    @staticmethod
    def _money(value: object) -> Decimal:
        amount = (Decimal(value) / 100 if isinstance(value, int) and not isinstance(value, bool)
                  else Decimal(str(value)))
        if not amount.is_finite() or amount < 0:
            raise InvalidOperation
        return amount

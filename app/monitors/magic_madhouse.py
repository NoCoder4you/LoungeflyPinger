"""Magic Madhouse UK adapter using its narrowly scoped BigCommerce search data."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin, urlparse

from app.http import AsyncHttpClient, HttpClientError
from app.models import Availability, Product
from app.monitors.base import RetailerMonitor
from app.release import parse_release_text

BASE_URL = "https://magicmadhouse.co.uk"
SEARCH_PATH = "/search.php?search_query=Mini+Backpack&_bc_fsnf=1&brand=43&section=product&page=1"
MAX_PAGES = 30


class MagicMadhouseParseError(ValueError):
    """The storefront returned incomplete or malformed structured product data."""


class MagicMadhouseMonitor(RetailerMonitor):
    """Monitor only Loungefly results from the retailer's Mini Backpack search."""

    def __init__(self, http: AsyncHttpClient, *, base_url: str = BASE_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def discover_products(self) -> list[Product]:
        found: dict[str, Product] = {}
        url: str | None = urljoin(self.base_url + "/", SEARCH_PATH.lstrip("/"))
        visited: set[str] = set()
        for _ in range(MAX_PAGES):
            if not url or url in visited:
                return list(found.values())
            visited.add(url)
            products, next_url = self.parse_listing(await self.http.get_text(url))
            for raw in products:
                if self._is_loungefly_mini_backpack(raw):
                    product = self.parse_product(raw)
                    found[product.retailer_product_id] = product
            url = self._safe_url(next_url) if next_url else None
        raise MagicMadhouseParseError("search pagination exceeded its safety limit")

    async def check_product(self, product: Product) -> Product:
        # The same structured search records expose stock counts, preorder state, and
        # custom arrival fields; avoid interpreting generic delivery UI on detail pages.
        try:
            current = next((item for item in await self.discover_products()
                            if item.retailer_product_id == product.retailer_product_id), None)
            return current or replace(product, availability=Availability.UNAVAILABLE)
        except (HttpClientError, MagicMadhouseParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            products, _ = self.parse_listing(await self.http.get_text(
                urljoin(self.base_url + "/", SEARCH_PATH.lstrip("/"))))
            return bool(products)
        except (HttpClientError, MagicMadhouseParseError):
            return False

    @staticmethod
    def parse_listing(html: object) -> tuple[list[dict[str, Any]], str | None]:
        if not isinstance(html, str) or not html.strip():
            raise MagicMadhouseParseError("response was empty")
        match = re.search(r'var\s+BODL\s*=\s*JSON\.parse\("(.*?)"\);', html, re.S)
        try:
            payload = json.loads(json.loads(f'"{match.group(1)}"')) if match else None
            search = payload["search"]
            products = search["products"]
            pagination = search["pagination"]
        except (AttributeError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise MagicMadhouseParseError("search data is missing or malformed") from exc
        if not isinstance(products, list) or not products or not all(isinstance(x, dict) for x in products):
            raise MagicMadhouseParseError("search data has no valid products")
        if not isinstance(pagination, dict) or pagination.get("next") is not None and not isinstance(pagination["next"], str):
            raise MagicMadhouseParseError("pagination data is malformed")
        return products, pagination.get("next")

    @staticmethod
    def _is_loungefly_mini_backpack(raw: dict[str, Any]) -> bool:
        name = raw.get("name")
        brand = raw.get("brand")
        categories = raw.get("category")
        if not isinstance(name, str) or not isinstance(brand, dict) or not isinstance(categories, list):
            return False
        normalized = " ".join(name.casefold().replace("-", " ").split())
        excluded = re.search(r"\b(?:keychain|key chain|charm|wallet|purse|crossbody|tote|pin)\b", normalized)
        backpack_category = any(isinstance(value, str) and
                                "loungefly/backpack" in value.casefold().replace("/apparel", "")
                                for value in categories)
        return (brand.get("name") == "Loungefly" and backpack_category and
                "mini backpack" in normalized and excluded is None)

    def _safe_url(self, value: str) -> str:
        url = urljoin(self.base_url + "/", value)
        if urlparse(url).netloc.casefold() != urlparse(self.base_url).netloc.casefold():
            raise MagicMadhouseParseError("URL is outside Magic Madhouse")
        return url

    @staticmethod
    def _arrival(fields: dict[str, str]) -> date | None:
        value = fields.get("estimated arrival") or fields.get("eta")
        if not value:
            return None
        match = re.search(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", value)
        if not match:
            return None
        try:
            return date(*(int(part) for part in match.groups()))
        except ValueError:
            return None

    def parse_product(self, raw: object) -> Product:
        if not isinstance(raw, dict):
            raise MagicMadhouseParseError("product is not an object")
        try:
            product_id = str(raw["id"]).strip()
            sku = str(raw["sku"]).strip()
            name = raw["name"].strip()
            price_data = raw["price"]["with_tax"]
            price = Decimal(str(price_data["value"]))
            currency = price_data["currency"].upper()
            custom = raw.get("custom_fields", [])
        except (KeyError, AttributeError, InvalidOperation, TypeError) as exc:
            raise MagicMadhouseParseError("product identity or price is invalid") from exc
        if not product_id or not sku or not name or currency != "GBP" or not price.is_finite() or price < 0:
            raise MagicMadhouseParseError("product identity or GBP price is unsupported")
        if not isinstance(custom, list) or not all(isinstance(item, dict) for item in custom):
            raise MagicMadhouseParseError("custom fields are malformed")
        fields = {str(item.get("name", "")).strip().casefold(): str(item.get("value", "")).strip()
                  for item in custom}
        preorder = raw.get("pre_order")
        if not isinstance(preorder, bool):
            raise MagicMadhouseParseError("preorder state is missing")
        stock = raw.get("stock_level")
        if stock is not None and (not isinstance(stock, int) or isinstance(stock, bool) or stock < 0):
            raise MagicMadhouseParseError("stock level is invalid")
        purchasable = raw.get("show_cart_action")
        if not isinstance(purchasable, bool):
            raise MagicMadhouseParseError("purchase state is missing")
        availability = (Availability.PREORDER if preorder and purchasable else
                        Availability.IN_STOCK if purchasable and stock != 0 else
                        Availability.OUT_OF_STOCK)
        price_block = raw["price"]
        rrp = price_block.get("rrp_with_tax")
        try:
            original = Decimal(str(rrp["value"])) if isinstance(rrp, dict) else None
        except (InvalidOperation, TypeError, KeyError) as exc:
            raise MagicMadhouseParseError("RRP is malformed") from exc
        image = raw.get("image", {}).get("data") if isinstance(raw.get("image"), dict) else None
        if image is not None and not isinstance(image, str):
            raise MagicMadhouseParseError("image is malformed")
        release_evidence = " ".join((fields.get("release date", ""), fields.get("expected release", "")))
        # BigCommerce's generic `release_date` is a preorder-control sentinel on this
        # store (currently 2038), not retailer-published release evidence.
        release = parse_release_text(release_evidence, source="Magic Madhouse custom field",
                                     local_timezone="Europe/London", date_order="DMY")
        return Product(
            retailer="Magic Madhouse UK", retailer_product_id=product_id, name=name,
            url=self._safe_url(str(raw.get("url", ""))), image_url=image, price=price,
            original_price=original if original is not None and original > price else None,
            currency="GBP", availability=availability, preorder=preorder,
            product_type="Mini Backpack", sku=sku, vendor="Loungefly",
            estimated_arrival_date=self._arrival(fields), release=release,
        )

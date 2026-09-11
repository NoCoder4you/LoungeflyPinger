"""Get Ready Comics UK adapter using the public WooCommerce Store API."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, InvalidOperation
from html import unescape
import re
from typing import Any
from urllib.parse import urlencode, urlparse

from app.http import AsyncHttpClient, HttpClientError
from app.models import Availability, Product
from app.monitors.base import RetailerMonitor
from app.release import parse_release_text

BASE_URL = "https://getreadycomics.com"
BACKPACK_CATEGORY_ID = 1178
PAGE_SIZE = 100
MAX_PAGES = 20


class GetReadyComicsParseError(ValueError):
    """The public WooCommerce representation was incomplete or malformed."""


class GetReadyComicsMonitor(RetailerMonitor):
    """Discover Loungefly mini backpacks from the retailer's backpack category."""

    def __init__(self, http: AsyncHttpClient, *, base_url: str = BASE_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def discover_products(self) -> list[Product]:
        found: dict[str, Product] = {}
        for page in range(1, MAX_PAGES + 1):
            payload = await self.http.get_json(self._collection_url(page))
            products = self._product_list(payload)
            for raw in products:
                if self._is_loungefly_mini_backpack(raw):
                    product = self.parse_product(raw)
                    found[product.retailer_product_id] = product
            if len(products) < PAGE_SIZE:
                break
        else:
            raise GetReadyComicsParseError("Get Ready Comics exceeded the pagination limit")
        return list(found.values())

    async def check_product(self, product: Product) -> Product:
        try:
            raw = await self.http.get_json(
                f"{self.base_url}/wp-json/wc/store/v1/products/{product.retailer_product_id}"
            )
            return self.parse_product(raw)
        except HttpClientError as exc:
            return replace(product, availability=(
                Availability.UNAVAILABLE if exc.status == 404 else Availability.ERROR
            ))
        except (GetReadyComicsParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            return bool(self._product_list(await self.http.get_json(self._collection_url(1, 1))))
        except (HttpClientError, GetReadyComicsParseError):
            return False

    def _collection_url(self, page: int, page_size: int = PAGE_SIZE) -> str:
        query = urlencode({"category": BACKPACK_CATEGORY_ID, "per_page": page_size, "page": page})
        return f"{self.base_url}/wp-json/wc/store/v1/products?{query}"

    @staticmethod
    def _product_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise GetReadyComicsParseError("Get Ready Comics response is not a product list")
        return payload

    @staticmethod
    def _category_slugs(raw: dict[str, Any]) -> set[str]:
        categories = raw.get("categories")
        if not isinstance(categories, list) or not all(isinstance(item, dict) for item in categories):
            raise GetReadyComicsParseError("Product categories are invalid")
        slugs = {item.get("slug") for item in categories}
        if not all(isinstance(slug, str) and slug for slug in slugs):
            raise GetReadyComicsParseError("Product category slugs are invalid")
        return slugs

    @classmethod
    def _is_loungefly_mini_backpack(cls, raw: dict[str, Any]) -> bool:
        try:
            slugs = cls._category_slugs(raw)
        except GetReadyComicsParseError:
            return False
        name = raw.get("name")
        if not isinstance(name, str):
            return False
        evidence = unescape(name).casefold().replace("-", " ")
        excluded = ("mystery mini backpack", "insert organiser", "bag charm", "keychain")
        return (
            "backpacks" in slugs
            and bool({"loungefly", "loungefly-shop-by-brand"} & slugs)
            and "mini backpack" in evidence
            and not any(term in evidence for term in excluded)
        )

    def parse_product(self, raw: object) -> Product:
        if not isinstance(raw, dict):
            raise GetReadyComicsParseError("Product is not an object")
        try:
            product_id = str(raw["id"]).strip()
            name = unescape(raw["name"]).strip()
            sku = unescape(raw["sku"]).strip()
            url = raw["permalink"].strip()
            in_stock = raw["is_in_stock"]
            prices = raw["prices"]
        except (KeyError, AttributeError, TypeError) as exc:
            raise GetReadyComicsParseError("Product identity or commerce state is incomplete") from exc
        if not product_id or not name or not isinstance(in_stock, bool) or not isinstance(prices, dict):
            raise GetReadyComicsParseError("Product identity or commerce state is invalid")
        if urlparse(url).netloc != urlparse(self.base_url).netloc or not urlparse(url).path.startswith("/shop/"):
            raise GetReadyComicsParseError("Product URL is outside Get Ready Comics")
        try:
            minor_unit = prices["currency_minor_unit"]
            if not isinstance(minor_unit, int) or isinstance(minor_unit, bool) or not 0 <= minor_unit <= 6:
                raise InvalidOperation
            price = self._money(prices["price"], minor_unit)
            regular_price = self._money(prices["regular_price"], minor_unit)
            currency = prices["currency_code"].strip().upper()
        except (KeyError, AttributeError, InvalidOperation, TypeError) as exc:
            raise GetReadyComicsParseError("Product pricing is invalid") from exc
        if currency != "GBP":
            raise GetReadyComicsParseError("Product currency is not GBP")

        slugs = self._category_slugs(raw)
        description = self._plain_text(raw.get("short_description", ""))
        full_description = self._plain_text(raw.get("description", ""))
        evidence = " ".join((name, description, full_description))
        preorder = "pre-order" in slugs or bool(re.search(r"\bpre[ -]?order\b", evidence, re.I))
        coming_soon = "coming-soon" in slugs
        availability = (
            Availability.PREORDER if preorder and in_stock else
            Availability.IN_STOCK if in_stock else
            Availability.OUT_OF_STOCK if preorder else
            Availability.COMING_SOON if coming_soon else Availability.OUT_OF_STOCK
        )
        images = raw.get("images")
        if not isinstance(images, list):
            raise GetReadyComicsParseError("Product images are invalid")
        image_url = images[0].get("src") if images and isinstance(images[0], dict) else None
        if image_url is not None and (not isinstance(image_url, str) or not image_url.startswith("https://")):
            raise GetReadyComicsParseError("Product image is invalid")
        exclusive = bool(re.search(r"\bGet Ready Comics exclusive\b", evidence, re.I))
        release_text = " ".join((evidence, "Coming Soon" if coming_soon else ""))
        return Product(
            retailer="Get Ready Comics UK", retailer_product_id=product_id, name=name, url=url,
            image_url=image_url, price=price, original_price=(regular_price if regular_price > price else None),
            currency="GBP", availability=availability, product_type="Mini Backpack",
            preorder=preorder, exclusive=exclusive,
            exclusive_retailer="Get Ready Comics UK" if exclusive else None,
            sku=sku or None,
            release=parse_release_text(release_text, source="Get Ready Comics WooCommerce Store API",
                                       local_timezone="Europe/London", date_order="DMY"),
        )

    @staticmethod
    def _plain_text(value: object) -> str:
        if not isinstance(value, str):
            raise GetReadyComicsParseError("Product description is invalid")
        return " ".join(unescape(re.sub(r"<[^>]*>", " ", value)).split())

    @staticmethod
    def _money(value: object, minor_unit: int) -> Decimal:
        amount = Decimal(str(value)) / (Decimal(10) ** minor_unit)
        if not amount.is_finite() or amount < 0:
            raise InvalidOperation
        return amount

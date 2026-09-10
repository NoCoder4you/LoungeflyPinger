"""TruffleShuffle UK adapter using the site's public JSON-LD product data."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from app.http import AsyncHttpClient, HttpClientError
from app.models import Availability, Product
from app.monitors.base import RetailerMonitor

BASE_URL = "https://www.truffleshuffle.co.uk"
COLLECTION_PATH = "/loungefly"
MAX_PAGES = 20


class TruffleShuffleParseError(ValueError):
    """The public product representation was incomplete or malformed."""


class _StructuredProductParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.products: list[tuple[object, bool]] = []
        self.next_url: str | None = None
        self._in_json_ld = False
        self._json_parts: list[str] = []
        self._in_card = False
        self._card_exclusive = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "a" and "productListingBox" in classes:
            self._in_card = True
            self._card_exclusive = False
        elif self._in_card:
            if "hashExclusive" in classes:
                self._card_exclusive = True
        if tag == "script" and (values.get("type") or "").lower() == "application/ld+json":
            self._in_json_ld = True
            self._json_parts = []
        if tag == "a" and "pageNavnext" in classes and values.get("href"):
            self.next_url = values["href"]

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_json_ld:
            self._in_json_ld = False
            try:
                value = json.loads("".join(self._json_parts))
            except (json.JSONDecodeError, TypeError):
                value = None
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, dict) and item.get("@type") == "Product":
                    self.products.append((item, self._card_exclusive))
        if tag == "a" and self._in_card:
            self._in_card = False

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._json_parts.append(data)


class TruffleShuffleMonitor(RetailerMonitor):
    """Monitor Loungefly mini backpacks without depending on presentation markup."""

    def __init__(self, http: AsyncHttpClient, *, base_url: str = BASE_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def discover_products(self) -> list[Product]:
        found: dict[str, Product] = {}
        url: str | None = urljoin(self.base_url + "/", COLLECTION_PATH.lstrip("/"))
        visited: set[str] = set()
        for _ in range(MAX_PAGES):
            if url is None or url in visited:
                break
            visited.add(url)
            products, next_url = self.parse_listing(await self.http.get_text(url))
            for raw, exclusive in products:
                if self._is_mini_backpack(raw):
                    product = self.parse_product(raw, source_url=url, exclusive=exclusive)
                    previous = found.get(product.retailer_product_id)
                    if previous is not None and previous.exclusive and not product.exclusive:
                        product = replace(product, exclusive=True)
                    found[product.retailer_product_id] = product
            url = urljoin(self.base_url + "/", next_url) if next_url else None
        else:
            if url:
                raise TruffleShuffleParseError("TruffleShuffle exceeded the pagination safety limit")
        return list(found.values())

    async def check_product(self, product: Product) -> Product:
        try:
            products, _ = self.parse_listing(await self.http.get_text(product.url))
            matches = [item for item in products if str(item[0].get("sku", "")) == product.retailer_product_id]
            if len(matches) != 1:
                raise TruffleShuffleParseError("Product JSON-LD was missing or ambiguous")
            raw, exclusive = matches[0]
            return self.parse_product(raw, source_url=product.url, exclusive=exclusive or product.exclusive)
        except (HttpClientError, TruffleShuffleParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            products, _ = self.parse_listing(await self.http.get_text(urljoin(self.base_url, COLLECTION_PATH)))
            return bool(products)
        except (HttpClientError, TruffleShuffleParseError):
            return False

    @staticmethod
    def parse_listing(html: object) -> tuple[list[tuple[dict[str, Any], bool]], str | None]:
        if not isinstance(html, str) or not html.strip():
            raise TruffleShuffleParseError("TruffleShuffle response was empty")
        parser = _StructuredProductParser()
        try:
            parser.feed(html)
            parser.close()
        except (ValueError, TypeError) as exc:
            raise TruffleShuffleParseError("TruffleShuffle HTML could not be parsed") from exc
        if not parser.products:
            raise TruffleShuffleParseError("TruffleShuffle response has no Product JSON-LD")
        return parser.products, parser.next_url

    @staticmethod
    def _is_mini_backpack(raw: dict[str, Any]) -> bool:
        name = raw.get("name")
        if not isinstance(name, str):
            return False
        normalized = " ".join(name.lower().replace("-", " ").split())
        excluded = (
            "wallet", "purse", "cardholder", "card holder", "crossbody", "pin", "keychain",
            "key chain", "charm", "t shirt", "tee", "hoodie", "sweatshirt", "dress",
        )
        return "loungefly" in normalized and "mini backpack" in normalized and not any(
            term in normalized for term in excluded
        )

    def parse_product(self, raw: object, *, source_url: str, exclusive: bool = False) -> Product:
        if not isinstance(raw, dict):
            raise TruffleShuffleParseError("Product JSON-LD is not an object")
        offers = raw.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if len(offers) == 1 else None
        if not isinstance(offers, dict):
            raise TruffleShuffleParseError("Product offer is missing")
        try:
            product_id = str(raw["sku"]).strip()
            name = raw["name"].strip()
            price = Decimal(str(offers["price"]))
            currency = offers["priceCurrency"].strip().upper()
            availability_value = offers["availability"].rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        except (KeyError, AttributeError, InvalidOperation, TypeError) as exc:
            raise TruffleShuffleParseError("Product identity, price, or availability is invalid") from exc
        availability_map = {
            "InStock": Availability.IN_STOCK,
            "OutOfStock": Availability.OUT_OF_STOCK,
            "SoldOut": Availability.OUT_OF_STOCK,
            "PreOrder": Availability.PREORDER,
            "PreSale": Availability.PREORDER,
            "BackOrder": Availability.BACKORDER,
            "Discontinued": Availability.UNAVAILABLE,
        }
        if not product_id or not name or availability_value not in availability_map:
            raise TruffleShuffleParseError("Product identity or availability is unsupported")
        if currency != "GBP":
            raise TruffleShuffleParseError("Product currency is not GBP")
        product_url = offers.get("url") or source_url
        if not isinstance(product_url, str):
            raise TruffleShuffleParseError("Product URL is invalid")
        product_url = urljoin(self.base_url + "/", product_url)
        if urlparse(product_url).netloc != urlparse(self.base_url).netloc:
            raise TruffleShuffleParseError("Product URL is outside TruffleShuffle")
        path_parts = urlparse(product_url).path.strip("/").split("/")
        if len(path_parts) < 3 or path_parts[0] != "product" or path_parts[1] != product_id:
            raise TruffleShuffleParseError("Product URL does not match its product ID")
        image = raw.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get("url")
        if not isinstance(image, str) or not image:
            raise TruffleShuffleParseError("Product image is missing")
        preorder = availability_map[availability_value] == Availability.PREORDER
        return Product(
            retailer="TruffleShuffle",
            retailer_product_id=product_id,
            name=name,
            url=product_url,
            image_url=urljoin(self.base_url + "/", image),
            price=price,
            currency="GBP",
            availability=availability_map[availability_value],
            product_type="Mini Backpack",
            franchise=None,
            character=None,
            exclusive=exclusive,
            preorder=preorder,
            sku=product_id,
        )

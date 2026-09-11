"""Merchoid UK adapter for its dedicated Loungefly mini-backpack catalogue."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from app.http import AsyncHttpClient, HttpClientError
from app.models import Availability, Product
from app.monitors.base import RetailerMonitor
from app.release import parse_release_text

BASE_URL = "https://www.merchoid.com"
COLLECTION_PATH = "/uk/loungefly/mini-backpacks/"
MAX_PAGES = 30


class MerchoidUKParseError(ValueError):
    """Merchoid returned incomplete or unrecognizable catalogue data."""


@dataclass
class _Card:
    product_id: str | None = None
    name: str | None = None
    url: str | None = None
    image: str | None = None
    final_price: str | None = None
    regular_price: str | None = None


class _CatalogueParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[_Card] = []
        self.next_url: str | None = None
        self._card: _Card | None = None
        self._name_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "li" and "product-item" in classes:
            self._card = _Card()
        if self._card is not None:
            if values.get("data-product-id") and not self._card.product_id:
                self._card.product_id = values["data-product-id"]
            if tag == "a" and "product-item-link" in classes:
                self._card.url = values.get("href")
                self._card.name = values.get("title")
                self._name_depth = 1
            elif self._name_depth:
                self._name_depth += 1
            if tag == "img" and "product-image-photo" in classes:
                self._card.image = values.get("src") or values.get("data-original")
            amount = values.get("data-price-amount")
            price_type = values.get("data-price-type")
            if amount and price_type == "finalPrice":
                self._card.final_price = amount
            elif amount and price_type in {"oldPrice", "regularPrice"}:
                self._card.regular_price = amount
        if tag == "a" and "next" in classes and values.get("href"):
            self.next_url = values["href"]

    def handle_endtag(self, tag: str) -> None:
        if self._name_depth:
            self._name_depth -= 1
        if tag == "li" and self._card is not None:
            self.cards.append(self._card)
            self._card = None

    def handle_data(self, data: str) -> None:
        if self._card is not None and self._name_depth and not self._card.name:
            value = " ".join(data.split())
            if value:
                self._card.name = value


class _DetailParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.product_id: str | None = None
        self.products: list[dict[str, Any]] = []
        self.final_price: str | None = None
        self.regular_price: str | None = None
        self._json = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("data-product-id") and not self.product_id:
            self.product_id = values["data-product-id"]
        amount = values.get("data-price-amount")
        if amount and values.get("data-price-type") == "finalPrice" and self.final_price is None:
            self.final_price = amount
        elif amount and values.get("data-price-type") in {"oldPrice", "regularPrice"}:
            self.regular_price = amount
        if tag == "script" and (values.get("type") or "").casefold() == "application/ld+json":
            self._json, self._parts = True, []

    def handle_data(self, data: str) -> None:
        if self._json:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "script" or not self._json:
            return
        self._json = False
        try:
            value = json.loads("".join(self._parts))
        except (json.JSONDecodeError, TypeError):
            return
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict) and item.get("@type") == "Product":
                self.products.append(item)


class MerchoidUKMonitor(RetailerMonitor):
    """Scan lightweight category pages; product pages are queried only on demand."""

    def __init__(self, http: AsyncHttpClient, *, base_url: str = BASE_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def discover_products(self) -> list[Product]:
        found: dict[str, Product] = {}
        url: str | None = urljoin(self.base_url + "/", COLLECTION_PATH.lstrip("/"))
        visited: set[str] = set()
        for _ in range(MAX_PAGES):
            if url is None or url in visited:
                return list(found.values())
            visited.add(url)
            cards, next_url = self.parse_listing(await self.http.get_text(url))
            for card in cards:
                if self._is_mini_backpack(card.name or ""):
                    product = self.parse_card(card)
                    found[product.retailer_product_id] = product
            url = urljoin(self.base_url + "/", next_url) if next_url else None
            if url is None:
                return list(found.values())
        raise MerchoidUKParseError("Merchoid UK exceeded the pagination safety limit")

    async def check_product(self, product: Product) -> Product:
        try:
            return self.parse_detail(await self.http.get_text(product.url), source_url=product.url)
        except (HttpClientError, MerchoidUKParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            cards, _ = self.parse_listing(await self.http.get_text(urljoin(self.base_url, COLLECTION_PATH)))
            return bool(cards)
        except (HttpClientError, MerchoidUKParseError):
            return False

    @staticmethod
    def parse_listing(html: object) -> tuple[list[_Card], str | None]:
        if not isinstance(html, str) or not html.strip():
            raise MerchoidUKParseError("Merchoid UK response was empty")
        parser = _CatalogueParser()
        try:
            parser.feed(html)
            parser.close()
        except (TypeError, ValueError) as exc:
            raise MerchoidUKParseError("Merchoid UK catalogue could not be parsed") from exc
        if not parser.cards:
            raise MerchoidUKParseError("Merchoid UK response has no product cards")
        return parser.cards, parser.next_url

    @staticmethod
    def _is_mini_backpack(name: str) -> bool:
        normalized = " ".join(name.casefold().replace("-", " ").split())
        excluded = ("full size", "fullsize", "convertible", "charm", "keychain", "key chain")
        return "mini backpack" in normalized and not any(
            re.search(rf"\b{re.escape(term)}\b", normalized) for term in excluded
        )

    def _safe_url(self, value: str) -> str:
        url = urljoin(self.base_url + "/", value)
        if urlparse(url).netloc != urlparse(self.base_url).netloc:
            raise MerchoidUKParseError("Product URL is outside Merchoid")
        return url

    def parse_card(self, card: _Card) -> Product:
        if not all((card.product_id, card.name, card.url, card.image, card.final_price)):
            raise MerchoidUKParseError("Merchoid UK product card is incomplete")
        try:
            price = Decimal(card.final_price)
            regular = Decimal(card.regular_price) if card.regular_price else None
        except InvalidOperation as exc:
            raise MerchoidUKParseError("Merchoid UK card pricing is invalid") from exc
        preorder = bool(re.search(r"\bpre[ -]?order\b", card.name, re.I))
        return Product(
            retailer="Merchoid UK", retailer_product_id=card.product_id, name=card.name,
            url=self._safe_url(card.url), image_url=self._safe_url(card.image),
            price=price, original_price=regular if regular and regular > price else None,
            currency="GBP", availability=Availability.PREORDER if preorder else Availability.IN_STOCK,
            preorder=preorder, product_type="Mini Backpack",
        )

    def parse_detail(self, html: object, *, source_url: str) -> Product:
        if not isinstance(html, str) or not html.strip():
            raise MerchoidUKParseError("Merchoid UK product response was empty")
        parser = _DetailParser()
        parser.feed(html)
        if len(parser.products) != 1 or not parser.product_id:
            raise MerchoidUKParseError("Merchoid UK product data was missing or ambiguous")
        raw = parser.products[0]
        offers = raw.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if len(offers) == 1 else None
        availability_offer = offers.get("offers") if isinstance(offers, dict) else None
        if not isinstance(availability_offer, dict):
            availability_offer = offers
        try:
            name = unescape(str(raw["name"])).strip()
            sku = str(raw["sku"]).strip()
            image = raw["image"]
            offered_price = offers.get("price", offers.get("lowPrice"))
            price = Decimal(parser.final_price or str(offered_price))
            regular_price = Decimal(parser.regular_price) if parser.regular_price else None
            currency = str(offers["priceCurrency"]).upper()
            state = str(availability_offer["availability"]).rsplit("/", 1)[-1]
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise MerchoidUKParseError("Merchoid UK product data is incomplete") from exc
        if isinstance(image, list):
            image = image[0] if image else None
        states = {"InStock": Availability.IN_STOCK, "OutOfStock": Availability.OUT_OF_STOCK,
                  "SoldOut": Availability.OUT_OF_STOCK, "PreOrder": Availability.PREORDER}
        if not name or not sku or not isinstance(image, str) or currency != "GBP" or state not in states:
            raise MerchoidUKParseError("Merchoid UK product data is unsupported")
        preorder = states[state] == Availability.PREORDER or bool(
            re.search(r"\bpre[ -]?order\b", name, re.I)
        )
        description = unescape(raw.get("description", ""))
        return Product(
            retailer="Merchoid UK", retailer_product_id=parser.product_id, name=name,
            url=self._safe_url(str(offers.get("url") or source_url)), image_url=self._safe_url(image),
            price=price, original_price=(regular_price if regular_price and regular_price > price else None),
            currency="GBP", availability=Availability.PREORDER if preorder else states[state],
            preorder=preorder, product_type="Mini Backpack", sku=sku,
            release=parse_release_text(description, source="Merchoid UK product data",
                                       local_timezone="Europe/London"),
        )

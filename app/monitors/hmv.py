"""HMV adapter for ordinary Playwright-rendered schema.org product data."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from app.browser import BrowserPage, BrowserService, PageStatus
from app.models import Availability, Product
from app.monitors.base import RetailerMonitor

BASE_URL = "https://hmv.com"
SEARCH_PATH = "/search?searchtext=loungefly%20mini%20backpack"


class HMVMonitorError(RuntimeError):
    def __init__(self, status: PageStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


class HMVParseError(ValueError):
    pass


class _JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[object] = []
        self._parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and (values.get("type") or "").casefold() == "application/ld+json":
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._parts is not None:
            try:
                self.values.append(json.loads("".join(self._parts)))
            except (json.JSONDecodeError, TypeError):
                pass
            self._parts = None


class HMVMonitor(RetailerMonitor):
    """Use only normal browser output; challenge pages are hard failures."""

    def __init__(self, browser: BrowserService, *, base_url: str = BASE_URL) -> None:
        self.browser = browser
        self.base_url = base_url.rstrip("/")
        self.last_status: PageStatus | None = None

    async def _load(self, url: str) -> BrowserPage:
        page = await self.browser.fetch(url)
        self.last_status = page.status
        if page.status != PageStatus.PAGE_LOADED:
            raise HMVMonitorError(page.status, page.detail or f"HMV returned {page.status}")
        return page

    async def discover_products(self) -> list[Product]:
        page = await self._load(urljoin(self.base_url, SEARCH_PATH))
        try:
            products = [
                self.parse_product(raw, source_url=page.url)
                for raw in self.parse_products(page.html)
                if self.is_mini_backpack(raw)
            ]
        except (HMVParseError, ValueError, TypeError) as exc:
            self.last_status = PageStatus.PARSER_ERROR
            raise HMVMonitorError(PageStatus.PARSER_ERROR, str(exc)) from exc
        return list({item.retailer_product_id: item for item in products}.values())

    async def check_product(self, product: Product) -> Product:
        try:
            page = await self._load(product.url)
            matches = [
                raw for raw in self.parse_products(page.html)
                if str(raw.get("sku", raw.get("productID", ""))).strip() == product.retailer_product_id
            ]
            if len(matches) != 1:
                raise HMVParseError("HMV product data was missing or ambiguous")
            return self.parse_product(matches[0], source_url=page.url)
        except (HMVMonitorError, HMVParseError, ValueError, TypeError):
            if self.last_status == PageStatus.PAGE_LOADED:
                self.last_status = PageStatus.PARSER_ERROR
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            return bool(await self.discover_products())
        except HMVMonitorError:
            return False

    @staticmethod
    def parse_products(html: object) -> list[dict[str, Any]]:
        if not isinstance(html, str) or not html.strip():
            raise HMVParseError("HMV response was empty")
        parser = _JsonLdParser()
        try:
            parser.feed(html)
            parser.close()
        except (TypeError, ValueError) as exc:
            raise HMVParseError("HMV rendered HTML could not be parsed") from exc
        products: list[dict[str, Any]] = []

        def visit(value: object) -> None:
            if isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, dict):
                if value.get("@type") == "Product":
                    products.append(value)
                for key in ("@graph", "itemListElement"):
                    visit(value.get(key))
                item = value.get("item")
                if isinstance(item, dict):
                    visit(item)

        visit(parser.values)
        if not products:
            raise HMVParseError("HMV response has no Product JSON-LD")
        return products

    @staticmethod
    def is_mini_backpack(raw: dict[str, Any]) -> bool:
        name = raw.get("name")
        if not isinstance(name, str):
            return False
        normalized = " ".join(name.casefold().replace("-", " ").split())
        excluded = (
            "wallet", "purse", "cardholder", "card holder", "crossbody", "pin", "keychain",
            "key chain", "tote", "shoulder bag",
        )
        return "loungefly" in normalized and "mini backpack" in normalized and not any(
            word in normalized for word in excluded
        )

    def parse_product(self, raw: object, *, source_url: str) -> Product:
        if not isinstance(raw, dict):
            raise HMVParseError("HMV Product JSON-LD is not an object")
        offers = raw.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if len(offers) == 1 else None
        if not isinstance(offers, dict):
            raise HMVParseError("HMV product offer is missing")
        if not isinstance(raw.get("name"), str):
            raise HMVParseError("HMV product name is invalid")
        try:
            product_id = str(raw.get("sku") or raw["productID"]).strip()
            name = raw["name"].strip()
            price = Decimal(str(offers["price"]))
            currency = str(offers["priceCurrency"]).strip().upper()
            state = str(offers["availability"]).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise HMVParseError("HMV product identity, price, or availability is invalid") from exc
        states = {
            "InStock": Availability.IN_STOCK, "OutOfStock": Availability.OUT_OF_STOCK,
            "SoldOut": Availability.OUT_OF_STOCK, "PreOrder": Availability.PREORDER,
            "PreSale": Availability.PREORDER, "BackOrder": Availability.BACKORDER,
            "Discontinued": Availability.UNAVAILABLE,
        }
        if not product_id or not name or state not in states or currency != "GBP":
            raise HMVParseError("HMV product has unsupported identity, currency, or availability")
        product_url = urljoin(self.base_url + "/", str(offers.get("url") or raw.get("url") or source_url))
        if urlparse(product_url).netloc.casefold() != urlparse(self.base_url).netloc.casefold():
            raise HMVParseError("HMV product URL is outside HMV")
        image = raw.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get("url")
        if not isinstance(image, str) or not image:
            raise HMVParseError("HMV product image is missing")
        description = str(raw.get("description") or "")
        availability = states[state]
        return Product(
            retailer="HMV", retailer_product_id=product_id, name=name, url=product_url,
            image_url=urljoin(self.base_url + "/", image), price=price, currency="GBP",
            availability=availability, product_type="Mini Backpack",
            exclusive="hmv exclusive" in f"{name} {description}".casefold(),
            preorder=availability == Availability.PREORDER,
        )

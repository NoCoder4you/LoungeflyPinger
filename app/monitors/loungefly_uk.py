"""Official regional Loungefly adapters using Salesforce Commerce Cloud JSON-LD."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

from app.http import AsyncHttpClient, HttpClientError
from app.models import Availability, Product
from app.release import parse_release_text
from app.monitors.base import RetailerMonitor

BASE_URL = "https://loungefly.com"
CATEGORY_PATH = "/gb/shop/backpacks/mini-backpacks/"
PAGE_SIZE = 20
MAX_PAGES = 30


class LoungeflyParseError(ValueError):
    """The official structured product representation was incomplete or changed."""


class _JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[object] = []
        self._capturing = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and (values.get("type") or "").lower() == "application/ld+json":
            self._capturing = True
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag != "script" or not self._capturing:
            return
        self._capturing = False
        try:
            self.values.append(json.loads("".join(self._parts)))
        except (json.JSONDecodeError, TypeError):
            # Other JSON-LD blocks are not part of the adapter contract. A missing
            # valid ItemList/Product below still makes the whole response fail.
            pass

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)


class _ProductFlagParser(HTMLParser):
    """Collect documented storefront product flags keyed by the surrounding SKU."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.flags: dict[str, set[str]] = {}
        self._sku: str | None = None
        self._flag_depth = 0
        self._flag_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("data-pid"):
            self._sku = values["data-pid"]
        flag = values.get("data-product-flag")
        if self._sku and flag:
            self.flags.setdefault(self._sku, set()).add(" ".join(flag.lower().split()))
        classes = (values.get("class") or "").split()
        if self._sku and "product-flag" in classes:
            self._flag_depth = 1
            self._flag_parts = []
        elif self._flag_depth:
            self._flag_depth += 1

    def handle_data(self, data: str) -> None:
        if self._flag_depth:
            self._flag_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._flag_depth:
            return
        self._flag_depth -= 1
        if not self._flag_depth and self._sku:
            flag = " ".join("".join(self._flag_parts).lower().split())
            if flag:
                self.flags.setdefault(self._sku, set()).add(flag)


class LoungeflyMonitor(RetailerMonitor):
    """Discover mini backpacks from a configured official regional storefront."""

    def __init__(self, http: AsyncHttpClient, *, base_url: str = BASE_URL,
                 category_path: str = CATEGORY_PATH, retailer: str = "Loungefly UK",
                 currency: str = "GBP", path_prefix: str = "/gb/",
                 timezone: str = "Europe/London") -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.category_path = category_path
        self.retailer = retailer
        self.currency = currency
        self.path_prefix = path_prefix
        self.timezone = timezone

    async def discover_products(self) -> list[Product]:
        found: dict[str, Product] = {}
        for page in range(MAX_PAGES):
            query = urlencode({"start": page * PAGE_SIZE, "sz": PAGE_SIZE})
            url = f"{urljoin(self.base_url + '/', self.category_path.lstrip('/'))}?{query}"
            html = await self.http.get_text(url)
            raw_products = self.parse_listing(html)
            flags = self.parse_product_flags(html)
            for raw in raw_products:
                if self._is_mini_backpack(raw):
                    product = self.parse_product(raw, source_url=url, flags=flags.get(str(raw.get("sku")), set()))
                    found[product.retailer_product_id] = product
            if len(raw_products) < PAGE_SIZE:
                return list(found.values())
        raise LoungeflyParseError(f"{self.retailer} exceeded the pagination safety limit")

    async def check_product(self, product: Product) -> Product:
        try:
            html = await self.http.get_text(product.url)
            raw = self.parse_product_page(html)
            flags = self.parse_product_flags(html).get(str(raw.get("sku")), set())
            checked = self.parse_product(raw, source_url=product.url, flags=flags)
            if checked.retailer_product_id != product.retailer_product_id:
                raise LoungeflyParseError("Product page SKU changed")
            return checked
        except (HttpClientError, LoungeflyParseError, ValueError, TypeError):
            # Parser or transport failure is not evidence that every item sold out.
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            url = urljoin(self.base_url + "/", self.category_path.lstrip("/"))
            return bool(self.parse_listing(await self.http.get_text(f"{url}?start=0&sz=1")))
        except (HttpClientError, LoungeflyParseError):
            return False

    @classmethod
    def parse_listing(cls, html: object) -> list[dict[str, Any]]:
        values = cls._structured_values(html)
        lists = [value for value in values if isinstance(value, dict) and value.get("@type") == "ItemList"]
        if len(lists) != 1 or not isinstance(lists[0].get("itemListElement"), list):
            raise LoungeflyParseError("Loungefly response has no unambiguous ItemList JSON-LD")
        products: list[dict[str, Any]] = []
        for element in lists[0]["itemListElement"]:
            if not isinstance(element, dict) or not isinstance(element.get("item"), dict):
                raise LoungeflyParseError("ItemList contains an invalid product entry")
            if element["item"].get("@type") != "Product":
                raise LoungeflyParseError("ItemList contains a non-product entry")
            products.append(element["item"])
        return products

    @classmethod
    def parse_product_page(cls, html: object) -> dict[str, Any]:
        products = [
            value for value in cls._structured_values(html)
            if isinstance(value, dict) and value.get("@type") == "Product"
        ]
        if len(products) != 1:
            raise LoungeflyParseError("Product page has no unambiguous Product JSON-LD")
        return products[0]

    @staticmethod
    def _structured_values(html: object) -> list[object]:
        if not isinstance(html, str) or not html.strip():
            raise LoungeflyParseError("Loungefly response was empty")
        parser = _JsonLdParser()
        try:
            parser.feed(html)
            parser.close()
        except (TypeError, ValueError) as exc:
            raise LoungeflyParseError("Loungefly HTML could not be parsed") from exc
        return parser.values

    @staticmethod
    def parse_product_flags(html: object) -> dict[str, set[str]]:
        if not isinstance(html, str):
            raise LoungeflyParseError("Loungefly response was not text")
        parser = _ProductFlagParser()
        parser.feed(html)
        parser.close()
        return parser.flags

    @staticmethod
    def _is_mini_backpack(raw: dict[str, Any]) -> bool:
        name = raw.get("name")
        if not isinstance(name, str):
            return False
        normalized = " ".join(name.lower().replace("-", " ").split())
        excluded = (
            "wallet", "purse", "cardholder", "card holder", "crossbody", "pin", "accessory",
            "keychain", "key chain", "charm", "shirt", "tee", "hoodie", "sweatshirt", "dress",
        )
        return "mini backpack" in normalized and not any(term in normalized for term in excluded)

    def parse_product(self, raw: object, *, source_url: str, flags: set[str] | None = None) -> Product:
        if not isinstance(raw, dict):
            raise LoungeflyParseError("Product JSON-LD is not an object")
        offers = raw.get("offers")
        if not isinstance(offers, dict):
            raise LoungeflyParseError("Product offer is missing")
        try:
            product_id = str(raw["sku"]).strip()
            name = raw["name"].strip()
            availability_name = offers["availability"].rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        except (KeyError, AttributeError, InvalidOperation, TypeError) as exc:
            raise LoungeflyParseError("Product identity, price, or availability is invalid") from exc
        raw_price = offers.get("price")
        raw_currency = offers.get("priceCurrency")
        if (raw_price is None) != (raw_currency is None):
            raise LoungeflyParseError("Product price and currency must both be present or null")
        try:
            price = Decimal(str(raw_price)) if raw_price is not None else None
            currency = raw_currency.strip().upper() if raw_currency is not None else self.currency
        except (AttributeError, InvalidOperation, TypeError) as exc:
            raise LoungeflyParseError("Product price or currency is invalid") from exc
        availability_map = {
            "InStock": Availability.IN_STOCK,
            "OutOfStock": Availability.OUT_OF_STOCK,
            "SoldOut": Availability.OUT_OF_STOCK,
            "PreOrder": Availability.PREORDER,
            "PreSale": Availability.PREORDER,
            "BackOrder": Availability.BACKORDER,
            "Discontinued": Availability.UNAVAILABLE,
            "LimitedAvailability": Availability.LOW_STOCK,
        }
        if not product_id or not name or availability_name not in availability_map:
            raise LoungeflyParseError("Product identity or availability is unsupported")
        if currency != self.currency:
            raise LoungeflyParseError(f"Product currency is not {self.currency}")
        product_url = offers.get("url") or raw.get("@id") or source_url
        if not isinstance(product_url, str):
            raise LoungeflyParseError("Product URL is invalid")
        product_url = urljoin(self.base_url + "/", product_url)
        parsed_url = urlparse(product_url)
        if parsed_url.netloc != urlparse(self.base_url).netloc or not parsed_url.path.startswith(self.path_prefix):
            raise LoungeflyParseError(f"Product URL is outside the {self.retailer} storefront")
        if not parsed_url.path.rstrip("/").endswith(f"/{product_id}.html"):
            raise LoungeflyParseError("Product URL does not match its SKU")
        image = raw.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get("url")
        if not isinstance(image, str) or not image:
            raise LoungeflyParseError("Product image is missing")
        description = raw.get("description") if isinstance(raw.get("description"), str) else ""
        release = parse_release_text(
            description, source=f"{self.retailer} Product JSON-LD", local_timezone=self.timezone
        )
        availability = availability_map[availability_name]
        flags = flags or set()
        if "pre-order" in flags or "preorder" in flags:
            availability = Availability.PREORDER
        elif "coming soon" in flags:
            availability = Availability.COMING_SOON
        elif "low stock" in flags and availability == Availability.IN_STOCK:
            availability = Availability.LOW_STOCK
        return Product(
            retailer=self.retailer,
            retailer_product_id=product_id,
            name=name,
            url=product_url,
            image_url=urljoin(self.base_url + "/", image),
            price=price,
            currency=self.currency,
            availability=availability,
            product_type="Mini Backpack",
            franchise=None,
            character=None,
            exclusive=("web exclusive" in flags or "exclusive" in name.lower()
                       or "a loungefly exclusive" in description.lower()),
            new_release=bool(flags & {"new", "new release"}),
            preorder=availability == Availability.PREORDER,
            sku=product_id,
            release=release,
        )


class LoungeflyUKMonitor(LoungeflyMonitor):
    pass


class LoungeflyUSMonitor(LoungeflyMonitor):
    def __init__(self, http: AsyncHttpClient, *, base_url: str = BASE_URL) -> None:
        super().__init__(http, base_url=base_url, category_path="/shop/backpacks/mini-backpacks/",
                         retailer="Loungefly US", currency="USD", path_prefix="/",
                         timezone="America/Los_Angeles")


# Backwards-compatible public exception name.
LoungeflyUKParseError = LoungeflyParseError

"""Hot Topic family storefront monitors using public schema.org product data."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

from app.http import AsyncHttpClient, HttpClientError
from app.models import Availability, Product
from app.monitors.base import RetailerMonitor
from app.release import parse_release_text

BASE_URL = "https://www.boxlunch.com"
CATEGORY_PATH = "/search"
SEARCH_QUERY = "loungefly mini backpack"
PAGE_SIZE = 80
MAX_PAGES = 10


class BoxLunchParseError(ValueError):
    """BoxLunch's structured collection or product contract changed or was incomplete."""


class _JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[object] = []
        self._parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and (dict(attrs).get("type") or "").casefold() == "application/ld+json":
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "script" or self._parts is None:
            return
        try:
            self.values.append(json.loads("".join(self._parts)))
        except (json.JSONDecodeError, TypeError):
            pass
        self._parts = None


class HotTopicStorefrontMonitor(RetailerMonitor):
    """Shared structured-data monitor for BoxLunch and Hot Topic storefronts."""

    def __init__(self, http: AsyncHttpClient, *, base_url: str, retailer: str,
                 exclusive_label: str, exclusive_retailer: str) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.retailer = retailer
        self.exclusive_label = exclusive_label.casefold()
        self.exclusive_retailer = exclusive_retailer

    def discovery_sources(self) -> tuple[tuple[str, dict[str, str], bool], ...]:
        return ((CATEGORY_PATH, {"q": SEARCH_QUERY}, False),)

    async def discover_products(self) -> list[Product]:
        found: dict[str, Product] = {}
        for path, parameters, is_new_arrivals in self.discovery_sources():
            for page in range(MAX_PAGES):
                query = urlencode({**parameters, "start": page * PAGE_SIZE, "sz": PAGE_SIZE})
                url = f"{urljoin(self.base_url + '/', path.lstrip('/'))}?{query}"
                raw_products = self.parse_listing(await self.http.get_text(url))
                for raw in raw_products:
                    if self._is_loungefly_mini_backpack(raw):
                        product = self.parse_product(raw, source_url=url)
                        if is_new_arrivals:
                            product = replace(product, new_release=True)
                        previous = found.get(product.retailer_product_id)
                        if previous is None or product.new_release:
                            found[product.retailer_product_id] = product
                if len(raw_products) < PAGE_SIZE:
                    break
            else:
                raise BoxLunchParseError(f"{self.retailer} exceeded the pagination safety limit")
        return list(found.values())

    async def check_product(self, product: Product) -> Product:
        try:
            checked = self.parse_product(
                self.parse_product_page(await self.http.get_text(product.url)), source_url=product.url
            )
            if checked.retailer_product_id != product.retailer_product_id:
                raise BoxLunchParseError(f"{self.retailer} product page SKU changed")
            return checked
        except (HttpClientError, BoxLunchParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            query = urlencode({"q": SEARCH_QUERY, "start": 0, "sz": 1})
            url = f"{urljoin(self.base_url + '/', CATEGORY_PATH.lstrip('/'))}?{query}"
            return bool(self.parse_listing(await self.http.get_text(url)))
        except (HttpClientError, BoxLunchParseError):
            return False

    @staticmethod
    def _structured_values(html: object) -> list[object]:
        if not isinstance(html, str) or not html.strip():
            raise BoxLunchParseError("BoxLunch response was empty")
        parser = _JsonLdParser()
        try:
            parser.feed(html)
            parser.close()
        except (TypeError, ValueError) as exc:
            raise BoxLunchParseError("BoxLunch HTML could not be parsed") from exc
        return parser.values

    @classmethod
    def parse_listing(cls, html: object) -> list[dict[str, Any]]:
        pages = [value for value in cls._structured_values(html)
                 if isinstance(value, dict) and value.get("@type") == "CollectionPage"]
        if len(pages) != 1:
            raise BoxLunchParseError("Response has no unambiguous CollectionPage JSON-LD")
        item_list = pages[0].get("mainEntity")
        if not isinstance(item_list, dict) or item_list.get("@type") != "ItemList":
            raise BoxLunchParseError("CollectionPage has no ItemList")
        elements = item_list.get("itemListElement")
        if not isinstance(elements, list):
            raise BoxLunchParseError("Collection ItemList is incomplete")
        products: list[dict[str, Any]] = []
        for element in elements:
            item = element.get("item") if isinstance(element, dict) else None
            if not isinstance(item, dict) or item.get("@type") != "Product":
                raise BoxLunchParseError("Collection ItemList contains an invalid product")
            products.append(item)
        return products

    @classmethod
    def parse_product_page(cls, html: object) -> dict[str, Any]:
        products: list[dict[str, Any]] = []
        for value in cls._structured_values(html):
            candidates = value if isinstance(value, list) else [value]
            products.extend(candidate for candidate in candidates
                            if isinstance(candidate, dict) and candidate.get("@type") == "Product")
        if len(products) != 1:
            raise BoxLunchParseError("Product page has no unambiguous Product JSON-LD")
        return products[0]

    @staticmethod
    def _is_loungefly_mini_backpack(raw: object) -> bool:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            return False
        name = " ".join(raw["name"].casefold().replace("-", " ").split())
        brand = raw.get("brand")
        brand_name = brand.get("name") if isinstance(brand, dict) else brand
        normalized_brand = str(brand_name or "").casefold().replace(" ", "")
        excluded = ("wallet", "bag charm", "crossbody", "tote", "pin", "keychain", "purse")
        loungefly_evidence = normalized_brand in {"loungefly", "loungfly", "lngefly"} or "loungefly" in name
        return loungefly_evidence and "mini backpack" in name and not any(term in name for term in excluded)

    def parse_product(self, raw: object, *, source_url: str) -> Product:
        if not isinstance(raw, dict):
            raise BoxLunchParseError("Product JSON-LD is not an object")
        offers = raw.get("offers")
        if not isinstance(offers, dict):
            raise BoxLunchParseError("Product offer is missing")
        try:
            sku = str(raw["sku"]).strip()
            name = raw["name"].strip()
            price = Decimal(str(offers["price"]))
            currency = offers["priceCurrency"].strip()
            availability_name = offers["availability"].rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        except (KeyError, AttributeError, InvalidOperation, TypeError) as exc:
            raise BoxLunchParseError("Product identity, price, or availability is invalid") from exc
        availability_map = {
            "InStock": Availability.IN_STOCK,
            "OnlineOnly": Availability.IN_STOCK,
            "OutOfStock": Availability.OUT_OF_STOCK,
            "SoldOut": Availability.OUT_OF_STOCK,
            "InStoreOnly": Availability.OUT_OF_STOCK,
            "PreOrder": Availability.PREORDER,
            "PreSale": Availability.PREORDER,
            "BackOrder": Availability.BACKORDER,
            "ComingSoon": Availability.COMING_SOON,
        }
        if not sku or not name or currency != "USD" or availability_name not in availability_map:
            raise BoxLunchParseError("Product identity, USD currency, or availability is unsupported")
        product_url = offers.get("url") or raw.get("@id") or source_url
        if not isinstance(product_url, str):
            raise BoxLunchParseError("Product URL is invalid")
        product_url = urljoin(self.base_url + "/", product_url)
        parsed_url = urlparse(product_url)
        if (parsed_url.netloc != urlparse(self.base_url).netloc
                or not parsed_url.path.startswith("/product/")
                or not parsed_url.path.rstrip("/").endswith(f"/{sku}.html")):
            raise BoxLunchParseError(f"Product URL does not match its {self.retailer} SKU")
        image = raw.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if not isinstance(image, str) or urlparse(image).scheme not in {"http", "https"}:
            raise BoxLunchParseError("Product image is missing or invalid")
        description = raw.get("description") if isinstance(raw.get("description"), str) else ""
        exclusive = self.exclusive_label in f"{name} {description}".casefold()
        availability = availability_map[availability_name]
        return Product(
            retailer=self.retailer, retailer_product_id=sku, name=name, url=product_url,
            image_url=image, price=price, currency="USD", availability=availability,
            product_type="Mini Backpack", exclusive=exclusive,
            exclusive_retailer=self.exclusive_retailer if exclusive else None,
            preorder=availability == Availability.PREORDER, sku=sku,
            release=parse_release_text(
                description, source=f"{self.retailer} Product JSON-LD",
                local_timezone="America/Los_Angeles", date_order="MDY",
            ),
        )


class BoxLunchMonitor(HotTopicStorefrontMonitor):
    def __init__(self, http: AsyncHttpClient, *, base_url: str = BASE_URL) -> None:
        super().__init__(http, base_url=base_url, retailer="BoxLunch",
                         exclusive_label="BoxLunch Exclusive", exclusive_retailer="BoxLunch")


class HotTopicUSMonitor(HotTopicStorefrontMonitor):
    def __init__(self, http: AsyncHttpClient, *, base_url: str = "https://www.hottopic.com") -> None:
        super().__init__(http, base_url=base_url, retailer="Hot Topic US",
                         exclusive_label="Hot Topic Exclusive", exclusive_retailer="Hot Topic")

    def discovery_sources(self) -> tuple[tuple[str, dict[str, str], bool], ...]:
        return (
            (CATEGORY_PATH, {"q": SEARCH_QUERY}, False),
            ("/backpacks-bags/new-arrivals/", {}, True),
        )

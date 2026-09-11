"""Entertainment Earth US monitor using retailer-published structured product data."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

from app.http import AsyncHttpClient, HttpClientError
from app.models import Availability, Product
from app.monitors.base import RetailerMonitor
from app.release import parse_release_text

BASE_URL = "https://www.entertainmentearth.com"
DISCOVERY_PATHS = ("/s/loungefly/c", "/s/entertainment-earth-exclusives/c")
PAGE_SIZE = 60
MAX_PAGES = 20


class EntertainmentEarthParseError(ValueError):
    """Entertainment Earth's structured page contract is missing or malformed."""


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.json_values: list[object] = []
        self.text_parts: list[str] = []
        self._json_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and (dict(attrs).get("type") or "").casefold() == "application/ld+json":
            self._json_parts = []

    def handle_data(self, data: str) -> None:
        if self._json_parts is not None:
            self._json_parts.append(data)
        else:
            self.text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "script" or self._json_parts is None:
            return
        try:
            self.json_values.append(json.loads("".join(self._json_parts)))
        except (json.JSONDecodeError, TypeError):
            pass
        self._json_parts = None


class EntertainmentEarthMonitor(RetailerMonitor):
    def __init__(self, http: AsyncHttpClient, *, base_url: str = BASE_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def discover_products(self) -> list[Product]:
        found: dict[str, Product] = {}
        for path in DISCOVERY_PATHS:
            for page in range(1, MAX_PAGES + 1):
                url = f"{urljoin(self.base_url + '/', path.lstrip('/'))}?{urlencode({'page': page})}"
                raw_products = self.parse_listing(await self.http.get_text(url))
                for raw in raw_products:
                    if self._is_loungefly_mini_backpack(raw):
                        product = self.parse_product(raw, source_url=url)
                        found[product.retailer_product_id] = product
                if len(raw_products) < PAGE_SIZE:
                    break
            else:
                raise EntertainmentEarthParseError("Entertainment Earth exceeded pagination safety limit")
        return list(found.values())

    async def check_product(self, product: Product) -> Product:
        try:
            raw, page_text = self.parse_product_page(await self.http.get_text(product.url))
            checked = self.parse_product(raw, source_url=product.url, page_text=page_text)
            if checked.retailer_product_id.casefold() != product.retailer_product_id.casefold():
                raise EntertainmentEarthParseError("Entertainment Earth item number changed")
            return checked
        except (HttpClientError, EntertainmentEarthParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            url = f"{urljoin(self.base_url + '/', DISCOVERY_PATHS[0].lstrip('/'))}?page=1"
            return bool(self.parse_listing(await self.http.get_text(url)))
        except (HttpClientError, EntertainmentEarthParseError):
            return False

    @staticmethod
    def _parse_html(html: object) -> _PageParser:
        if not isinstance(html, str) or not html.strip():
            raise EntertainmentEarthParseError("Entertainment Earth response was empty")
        parser = _PageParser()
        try:
            parser.feed(html)
            parser.close()
        except (TypeError, ValueError) as exc:
            raise EntertainmentEarthParseError("Entertainment Earth HTML could not be parsed") from exc
        return parser

    @classmethod
    def parse_listing(cls, html: object) -> list[dict[str, Any]]:
        parser = cls._parse_html(html)
        lists: list[dict[str, Any]] = []
        for value in parser.json_values:
            candidates = value if isinstance(value, list) else [value]
            for candidate in candidates:
                if isinstance(candidate, dict) and candidate.get("@type") == "ItemList":
                    lists.append(candidate)
                elif isinstance(candidate, dict) and candidate.get("@type") == "CollectionPage":
                    entity = candidate.get("mainEntity")
                    if isinstance(entity, dict) and entity.get("@type") == "ItemList":
                        lists.append(entity)
        if len(lists) != 1 or not isinstance(lists[0].get("itemListElement"), list):
            raise EntertainmentEarthParseError("Response has no unambiguous product ItemList")
        products = []
        for element in lists[0]["itemListElement"]:
            item = element.get("item") if isinstance(element, dict) else None
            if not isinstance(item, dict) or item.get("@type") != "Product":
                raise EntertainmentEarthParseError("Product ItemList contains an invalid item")
            products.append(item)
        return products

    @classmethod
    def parse_product_page(cls, html: object) -> tuple[dict[str, Any], str]:
        parser = cls._parse_html(html)
        products: list[dict[str, Any]] = []
        for value in parser.json_values:
            candidates = value if isinstance(value, list) else [value]
            products.extend(v for v in candidates if isinstance(v, dict) and v.get("@type") == "Product")
        if len(products) != 1:
            raise EntertainmentEarthParseError("Product page has no unambiguous Product JSON-LD")
        return products[0], " ".join(" ".join(parser.text_parts).split())

    @staticmethod
    def _is_loungefly_mini_backpack(raw: object) -> bool:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            return False
        name = " ".join(raw["name"].casefold().replace("-", " ").split())
        brand = raw.get("brand")
        brand_name = brand.get("name") if isinstance(brand, dict) else brand
        is_loungefly = "loungefly" in name or "loungefly" in str(brand_name or "").casefold()
        # EE calls small novelty accessories "mini-backpack bag charms"; they are not bags.
        accessory = re.search(r"\b(?:bag\s*charm|charm|keychain|key\s*chain|wallet|pin)\b", name)
        return is_loungefly and "mini backpack" in name and accessory is None

    @staticmethod
    def _estimated_ship_date(text: str) -> date | None:
        match = re.search(
            r"\bestimated\s+ship(?:ping)?\s+date\s*:?\s*"
            r"(?:(\d{1,2})/(\d{1,2})/(\d{4})|"
            r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
            r"(\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(\d{4}))",
            text, re.I,
        )
        if not match:
            return None
        try:
            if match.group(1):
                return date(int(match.group(3)), int(match.group(1)), int(match.group(2)))
            month = __import__("datetime").datetime.strptime(match.group(4), "%B").month
            return date(int(match.group(6)), month, int(match.group(5)))
        except ValueError:
            return None

    def parse_product(self, raw: object, *, source_url: str, page_text: str = "") -> Product:
        if not isinstance(raw, dict):
            raise EntertainmentEarthParseError("Product JSON-LD is not an object")
        offers = raw.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if len(offers) == 1 else None
        if not isinstance(offers, dict):
            raise EntertainmentEarthParseError("Product offer is missing")
        try:
            item_number = str(raw.get("sku") or raw["productID"]).strip()
            name = raw["name"].strip()
            price = Decimal(str(offers["price"]))
            currency = offers["priceCurrency"].strip().upper()
        except (KeyError, AttributeError, InvalidOperation, TypeError) as exc:
            raise EntertainmentEarthParseError("Product identity or USD price is invalid") from exc
        if not item_number or not name or currency != "USD" or not price.is_finite() or price < 0:
            raise EntertainmentEarthParseError("Product identity or USD price is unsupported")
        product_url = offers.get("url") or raw.get("url") or raw.get("@id") or source_url
        if not isinstance(product_url, str):
            raise EntertainmentEarthParseError("Product URL is invalid")
        product_url = urljoin(self.base_url + "/", product_url)
        parsed = urlparse(product_url)
        if parsed.netloc.casefold() != urlparse(self.base_url).netloc.casefold() or "/product/" not in parsed.path:
            raise EntertainmentEarthParseError("Product URL is not an Entertainment Earth product")
        image = raw.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if not isinstance(image, str) or urlparse(image).scheme not in {"http", "https"}:
            raise EntertainmentEarthParseError("Product image is missing or invalid")
        description = raw.get("description") if isinstance(raw.get("description"), str) else ""
        status_text = str(raw.get("availabilityText") or raw.get("inventoryStatus") or "")
        schema_status = str(offers.get("availability") or "").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        evidence = status_text or page_text
        if re.search(r"\bpre[ -]?sold out\b", evidence, re.I):
            availability = Availability.OUT_OF_STOCK
            preorder = True
        elif re.search(r"\bsold out\b", evidence, re.I):
            availability = Availability.OUT_OF_STOCK
            preorder = False
        elif re.search(r"\bpre[ -]?order\b", evidence, re.I) or schema_status.casefold() == "preorder":
            availability = Availability.PREORDER
            preorder = True
        elif re.search(r"\bin stock\b", evidence, re.I) or schema_status.casefold() == "instock":
            availability = Availability.IN_STOCK
            preorder = False
        elif schema_status.casefold() in {"outofstock", "soldout", "discontinued"}:
            availability = Availability.OUT_OF_STOCK
            preorder = False
        else:
            raise EntertainmentEarthParseError("Product availability is unsupported")
        metadata_text = " ".join((description, page_text))
        exclusive = bool(re.search(r"\bEntertainment Earth Exclusive\b", metadata_text, re.I))
        return Product(
            retailer="Entertainment Earth", retailer_product_id=item_number, name=name,
            url=product_url, image_url=image, price=price, currency="USD",
            availability=availability, product_type="Mini Backpack", exclusive=exclusive,
            exclusive_retailer="Entertainment Earth" if exclusive else None,
            preorder=preorder, sku=item_number,
            release=parse_release_text(metadata_text, source="Entertainment Earth product page",
                                       local_timezone="America/Los_Angeles", date_order="MDY"),
            estimated_ship_date=self._estimated_ship_date(metadata_text),
        )

"""Regional Disney Store adapters using structured storefront product telemetry."""

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

BASE_URL = "https://www.disneystore.co.uk"
CATEGORY_PATH = "/brands/loungefly"
PAGE_SIZE = 48
MAX_PAGES = 10


class DisneyStoreParseError(ValueError):
    """The public Disney Store product representation was incomplete or changed."""


class _StorefrontParser(HTMLParser):
    """Read the ItemList and product telemetry emitted in each product tile."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.json_values: list[object] = []
        self.tiles: list[dict[str, Any]] = []
        self._json_parts: list[str] | None = None
        self._tile_depth: int | None = None
        self._depth = 0
        self._tile: dict[str, Any] | None = None
        self.microdata_urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if tag == "script" and (values.get("type") or "").casefold() == "application/ld+json":
            self._json_parts = []
        if tag == "div" and "product-grid__tile" in classes:
            self._tile_depth = self._depth
            self._tile = {"tile_id": values.get("data-pid")}
        if self._tile is not None:
            encoded = values.get("data-tealium-productstring")
            if encoded is not None:
                try:
                    self._tile["product"] = json.loads(encoded)
                except (json.JSONDecodeError, TypeError):
                    self._tile["product"] = None
            if tag == "a" and "product__tile_image_link" in classes:
                self._tile["url"] = values.get("href")
            if tag == "img" and "product__tile_image" in classes:
                image_url = values.get("src") or values.get("data-src")
                if image_url:
                    self._tile.setdefault("image_url", image_url)
        if (self._tile is not None and tag == "meta" and values.get("itemprop") == "url"
                and values.get("content")):
            self.microdata_urls.append(values["content"])
        self._depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._json_parts is not None:
            self._json_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        self._depth -= 1
        if tag == "script" and self._json_parts is not None:
            try:
                self.json_values.append(json.loads("".join(self._json_parts)))
            except (json.JSONDecodeError, TypeError):
                pass
            self._json_parts = None
        if self._tile is not None and self._tile_depth == self._depth:
            self.tiles.append(self._tile)
            self._tile = None
            self._tile_depth = None


class DisneyStoreMonitor(RetailerMonitor):
    """Monitor Loungefly Mini Backpacks without browser automation."""

    def __init__(self, http: AsyncHttpClient, *, base_url: str = BASE_URL,
                 category_path: str = CATEGORY_PATH, retailer: str = "Disney Store UK",
                 currency: str = "GBP", timezone: str = "Europe/London",
                 require_variant_id: bool = True) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.category_path = category_path
        self.retailer = retailer
        self.currency = currency
        self.timezone = timezone
        self.require_variant_id = require_variant_id

    async def discover_products(self) -> list[Product]:
        found: dict[str, Product] = {}
        for page in range(MAX_PAGES):
            url = urljoin(self.base_url + "/", self.category_path.lstrip("/"))
            url = f"{url}?{urlencode({'start': page * PAGE_SIZE, 'sz': PAGE_SIZE})}"
            raw_products = self.parse_listing(await self.http.get_text(url))
            for raw in raw_products:
                if self._is_mini_backpack(raw.get("product")):
                    product = self.parse_product(raw, source_url=url)
                    found[product.retailer_product_id] = product
            if len(raw_products) < PAGE_SIZE:
                return list(found.values())
        raise DisneyStoreParseError(f"{self.retailer} exceeded the pagination safety limit")

    async def check_product(self, product: Product) -> Product:
        try:
            raw = self.parse_product_page(await self.http.get_text(product.url))
            checked = self.parse_product(raw, source_url=product.url)
            if checked.retailer_product_id != product.retailer_product_id:
                raise DisneyStoreParseError("Product page ID changed")
            return checked
        except (HttpClientError, DisneyStoreParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            url = urljoin(self.base_url + "/", self.category_path.lstrip("/"))
            return bool(self.parse_listing(await self.http.get_text(f"{url}?start=0&sz=1")))
        except (HttpClientError, DisneyStoreParseError):
            return False

    @classmethod
    def _parse_html(cls, html: object) -> _StorefrontParser:
        if not isinstance(html, str) or not html.strip():
            raise DisneyStoreParseError("Disney Store response was empty")
        parser = _StorefrontParser()
        try:
            parser.feed(html)
            parser.close()
        except (TypeError, ValueError) as exc:
            raise DisneyStoreParseError("Disney Store HTML could not be parsed") from exc
        return parser

    @classmethod
    def parse_listing(cls, html: object) -> list[dict[str, Any]]:
        parser = cls._parse_html(html)
        item_lists = [
            value for value in parser.json_values
            if isinstance(value, dict) and value.get("@type") == "ItemList"
        ]
        if cls is DisneyStoreMonitor:
            raise DisneyStoreParseError("A regional Disney Store monitor is required")
        monitor_requires_json = getattr(cls, "REQUIRE_JSON_ITEM_LIST", True)
        if monitor_requires_json:
            if len(item_lists) != 1 or not isinstance(item_lists[0].get("itemListElement"), list):
                raise DisneyStoreParseError("Response has no unambiguous ItemList JSON-LD")
            expected_urls = {
                item.get("url") for item in item_lists[0]["itemListElement"]
                if isinstance(item, dict) and isinstance(item.get("url"), str)
            }
            expected_count = len(item_lists[0]["itemListElement"])
        else:
            expected_urls = set(parser.microdata_urls)
            expected_count = len(parser.microdata_urls)
        if not expected_urls or len(parser.tiles) != expected_count:
            raise DisneyStoreParseError("ItemList and product tiles are incomplete")
        for tile in parser.tiles:
            if not isinstance(tile.get("product"), dict) or not isinstance(tile.get("url"), str):
                raise DisneyStoreParseError("Product tile is missing structured data")
            absolute = urljoin(getattr(cls, "REGION_BASE_URL", BASE_URL) + "/", tile["url"])
            normalized_expected = {urljoin(getattr(cls, "REGION_BASE_URL", BASE_URL) + "/", url)
                                   for url in expected_urls}
            if absolute not in normalized_expected:
                raise DisneyStoreParseError("Product tile does not match the ItemList")
        return parser.tiles

    @classmethod
    def parse_product_page(cls, html: object) -> dict[str, Any]:
        parser = cls._parse_html(html)
        products: list[dict[str, Any]] = []
        for tile in parser.tiles:
            if isinstance(tile.get("product"), dict):
                products.append(tile)
        # PDP markup has the same telemetry object but no product-grid wrapper.
        if not products:
            class ProductParser(HTMLParser):
                def __init__(self) -> None:
                    super().__init__(convert_charrefs=True)
                    self.values: list[dict[str, Any]] = []
                    self.text: list[str] = []
                def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
                    encoded = dict(attrs).get("data-tealium-productstring")
                    if encoded:
                        try:
                            value = json.loads(encoded)
                            if isinstance(value, dict): self.values.append(value)
                        except (json.JSONDecodeError, TypeError):
                            pass
                def handle_data(self, data: str) -> None:
                    self.text.append(data)
            product_parser = ProductParser()
            product_parser.feed(html)  # type: ignore[arg-type]
            if len(product_parser.values) == 1:
                value = product_parser.values[0]
                return {
                    "tile_id": value.get("id"), "url": None, "product": value,
                    "page_text": " ".join(" ".join(product_parser.text).split()),
                }
        if len(products) != 1:
            raise DisneyStoreParseError("Product page has no unambiguous product telemetry")
        return products[0]

    @staticmethod
    def _is_mini_backpack(raw: object) -> bool:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            return False
        name = " ".join(raw["name"].casefold().replace("-", " ").split())
        brand = str(raw.get("brand", "")).strip().casefold()
        excluded = ("wallet", "purse", "crossbody", "charm", "pin", "headband", "clothing", "toy")
        return (brand == "loungefly" or "loungefly" in name) and "mini backpack" in name and not any(
            x in name for x in excluded
        )

    def parse_product(self, raw: object, *, source_url: str) -> Product:
        if not isinstance(raw, dict) or not isinstance(raw.get("product"), dict):
            raise DisneyStoreParseError("Product telemetry is missing")
        data = raw["product"]
        try:
            product_id = str(data["id"]).strip()
            raw_variant_id = data.get("variant_id")
            variant_id = str(raw_variant_id).strip() if raw_variant_id is not None else product_id
            name = data["name"].strip()
            price = Decimal(str(data["price"]))
            availability_text = data["availability"].strip().casefold().replace("-", "_").replace(" ", "_")
        except (KeyError, AttributeError, InvalidOperation, TypeError) as exc:
            raise DisneyStoreParseError("Product identity, price, or availability is invalid") from exc
        availability_map = {
            "online___in_stock": Availability.IN_STOCK,
            "online___out_of_stock": Availability.OUT_OF_STOCK,
            "online___preorder": Availability.PREORDER,
            "online___pre_order": Availability.PREORDER,
            "online___coming_soon": Availability.COMING_SOON,
            "online___backorder": Availability.BACKORDER,
        }
        if (not product_id or (self.require_variant_id and (
                raw_variant_id is None or product_id != variant_id)) or not name
                or availability_text not in availability_map):
            raise DisneyStoreParseError("Product identity or availability is unsupported")
        images = data.get("image_url")
        image_url = images[0] if isinstance(images, list) and images else raw.get("image_url")
        if not isinstance(image_url, str) or not image_url:
            raise DisneyStoreParseError("Product image is missing")
        product_url = raw.get("url") or source_url
        if not isinstance(product_url, str):
            raise DisneyStoreParseError("Product URL is invalid")
        product_url = urljoin(self.base_url + "/", product_url)
        parsed = urlparse(product_url)
        if parsed.netloc != urlparse(self.base_url).netloc or not parsed.path.endswith(f"-{product_id}.html"):
            raise DisneyStoreParseError("Product URL does not match its ID")
        if urlparse(image_url).scheme not in {"http", "https"}:
            raise DisneyStoreParseError("Product image URL is invalid")
        availability = availability_map[availability_text]
        name_marker = name.casefold()
        badges = {
            str(data.get(key, "")).strip().casefold() for key in ("badge", "badge_global")
        }
        message = str(data.get("message", "")).strip().casefold()
        page_text = str(raw.get("page_text", "")).casefold()
        parks = ("disney parks" in name_marker
                 or "from the disney parks | loungefly collection" in page_text
                 or bool(badges & {"disney parks", "parks exclusive"}))
        exclusive = ("disney exclusive" in name_marker or "disney store exclusive" in name_marker or parks
                     or "disney store exclusive" in page_text
                     or bool(badges & {"exclusive", "uniquely disney", "disney store exclusive"}))
        character = data.get("pims_character_name")
        if not isinstance(character, str) or not character.strip():
            character = None
        release_text = next((data.get(key) for key in (
            "release_date_text", "preorder_message", "availability_message"
        ) if isinstance(data.get(key), str)), None)
        release = parse_release_text(
            release_text, source=f"{self.retailer} product telemetry",
            local_timezone=self.timezone,
        )
        return Product(
            retailer=self.retailer, retailer_product_id=product_id, name=name,
            url=product_url, image_url=image_url, price=price, currency=self.currency,
            availability=availability, product_type="Mini Backpack",
            franchise="Disney Parks" if parks else None,
            character=character, exclusive=exclusive,
            new_release=message.startswith("new"),
            preorder=availability == Availability.PREORDER,
            sku=product_id,
            release=release,
        )


class DisneyStoreUKMonitor(DisneyStoreMonitor):
    REGION_BASE_URL = BASE_URL
    REQUIRE_JSON_ITEM_LIST = True


class DisneyStoreUSMonitor(DisneyStoreMonitor):
    REGION_BASE_URL = "https://www.disneystore.com"
    REQUIRE_JSON_ITEM_LIST = False

    def __init__(self, http: AsyncHttpClient, *, base_url: str = REGION_BASE_URL) -> None:
        super().__init__(
            http, base_url=base_url, category_path="/brands/loungefly/",
            retailer="Disney Store US", currency="USD", timezone="America/Los_Angeles",
            require_variant_id=False,
        )


# Backwards-compatible public exception name.
DisneyStoreUKParseError = DisneyStoreParseError

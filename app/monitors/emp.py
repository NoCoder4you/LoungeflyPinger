"""Shared Salesforce Commerce Cloud adapter for EMP's European storefronts."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlparse

from app.http import AsyncHttpClient, HttpClientError
from app.models import Availability, Product, ReleaseInfo, ReleasePrecision
from app.monitors.base import RetailerMonitor


@dataclass(frozen=True, slots=True)
class EMPRegion:
    code: str
    name: str
    base_url: str
    language: str
    currency: str = "EUR"
    mini_backpack_terms: tuple[str, ...] = ()


EMP_REGIONS = {
    "de": EMPRegion("de", "EMP Germany", "https://www.emp.de", "de-DE", mini_backpack_terms=("mini rucksack", "minirucksack")),
    "fr": EMPRegion("fr", "EMP France", "https://www.emp-online.fr", "fr-FR", mini_backpack_terms=("mini sac a dos",)),
    "es": EMPRegion("es", "EMP Spain", "https://www.emp-online.es", "es-ES", mini_backpack_terms=("mini mochila",)),
    "it": EMPRegion("it", "EMP Italy", "https://www.emp-online.it", "it-IT", mini_backpack_terms=("mini zaino",)),
}
PAGE_SIZE, MAX_PAGES = 120, 20


class EMPParseError(ValueError):
    """The common EMP page contract is missing or malformed."""


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.products: list[dict[str, object]] = []
        self.page: dict[str, object] = {"text": []}
        self.product: dict[str, object] | None = None
        self.depth = 0

    def handle_starttag(self, tag, pairs):
        attrs = dict(pairs)
        void = tag in {"meta", "link", "img", "input", "br", "hr"}
        if self.product is None and "product-tile" in (attrs.get("class") or "").split():
            self.product = {"tile_url": attrs.get("data-url"), "item_id": attrs.get("data-itemid")}
            self.depth = 1
        elif self.product is not None and not void:
            self.depth += 1
        target = self.product if self.product is not None else self.page
        prop = (attrs.get("itemprop") or attrs.get("itemProp") or "").casefold()
        value = attrs.get("content") or attrs.get("href") or attrs.get("src")
        if prop and value:
            target.setdefault(prop, [])
            target[prop].append(value)
        if attrs.get("data-product-type"):
            self.page["product_type"] = attrs["data-product-type"]
        if self.product is None and not self.page.get("item_id"):
            product_id = attrs.get("data-pid") or attrs.get("data-itemid")
            if product_id and product_id.isdigit():
                self.page["item_id"] = product_id

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if data.strip():
            self.page["text"].append(data.strip())

    def handle_endtag(self, tag):
        if self.product is None or tag in {"meta", "link", "img", "input", "br", "hr"}:
            return
        self.depth -= 1
        if not self.depth:
            self.products.append(self.product)
            self.product = None


class EMPMonitor(RetailerMonitor):
    """One configurable parser for all supported EMP regional storefronts."""
    def __init__(self, http: AsyncHttpClient, region: EMPRegion | str) -> None:
        self.http = http
        try:
            self.region = EMP_REGIONS[region] if isinstance(region, str) else region
        except KeyError as exc:
            raise ValueError(f"Unsupported EMP region: {region}") from exc

    async def discover_products(self) -> list[Product]:
        found = {}
        for page in range(MAX_PAGES):
            url = f"{self.region.base_url}/search?{urlencode({'q': 'loungefly', 'sz': PAGE_SIZE, 'start': page * PAGE_SIZE})}"
            raws = self.parse_listing(await self.http.get_text(url))
            for raw in raws:
                if self._is_mini_backpack(raw):
                    product = self.parse_product(raw, source_url=url)
                    found[product.retailer_product_id] = product
            if len(raws) < PAGE_SIZE:
                return list(found.values())
        raise EMPParseError(f"{self.region.name} exceeded pagination safety limit")

    async def check_product(self, product: Product) -> Product:
        try:
            checked = self.parse_product(self.parse_product_page(await self.http.get_text(product.url)), source_url=product.url)
            if checked.retailer_product_id != product.retailer_product_id:
                raise EMPParseError("EMP article number changed")
            return checked
        except (HttpClientError, EMPParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            url = f"{self.region.base_url}/search?{urlencode({'q': 'loungefly', 'sz': PAGE_SIZE, 'start': 0})}"
            return bool(self.parse_listing(await self.http.get_text(url)))
        except (HttpClientError, EMPParseError):
            return False

    @staticmethod
    def _parse(html: object) -> _Parser:
        if not isinstance(html, str) or not html.strip():
            raise EMPParseError("EMP response was empty")
        parser = _Parser()
        try:
            parser.feed(html); parser.close()
        except (TypeError, ValueError) as exc:
            raise EMPParseError("EMP HTML could not be parsed") from exc
        return parser

    @classmethod
    def parse_listing(cls, html: object) -> list[dict[str, object]]:
        products = cls._parse(html).products
        if not products:
            raise EMPParseError("EMP response contains no product tiles")
        return products

    @classmethod
    def parse_product_page(cls, html: object) -> dict[str, object]:
        page = cls._parse(html).page
        if not page.get("name") or not page.get("availability"):
            raise EMPParseError("EMP product microdata is missing")
        return page

    @staticmethod
    def _first(raw, key):
        value = raw.get(key)
        return value[0] if isinstance(value, list) and value and isinstance(value[0], str) else value if isinstance(value, str) else None

    @staticmethod
    def _fold(value):
        return " ".join(unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold().replace("-", " ").split())

    def _is_mini_backpack(self, raw):
        name = self._fold(self._first(raw, "name") or "")
        return "loungefly" in name and any(term in name for term in self.region.mini_backpack_terms)

    def parse_product(self, raw: object, *, source_url: str) -> Product:
        if not isinstance(raw, dict):
            raise EMPParseError("EMP product is not an object")
        name = (self._first(raw, "name") or "").strip()
        article_id = (self._first(raw, "productid") or str(raw.get("item_id") or "")).removeprefix("sku:").strip()
        url = urljoin(self.region.base_url + "/", self._first(raw, "url") or str(raw.get("tile_url") or source_url))
        if not name or not article_id or urlparse(url).netloc.casefold() != urlparse(self.region.base_url).netloc.casefold():
            raise EMPParseError("EMP product identity or URL is invalid")
        try:
            values = raw.get("price")
            prices = [Decimal(str(v)) for v in values] if isinstance(values, list) else []
        except InvalidOperation as exc:
            raise EMPParseError("EMP price is invalid") from exc
        if not prices or any(not p.is_finite() or p < 0 for p in prices):
            raise EMPParseError("EMP price is missing or invalid")
        currency = (self._first(raw, "pricecurrency") or "").upper()
        if currency != self.region.currency:
            raise EMPParseError("EMP currency does not match storefront")
        status = (self._first(raw, "availability") or "").rsplit("/", 1)[-1].casefold()
        text = " ".join(raw.get("text", [])) if isinstance(raw.get("text"), list) else ""
        folded = self._fold(text)
        preorder = status == "preorder" or bool(re.search(r"\b(vorbestell|precommande|preventa|preordine)", folded))
        low = bool(re.search(r"\b(nur noch\s+\d+|plus que\s+\d+|quedan\s+\d+|rimast[ioe]\s+\d+)\b", folded))
        if status in {"outofstock", "soldout", "discontinued"}: availability = Availability.OUT_OF_STOCK
        elif preorder: availability = Availability.PREORDER
        elif status == "instock": availability = Availability.LOW_STOCK if low else Availability.IN_STOCK
        else: raise EMPParseError("EMP availability is unsupported")
        release = None
        if value := self._first(raw, "releasedate"):
            try: release_date = date.fromisoformat(value)
            except ValueError as exc: raise EMPParseError("EMP structured release date is invalid") from exc
            release = ReleaseInfo(ReleasePrecision.DATE_ONLY, release_date=release_date, text=value, source=f"{self.region.name} releaseDate metadata")
        original = max(prices) if len(prices) > 1 and max(prices) > prices[0] else None
        return Product(retailer=self.region.name, retailer_product_id=article_id, sku=article_id,
                       name=name, url=url, image_url=self._first(raw, "image"), price=prices[0],
                       original_price=original, currency=currency, availability=availability,
                       preorder=preorder, product_type="Mini Backpack", release=release)

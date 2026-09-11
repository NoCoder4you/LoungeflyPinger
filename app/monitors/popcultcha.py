"""Popcultcha Australia adapter for its public Magento catalogue pages."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

from app.http import AsyncHttpClient, HttpClientError
from app.models import Availability, Product
from app.monitors.base import RetailerMonitor
from app.release import parse_release_text

BASE_URL = "https://www.popcultcha.com.au"
CATALOGUE_PATH = "/shop-by/manufacturer/loungefly.html"
MAX_PAGES = 40


class PopcultchaParseError(ValueError):
    """Popcultcha's public page contract was incomplete or malformed."""


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.json_values: list[object] = []
        self.links: list[tuple[str, str, str]] = []
        self.text_parts: list[str] = []
        self._json_parts: list[str] | None = None
        self._link: tuple[str, str] | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and (values.get("type") or "").casefold() == "application/ld+json":
            self._json_parts = []
        elif tag == "a" and values.get("href"):
            self._link = (values["href"] or "", values.get("rel") or "")
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._json_parts is not None:
            self._json_parts.append(data)
        else:
            self.text_parts.append(data)
            if self._link is not None:
                self._link_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._json_parts is not None:
            try:
                self.json_values.append(json.loads("".join(self._json_parts)))
            except (json.JSONDecodeError, TypeError):
                pass
            self._json_parts = None
        elif tag == "a" and self._link is not None:
            self.links.append((*self._link, " ".join("".join(self._link_text).split())))
            self._link = None


class PopcultchaMonitor(RetailerMonitor):
    """Monitor Loungefly mini backpacks without treating Magento salability as preorder state."""

    def __init__(self, http: AsyncHttpClient, *, base_url: str = BASE_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def discover_products(self) -> list[Product]:
        found: dict[str, Product] = {}
        for page in range(1, MAX_PAGES + 1):
            url = urljoin(self.base_url + "/", CATALOGUE_PATH.lstrip("/"))
            if page > 1:
                url += "?" + urlencode({"p": page})
            links, has_next = self.parse_listing(await self.http.get_text(url))
            for product_url in links:
                try:
                    product = self.parse_product_page(await self.http.get_text(product_url), product_url)
                except (HttpClientError, PopcultchaParseError, ValueError, TypeError):
                    continue
                if self._is_loungefly_mini_backpack(product):
                    found[product.retailer_product_id] = product
            if not has_next:
                return list(found.values())
        raise PopcultchaParseError("Popcultcha catalogue exceeded the pagination safety limit")

    async def check_product(self, product: Product) -> Product:
        try:
            checked = self.parse_product_page(await self.http.get_text(product.url), product.url)
            if checked.retailer_product_id != product.retailer_product_id:
                raise PopcultchaParseError("Popcultcha SKU changed")
            return checked
        except (HttpClientError, PopcultchaParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            url = urljoin(self.base_url + "/", CATALOGUE_PATH.lstrip("/"))
            self.parse_listing(await self.http.get_text(url))
            return True
        except (HttpClientError, PopcultchaParseError):
            return False

    @staticmethod
    def _parser(html: object) -> _PageParser:
        if not isinstance(html, str) or not html.strip():
            raise PopcultchaParseError("Popcultcha response was empty")
        parser = _PageParser()
        parser.feed(html)
        parser.close()
        return parser

    def parse_listing(self, html: object) -> tuple[list[str], bool]:
        parser = self._parser(html)
        host = urlparse(self.base_url).netloc.casefold()
        links: list[str] = []
        has_next = False
        for href, rel, text in parser.links:
            absolute = urljoin(self.base_url + "/", href)
            if "next" in rel.casefold() or text.casefold() == "next":
                has_next = True
            path = urlparse(absolute).path.casefold()
            if (urlparse(absolute).netloc.casefold() == host and path.endswith(".html")
                    and "mini backpack" in " ".join(text.casefold().replace("-", " ").split())
                    and absolute not in links):
                links.append(absolute)
        if not links:
            raise PopcultchaParseError("Popcultcha listing has no mini-backpack product links")
        return links, has_next

    @staticmethod
    def _product_json(parser: _PageParser) -> dict[str, Any]:
        products: list[dict[str, Any]] = []
        for value in parser.json_values:
            values = value if isinstance(value, list) else [value]
            for candidate in values:
                if isinstance(candidate, dict) and candidate.get("@type") == "Product":
                    products.append(candidate)
                elif isinstance(candidate, dict) and isinstance(candidate.get("@graph"), list):
                    products.extend(item for item in candidate["@graph"]
                                    if isinstance(item, dict) and item.get("@type") == "Product")
        if len(products) != 1:
            raise PopcultchaParseError("Product page has no unambiguous Product JSON-LD")
        return products[0]

    @staticmethod
    def _eta(text: str) -> date | None:
        match = re.search(
            r"\bETA\s*:?\s*(?:(\d{1,2})[/-](\d{1,2})[/-](\d{4})|"
            r"(\d{1,2})(?:st|nd|rd|th)?\s+(January|February|March|April|May|June|July|"
            r"August|September|October|November|December)\s+(\d{4}))", text, re.I)
        if not match:
            return None
        try:
            if match.group(1):
                return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
            month = datetime.strptime(match.group(5), "%B").month
            return date(int(match.group(6)), month, int(match.group(4)))
        except ValueError:
            return None

    @staticmethod
    def _is_loungefly_mini_backpack(product: Product) -> bool:
        name = " ".join(product.name.casefold().replace("-", " ").split())
        excluded = re.search(r"\b(?:wallet|purse|crossbody|bag charm|charm|keychain|pin)\b", name)
        return product.vendor.casefold() == "loungefly" and "mini backpack" in name and not excluded

    def parse_product_page(self, html: object, source_url: str) -> Product:
        parser = self._parser(html)
        raw = self._product_json(parser)
        text = " ".join(" ".join(parser.text_parts).split())
        offers = raw.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if len(offers) == 1 else None
        if not isinstance(offers, dict):
            raise PopcultchaParseError("Product offer is missing")
        brand = raw.get("brand")
        manufacturer = brand.get("name") if isinstance(brand, dict) else brand
        manufacturer_match = re.search(r"\bManufacturer\s*:?\s*([^|]+?)(?=\s+(?:SKU|Barcode|ETA)\b|$)", text, re.I)
        manufacturer = str(manufacturer or (manufacturer_match.group(1) if manufacturer_match else "")).strip()
        try:
            name = str(raw["name"]).strip()
            sku = str(raw.get("sku") or re.search(r"\bSKU\s*:?\s*([A-Za-z0-9._-]+)", text, re.I).group(1)).strip()
            price = Decimal(str(offers["price"]).replace(",", ""))
            currency = str(offers["priceCurrency"]).strip().upper()
        except (KeyError, AttributeError, InvalidOperation, TypeError) as exc:
            raise PopcultchaParseError("Product identity or price is invalid") from exc
        if not name or not sku or currency != "AUD" or not price.is_finite() or price < 0:
            raise PopcultchaParseError("Product identity or AUD price is unsupported")
        url = raw.get("url") or offers.get("url") or source_url
        if not isinstance(url, str):
            raise PopcultchaParseError("Product URL is invalid")
        url = urljoin(self.base_url + "/", url)
        if urlparse(url).netloc.casefold() != urlparse(self.base_url).netloc.casefold():
            raise PopcultchaParseError("Product URL is outside Popcultcha")
        schema_status = str(offers.get("availability") or "").rsplit("/", 1)[-1].casefold()
        preorder = bool(re.search(r"\bpre[ -]?order\b", text, re.I))
        sold_out = bool(re.search(r"\b(?:sold out|out of stock|pre[ -]?sold out)\b", text, re.I))
        # Popcultcha can show Magento's generic "In Stock" alongside a PRE-ORDER
        # purchase action. The explicit preorder state is the customer-facing meaning.
        if sold_out:
            availability = Availability.OUT_OF_STOCK
        elif preorder:
            availability = Availability.PREORDER
        elif schema_status == "instock" or re.search(r"\bin stock\b", text, re.I):
            availability = Availability.IN_STOCK
        elif schema_status in {"outofstock", "soldout", "discontinued"}:
            availability = Availability.OUT_OF_STOCK
        else:
            raise PopcultchaParseError("Product availability is unsupported")
        barcode = raw.get("gtin13") or raw.get("gtin")
        barcode_match = re.search(r"\bBarcode\s*:?\s*(\d{8,14})\b", text, re.I)
        barcode = str(barcode or (barcode_match.group(1) if barcode_match else "")).strip() or None
        image = raw.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if image is not None and not isinstance(image, str):
            raise PopcultchaParseError("Product image is invalid")
        description = str(raw.get("description") or "")
        release_text = re.sub(r"\bETA\s*:?\s*[^.;|]+", " ", " ".join((description, text)), flags=re.I)
        return Product(
            retailer="Popcultcha", retailer_product_id=sku, name=name, url=url,
            image_url=image, price=price, currency="AUD", availability=availability,
            product_type="Mini Backpack", preorder=preorder, sku=sku, barcode=barcode,
            vendor=manufacturer,
            estimated_ship_date=self._eta(" ".join((description, text))),
            release=parse_release_text(release_text, source="Popcultcha product page",
                                       local_timezone="Australia/Melbourne", date_order="DMY"),
        )

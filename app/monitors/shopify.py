"""Reusable public Shopify collection-feed retailer support."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html import unescape
import re
from typing import Any
from urllib.parse import quote, urljoin

from app.http import AsyncHttpClient, HttpClientError
from app.models import Availability, Product
from app.monitors.base import RetailerMonitor
from app.release import parse_release_text


class ShopifyParseError(ValueError):
    """A public Shopify representation omitted required commerce data."""


class ShopifyRetailerMonitor(RetailerMonitor):
    retailer_name = "Shopify"
    base_url = ""
    currency = "USD"
    local_timezone: str | None = None
    date_order = "MDY"
    collections: dict[str, str | None] = {}
    page_size = 250
    max_pages = 10
    collection_page_limits: dict[str, int] = {}
    low_stock_threshold = 3
    retailer_exclusive_pattern: str | None = None
    loungefly_collection_handles: frozenset[str] = frozenset()

    def __init__(self, http: AsyncHttpClient, *, base_url: str | None = None) -> None:
        self.http = http
        self.base_url = (base_url or type(self).base_url).rstrip("/")

    async def _collection_products(self, handle: str) -> list[dict[str, Any]]:
        result = []
        max_pages = self.collection_page_limits.get(handle, self.max_pages)
        for page in range(1, max_pages + 1):
            payload = await self.http.get_json(
                f"{self.base_url}/collections/{quote(handle)}/products.json"
                f"?limit={self.page_size}&page={page}"
            )
            batch = self._product_list(payload)
            result.extend(batch)
            if len(batch) < self.page_size:
                return result
        if handle in self.collection_page_limits:
            return result
        raise ShopifyParseError(f"Collection {handle} exceeded pagination safety limit")

    async def discover_products(self) -> list[Product]:
        raw_by_id: dict[str, dict[str, Any]] = {}
        memberships: dict[str, set[str]] = {}
        signals: dict[str, set[str]] = {}
        for handle, signal in self.collections.items():
            for raw in await self._collection_products(handle):
                product_id = str(raw.get("id") or "").strip()
                if not product_id:
                    raise ShopifyParseError("Shopify product ID is missing")
                raw_by_id[product_id] = raw
                memberships.setdefault(product_id, set()).add(handle)
                if signal:
                    signals.setdefault(product_id, set()).add(signal)
        products = []
        for product_id, raw in raw_by_id.items():
            product_type = self.classify_product_type(raw)
            if product_type and (self.is_loungefly(raw) or
                                 memberships[product_id] & self.loungefly_collection_handles):
                products.append(self.parse_product(
                    raw, product_type=product_type,
                    collections=memberships[product_id], signals=signals.get(product_id, set()),
                ))
        return products

    async def check_product(self, product: Product) -> Product:
        handle = product.url.rstrip("/").rsplit("/", 1)[-1]
        try:
            raw = await self.http.get_json(f"{self.base_url}/products/{quote(handle)}.js")
            product_type = self.classify_product_type(raw)
            if not product_type or not (
                self.is_loungefly(raw) or set(product.collections) & self.loungefly_collection_handles
            ):
                raise ShopifyParseError("Product is no longer an enabled Loungefly bag")
            return self.parse_product(raw, product_type=product_type,
                                      collections=set(product.collections), signals=set())
        except (HttpClientError, ShopifyParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            handle = next(iter(self.collections))
            self._product_list(await self.http.get_json(
                f"{self.base_url}/collections/{quote(handle)}/products.json?limit=1&page=1"
            ))
            return True
        except (HttpClientError, ShopifyParseError, StopIteration):
            return False

    @staticmethod
    def _product_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise ShopifyParseError("Shopify response has no product list")
        if not all(isinstance(item, dict) for item in payload["products"]):
            raise ShopifyParseError("Shopify product list contains invalid entries")
        return payload["products"]

    @staticmethod
    def _plain(value: object) -> str:
        return " ".join(unescape(re.sub(r"<[^>]+>", " ", str(value))).split())

    @staticmethod
    def _tags(raw: dict[str, Any]) -> tuple[str, ...]:
        value = raw.get("tags", [])
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return tuple(item.strip() for item in value if item.strip())
        return ()

    @classmethod
    def is_loungefly(cls, raw: dict[str, Any]) -> bool:
        tags = " ".join(cls._tags(raw))
        return bool(re.search(r"\bloungefly\b", " ".join((
            str(raw.get("vendor") or ""), tags, str(raw.get("title") or ""),
            cls._plain(raw.get("body_html") or raw.get("description") or ""),
        )), re.I))

    @classmethod
    def classify_product_type(cls, raw: object) -> str | None:
        if not isinstance(raw, dict):
            return None
        title = str(raw.get("title") or "")
        structured = " ".join((str(raw.get("product_type") or raw.get("type") or ""),
                               " ".join(cls._tags(raw))))
        description = cls._plain(raw.get("body_html") or raw.get("description") or "")
        evidence = " ".join((structured, title, description))
        # Specific accessory nouns override decorative bag words.
        accessory = re.search(r"\b(?:wallets?|card[ -]?holders?|coin[ -]?purses?|"
                              r"cosmetic[ -]?bags?|pins?|enamel pins?|key[ -]?chains?|"
                              r"bag[ -]?(?:clips?|charms?)|apparel|shirts?|hats?)\b",
                              " ".join((structured, title)), re.I)
        bag_with_accessory = re.search(
            r"\b(?:backpack|crossbody|tote|shoulder|handbag|satchel|bucket|sling|messenger|duffle)"
            r"\b[^.]{0,80}\bwith\b[^.]{0,50}\b(?:charm|coin bag|pins?|wallet)\b", title, re.I
        )
        if accessory and not bag_with_accessory:
            return None
        categories = (
            (r"\bmini[ -]+backpacks?\b", "Mini Backpack"),
            (r"\bmid[ -]?size(?:d)?\s+backpacks?\b", "Mid Size Backpack"),
            (r"\bfull[ -]?size(?:d)?\s+backpacks?\b", "Full Size Backpack"),
            (r"\bconvertible\s+(?:(?:tote|backpack)\s+)?bags?\b|"
             r"\bconvertible\s+backpacks?\b", "Convertible Bag"),
            (r"\bcross[ -]?bod(?:y|ies)(?:\s+bags?)?\b", "Crossbody"),
            (r"\bshoulder\s+bags?\b", "Shoulder Bag"), (r"\bhandbags?\b", "Handbag"),
            (r"\bsatchels?\b", "Satchel"), (r"\bbucket\s+bags?\b", "Bucket Bag"),
            (r"\bdrawstring\s+bags?\b", "Drawstring Bag"),
            (r"\bsling\s+bags?\b", "Sling Bag"),
            (r"\bmessenger\s+bags?\b", "Messenger Bag"),
            (r"\bduff(?:le|el)\s+bags?\b", "Duffle Bag"),
            (r"\bbelt\s+bags?\b|\b(?:waist|hip)\s+packs?\b", "Belt Bag"),
            (r"\btotes?(?:\s+bags?)?\b", "Tote"), (r"\bbackpacks?\b", "Backpack"),
        )
        # Structured taxonomy wins except where the title offers a more specific bag type.
        for source in (title, structured, description):
            for pattern, value in categories:
                if re.search(pattern, source, re.I):
                    return value
        if re.search(r"\bbags?\b", evidence, re.I):
            return "Other Bag"
        return None

    @staticmethod
    def _money(value: object) -> Decimal:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError) as exc:
            raise ShopifyParseError("Shopify variant price is invalid") from exc
        if not result.is_finite() or result < 0:
            raise ShopifyParseError("Shopify variant price is invalid")
        return result / 100 if isinstance(value, int) else result

    @staticmethod
    def _eta(text: str) -> tuple[date | None, date | None, str | None]:
        pattern = (r"\b(?P<label>ETA|expected arrival|estimated arrival|expected delivery|"
                   r"estimated delivery|estimated ship(?:ping)? date)\s*:?\s*"
                   r"(?P<value>(?:January|February|March|April|May|June|July|August|"
                   r"September|October|November|December)(?:\s+\d{1,2}(?:st|nd|rd|th)?,?)?"
                   r"\s+\d{4})")
        ship_date = arrival_date = None
        matches = list(re.finditer(pattern, text, re.I))
        for match in matches:
            clean = re.sub(r"(\d)(?:st|nd|rd|th)", r"\1", match.group("value"), flags=re.I)
            # Preserve month-only estimates as text; inventing the first day would
            # incorrectly turn imprecise retailer guidance into an exact date.
            if not re.search(r"\b\d{1,2}(?:st|nd|rd|th)?\b", match.group("value"), re.I):
                continue
            try:
                parsed = datetime.strptime(clean.replace(",", ""), "%B %d %Y").date()
            except ValueError:
                continue
            if "ship" in match.group("label").casefold():
                ship_date = parsed
            else:
                arrival_date = parsed
        return ship_date, arrival_date, "; ".join(match.group(0) for match in matches) or None

    @classmethod
    def _franchise(cls, raw: dict[str, Any], tags: tuple[str, ...]) -> str | None:
        evidence = " ".join((str(raw.get("product_type") or raw.get("type") or ""), *tags))
        franchises = (
            (r"\bstar wars\b", "Star Wars"), (r"\bpok[eé]mon\b", "Pokémon"),
            (r"\bharry potter\b", "Harry Potter"), (r"\bmarvel\b", "Marvel"),
            (r"\bsanrio\b", "Sanrio"), (r"\bdisney\b", "Disney"),
            (r"\buniversal\b", "Universal"), (r"\bhorror\b", "Horror"),
            (r"\banime\b", "Anime"),
        )
        return next((name for pattern, name in franchises if re.search(pattern, evidence, re.I)), None)

    def parse_product(self, raw: object, *, product_type: str,
                      collections: set[str], signals: set[str]) -> Product:
        if not isinstance(raw, dict):
            raise ShopifyParseError("Product is not an object")
        try:
            product_id, title, handle = (str(raw["id"]).strip(), raw["title"].strip(),
                                         raw["handle"].strip())
            variants = raw["variants"]
        except (KeyError, AttributeError) as exc:
            raise ShopifyParseError("Product identity is incomplete") from exc
        if not product_id or not title or not handle or not isinstance(variants, list) or not variants:
            raise ShopifyParseError("Product identity or variants are incomplete")
        if not all(isinstance(item, dict) and isinstance(item.get("available"), bool)
                   for item in variants):
            raise ShopifyParseError("Variant availability is missing")
        selected = next((item for item in variants if item["available"]), variants[0])
        variant_id = str(selected.get("id") or "").strip()
        if not variant_id:
            raise ShopifyParseError("Shopify variant ID is missing")
        price = self._money(selected.get("price"))
        compare_value = selected.get("compare_at_price")
        compare = None if compare_value in (None, "") else self._money(compare_value)
        tags = self._tags(raw)
        description = self._plain(raw.get("body_html") or raw.get("description") or "")
        evidence = " ".join((title, description, *tags))
        preorder = "preorder" in signals or bool(re.search(r"\bpre[ -]?orders?\b", evidence, re.I))
        inventory = selected.get("inventory_quantity")
        if preorder:
            availability = Availability.PREORDER
        elif re.search(r"\bback[ -]?orders?\b", evidence, re.I):
            availability = Availability.BACKORDER
        elif re.search(r"\bcoming soon\b", evidence, re.I):
            availability = Availability.COMING_SOON
        elif not any(item["available"] for item in variants):
            availability = Availability.OUT_OF_STOCK
        elif isinstance(inventory, int) and 0 < inventory <= self.low_stock_threshold:
            availability = Availability.LOW_STOCK
        else:
            availability = Availability.IN_STOCK
        published = None
        if raw.get("published_at"):
            try:
                published = datetime.fromisoformat(str(raw["published_at"]).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ShopifyParseError("Publication timestamp is invalid") from exc
            if published.tzinfo is None:
                raise ShopifyParseError("Publication timestamp has no timezone")
        image = raw.get("featured_image")
        if isinstance(image, dict): image = image.get("src")
        images = raw.get("images")
        if not image and isinstance(images, list) and images:
            image = images[0].get("src") if isinstance(images[0], dict) else images[0]
        if isinstance(image, str) and image.startswith("//"): image = "https:" + image
        if image is not None and not isinstance(image, str):
            raise ShopifyParseError("Product image is invalid")
        release = parse_release_text(description, source=f"{self.retailer_name} Shopify product data",
                                     local_timezone=self.local_timezone, date_order=self.date_order)
        ship_date, arrival_date, eta_text = self._eta(description)
        retailer_exclusive = bool(self.retailer_exclusive_pattern and re.search(
            self.retailer_exclusive_pattern, evidence, re.I
        ))
        region = re.search(
            r"\b(US|USA|United States|UK|EU|Canada|AU|Australia|Australian)\s+exclusive\b",
            evidence, re.I,
        )
        exclusive_region = None
        if region:
            exclusive_region = {"usa": "US", "united states": "US", "australia": "AU",
                                "australian": "AU"}.get(
                region.group(1).casefold(), region.group(1).upper())
        explicit_exclusive = retailer_exclusive or exclusive_region is not None or bool(
            re.search(r"\bexclusive\b", evidence, re.I)
        )
        barcode, sku, vendor = selected.get("barcode"), selected.get("sku"), raw.get("vendor")
        ordered_collections = tuple(sorted(collections))
        rare = "rare" in signals
        vaulted = bool(re.search(r"\b(?:vaulted|retired)\b", evidence, re.I))
        limited_edition = bool(re.search(r"\blimited edition\b", evidence, re.I))
        limited_release = bool(re.search(r"\blimited release\b", evidence, re.I))
        return Product(
            retailer=self.retailer_name, retailer_product_id=product_id, name=title,
            url=urljoin(self.base_url + "/", f"products/{handle}"), image_url=image,
            price=price, original_price=compare if compare and compare > price else None,
            compare_at_price=compare, currency=self.currency, availability=availability,
            product_type=product_type, preorder=preorder, variant_id=variant_id,
            sku=str(sku).strip() if sku not in (None, "") else None,
            barcode=str(barcode).strip() if barcode not in (None, "") else None,
            vendor=vendor.strip() if isinstance(vendor, str) and vendor.strip() else None,
            tags=tags, listing_published_at=published, release=release,
            franchise=self._franchise(raw, tags),
            exclusive=explicit_exclusive,
            exclusive_retailer=self.retailer_name if retailer_exclusive else None,
            exclusive_region=exclusive_region,
            estimated_ship_date=ship_date, estimated_arrival_date=arrival_date,
            estimated_arrival_text=eta_text, collections=ordered_collections,
            sale="sale" in signals or bool(compare and compare > price),
            clearance="clearance" in signals, collection_type="RARE" if rare else None,
            last_chance="last_chance" in signals, limited_edition=limited_edition,
            limited_release=limited_release, vaulted=vaulted,
            exclusivity_text=(evidence if explicit_exclusive else None),
        )

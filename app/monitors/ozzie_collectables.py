"""Ozzie Collectables Australia adapter for its public Shopify catalogue."""

from __future__ import annotations

from collections import deque
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

BASE_URL = "https://www.ozziecollectables.com"
COLLECTION_HANDLE = "loungefly"
PAGE_SIZE = 250
MAX_PAGES = 10
DETAIL_BATCH_SIZE = 20


class OzzieCollectablesParseError(ValueError):
    """A required part of Ozzie's public Shopify representation was malformed."""


class OzzieCollectablesMonitor(RetailerMonitor):
    """Monitor every Loungefly bag category while excluding small accessories."""

    def __init__(self, http: AsyncHttpClient, *, base_url: str = BASE_URL,
                 detail_batch_size: int = DETAIL_BATCH_SIZE) -> None:
        if detail_batch_size < 0:
            raise ValueError("detail_batch_size cannot be negative")
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.detail_batch_size = detail_batch_size
        self._signatures: dict[str, tuple[object, ...]] = {}
        self._details: dict[str, Product] = {}
        self._pending: deque[str] = deque()
        self._queued: set[str] = set()

    async def discover_products(self) -> list[Product]:
        summaries: dict[str, Product] = {}
        handles: dict[str, str] = {}
        signatures: dict[str, tuple[object, ...]] = {}
        for page in range(1, MAX_PAGES + 1):
            payload = await self.http.get_json(
                f"{self.base_url}/collections/{COLLECTION_HANDLE}/products.json"
                f"?limit={PAGE_SIZE}&page={page}"
            )
            batch = self._product_list(payload)
            for raw in batch:
                product_type = self.classify_product_type(raw)
                if product_type is None or not self._is_loungefly(raw):
                    continue
                product = self.parse_product(raw, product_type=product_type)
                handle = self._handle(raw)
                key = product.retailer_product_id
                summaries[key], handles[key] = product, handle
                signatures[key] = self._signature(raw)
                if self._signatures.get(key) != signatures[key] and key not in self._queued:
                    self._pending.append(key)
                    self._queued.add(key)
            if len(batch) < PAGE_SIZE:
                break
        else:
            raise OzzieCollectablesParseError("Ozzie collection exceeded pagination safety limit")

        # Collection JSON performs the cheap complete scan. A modest rotating batch
        # of product.js requests enriches only new or changed records with barcode
        # and exact inventory, avoiding a full detail-page crawl every 15 minutes.
        for _ in range(min(self.detail_batch_size, len(self._pending))):
            key = self._pending.popleft()
            self._queued.discard(key)
            if key not in summaries:
                continue
            try:
                raw = await self.http.get_json(
                    f"{self.base_url}/products/{quote(handles[key])}.js"
                )
                product_type = self.classify_product_type(raw)
                if product_type is None or not self._is_loungefly(raw):
                    self._details.pop(key, None)
                    continue
                detail = self.parse_product(raw, product_type=product_type)
                if detail.retailer_product_id != key:
                    raise OzzieCollectablesParseError("Ozzie SKU changed between feeds")
                self._details[key] = detail
            except (HttpClientError, OzzieCollectablesParseError, ValueError, TypeError):
                # The complete collection observation remains usable; retry detail
                # enrichment on a later scan without manufacturing an inventory state.
                self._pending.append(key)
                self._queued.add(key)
        self._signatures = signatures
        live_keys = set(summaries)
        self._details = {key: value for key, value in self._details.items() if key in live_keys}
        return [self._details.get(key, summary) for key, summary in summaries.items()]

    async def check_product(self, product: Product) -> Product:
        handle = product.url.rstrip("/").rsplit("/", 1)[-1]
        try:
            if not handle:
                raise OzzieCollectablesParseError("Canonical product handle is missing")
            raw = await self.http.get_json(f"{self.base_url}/products/{quote(handle)}.js")
            product_type = self.classify_product_type(raw)
            if product_type is None or not self._is_loungefly(raw):
                raise OzzieCollectablesParseError("Product is no longer an enabled Loungefly bag")
            checked = self.parse_product(raw, product_type=product_type)
            if checked.retailer_product_id != product.retailer_product_id:
                raise OzzieCollectablesParseError("Ozzie SKU changed")
            return checked
        except (HttpClientError, OzzieCollectablesParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            self._product_list(await self.http.get_json(
                f"{self.base_url}/collections/{COLLECTION_HANDLE}/products.json?limit=1&page=1"
            ))
            return True
        except (HttpClientError, OzzieCollectablesParseError):
            return False

    @staticmethod
    def _product_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise OzzieCollectablesParseError("Ozzie response has no product list")
        products = payload["products"]
        if not all(isinstance(item, dict) for item in products):
            raise OzzieCollectablesParseError("Ozzie product list contains invalid entries")
        return products

    @staticmethod
    def _tags(raw: dict[str, Any]) -> tuple[str, ...]:
        value = raw.get("tags", [])
        if isinstance(value, str):
            return tuple(tag.strip() for tag in value.split(",") if tag.strip())
        if isinstance(value, list) and all(isinstance(tag, str) for tag in value):
            return tuple(tag.strip() for tag in value if tag.strip())
        return ()

    @classmethod
    def _is_loungefly(cls, raw: dict[str, Any]) -> bool:
        tags = {tag.casefold() for tag in cls._tags(raw)}
        vendor = str(raw.get("vendor") or "").casefold()
        description = cls._plain_text(raw.get("description") or raw.get("body_html") or "")
        return vendor == "loungefly" or "loungefly" in tags or bool(
            re.search(r"\bloungefly\b", description, re.I)
        )

    @classmethod
    def classify_product_type(cls, raw: object) -> str | None:
        if not isinstance(raw, dict):
            return None
        title = str(raw.get("title") or "")
        tags = " ".join(cls._tags(raw))
        structured = f"{raw.get('product_type') or raw.get('type') or ''} {tags}"
        description = cls._plain_text(raw.get("description") or raw.get("body_html") or "")
        all_evidence = " ".join((structured, title, description))
        # Accessory category/tag evidence and explicit charm/keychain nouns outrank
        # the decorative phrase "mini backpack" used for miniature charms.
        accessory_category = re.search(
            r"\b(?:bag accessories|wallets?|purses?|pins?\s*(?:&|and)?\s*badges?|"
            r"headwear|apparel|card holders?)\b", structured, re.I
        )
        keychain = re.search(r"\b(?:key\s*chains?|keychains?)\b", all_evidence, re.I)
        charm = re.search(r"\b(?:bag charms?|mini bag charms?)\b", all_evidence, re.I)
        bag_with_charm = re.search(r"\bbackpack\s+with\b[^.]{0,60}\bbag charm\b", title, re.I)
        if accessory_category or keychain or (charm and not bag_with_charm):
            return None
        categories = (
            (r"\bmini\s+backpacks?\b", "Mini Backpack"),
            (r"\bmid[ -]?size(?:d)?\s+backpacks?\b", "Mid Size Backpack"),
            (r"\bfull[ -]?size(?:d)?\s+backpacks?\b", "Full Size Backpack"),
            (r"\bconvertible\s+(?:tote\s+)?bags?\b", "Convertible Bag"),
            (r"\bcrossbody(?:\s+bags?)?\b", "Crossbody"),
            (r"\bshoulder\s+bags?\b", "Shoulder Bag"),
            (r"\bmessenger\s+bags?\b", "Messenger Bag"),
            (r"\bhandbags?\b", "Handbag"),
            (r"\bsatchels?(?:\s+bags?)?\b", "Satchel"),
            (r"\bbucket\s+bags?\b", "Bucket Bag"),
            (r"\bdrawstring\s+bags?\b", "Drawstring Bag"),
            (r"\bsling\s+bags?\b", "Sling Bag"),
            (r"\bduff[el]{2}\s+bags?\b", "Duffle Bag"),
            (r"\btotes?(?:\s+bags?)?\b", "Tote"),
            (r"\bbackpacks?\b", "Backpack"),
        )
        # A precise title (for example "Shoulder Bag") outranks a broader
        # merchandising tag (the store files shoulder bags under CROSSBODY BAGS).
        for source in (title, " ".join((structured, description))):
            for pattern, result in categories:
                if re.search(pattern, source, re.I):
                    return result
        if re.search(r"\bother baggage items?\b", structured, re.I) and re.search(
            r"\bbag\b", title, re.I
        ):
            return "Other Bag"
        return None

    @staticmethod
    def _plain_text(value: object) -> str:
        return " ".join(unescape(re.sub(r"<[^>]+>", " ", str(value))).split())

    @staticmethod
    def _handle(raw: dict[str, Any]) -> str:
        handle = raw.get("handle")
        if not isinstance(handle, str) or not handle.strip():
            raise OzzieCollectablesParseError("Shopify product handle is missing")
        return handle.strip()

    @classmethod
    def _signature(cls, raw: dict[str, Any]) -> tuple[object, ...]:
        variants = raw.get("variants")
        variant_values = tuple(
            (item.get("id"), item.get("sku"), item.get("price"), item.get("compare_at_price"),
             item.get("available"), item.get("barcode"))
            for item in variants if isinstance(item, dict)
        ) if isinstance(variants, list) else ()
        return (raw.get("title"), raw.get("vendor"), raw.get("type"), raw.get("product_type"),
                cls._tags(raw), raw.get("description"), raw.get("body_html"), variant_values)

    @staticmethod
    def _money(value: object, *, cents: bool) -> Decimal:
        try:
            amount = Decimal(str(value))
            if cents:
                amount /= Decimal("100")
        except (InvalidOperation, TypeError) as exc:
            raise OzzieCollectablesParseError("Shopify variant price is invalid") from exc
        if not amount.is_finite() or amount < 0:
            raise OzzieCollectablesParseError("Shopify variant price is invalid")
        return amount

    @staticmethod
    def _eta(text: str) -> tuple[date | None, str | None]:
        match = re.search(
            r"\b(?:ETA|Estimated Time of Arrival)\s*:?\s*(Date TBA|TBA|"
            r"(?:January|February|March|April|May|June|July|August|September|October|November|"
            r"December)\s+\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{4})\b", text, re.I
        )
        if not match:
            return None, None
        value = " ".join(match.group(1).split())
        for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
            try:
                parsed = datetime.strptime(value, fmt).date()
                return parsed, parsed.strftime("%-d %B %Y")
            except ValueError:
                pass
        if value.casefold() in {"tba", "date tba"}:
            return None, "Date TBA"
        try:
            parsed = datetime.strptime(value.title(), "%B %Y")
        except ValueError:
            return None, None
        # Month-only ETA remains text; no synthetic day is stored as a date.
        return None, parsed.strftime("%B %Y")

    def parse_product(self, raw: object, *, product_type: str | None = None) -> Product:
        if not isinstance(raw, dict):
            raise OzzieCollectablesParseError("Product is not an object")
        try:
            title = raw["title"].strip()
            variants = raw["variants"]
            handle = self._handle(raw)
        except (KeyError, AttributeError) as exc:
            raise OzzieCollectablesParseError("Product identity is incomplete") from exc
        if not title or not isinstance(variants, list) or not variants:
            raise OzzieCollectablesParseError("Product identity or variants are incomplete")
        if not all(isinstance(item, dict) and isinstance(item.get("available"), bool)
                   for item in variants):
            raise OzzieCollectablesParseError("Variant availability is missing")
        selected = next((item for item in variants if item["available"]), variants[0])
        sku = str(selected.get("sku") or "").strip()
        shopify_id = str(raw.get("id") or "").strip()
        variant_id = str(selected.get("id") or "").strip()
        if not (sku or shopify_id) or not variant_id:
            raise OzzieCollectablesParseError("Product SKU/Shopify identity is missing")
        cents = isinstance(selected.get("price"), int)
        price = self._money(selected.get("price"), cents=cents)
        compare_value = selected.get("compare_at_price")
        compare = None if compare_value in (None, "") else self._money(compare_value, cents=cents)
        original_price = compare if compare is not None and compare > price else None
        tags = self._tags(raw)
        description = self._plain_text(raw.get("description") or raw.get("body_html") or "")
        evidence = " ".join((title, description, *tags))
        normalized_tags = {tag.casefold().strip() for tag in tags}
        explicit_out = bool(re.search(r"\b(?:out of stock|sold out|pre[ -]?sold out)\b", evidence, re.I))
        preorder = "preorder" in normalized_tags or bool(
            re.search(r"\bpre[ -]?orders?\b", " ".join((title, description)), re.I)
        )
        backorder = "backorder" in normalized_tags or bool(
            re.search(r"\bback[ -]?orders?\b", " ".join((title, description)), re.I)
        )
        coming_soon = bool(re.search(r"\bcoming soon\b", evidence, re.I))
        inventory = selected.get("inventory_quantity")
        if explicit_out:
            availability = Availability.OUT_OF_STOCK
        elif preorder:
            availability = Availability.PREORDER
        elif backorder:
            availability = Availability.BACKORDER
        elif coming_soon:
            availability = Availability.COMING_SOON
        elif not any(item["available"] for item in variants):
            availability = Availability.OUT_OF_STOCK
        elif isinstance(inventory, int) and 0 < inventory <= 3:
            availability = Availability.LOW_STOCK
        else:
            availability = Availability.IN_STOCK
        eta_date, eta_text = self._eta(description)
        # The release parser requires explicit release/launch wording; ETA is not
        # among its prefixes and therefore cannot fabricate official release data.
        release = parse_release_text(
            description, source="Ozzie Collectables Shopify product data",
            local_timezone="Australia/Melbourne", date_order="DMY",
        )
        exclusive = bool(re.search(r"\bexclusive\b", evidence, re.I))
        retailer_exclusive = bool(re.search(r"\bozzie collectables exclusive\b", evidence, re.I))
        region_match = re.search(r"\b(US|UK|EU|AU|Australia) exclusive\b", evidence, re.I)
        exclusive_region = region_match.group(1).upper() if region_match else None
        if exclusive_region == "AUSTRALIA":
            exclusive_region = "AU"
        image = raw.get("featured_image")
        if isinstance(image, dict):
            image = image.get("src")
        images = raw.get("images")
        if not image and isinstance(images, list) and images:
            image = images[0].get("src") if isinstance(images[0], dict) else images[0]
        if isinstance(image, str) and image.startswith("//"):
            image = "https:" + image
        if image is not None and not isinstance(image, str):
            raise OzzieCollectablesParseError("Product image is invalid")
        published = None
        if raw.get("published_at"):
            try:
                published = datetime.fromisoformat(str(raw["published_at"]).replace("Z", "+00:00"))
            except ValueError as exc:
                raise OzzieCollectablesParseError("Publication timestamp is invalid") from exc
            if published.tzinfo is None:
                raise OzzieCollectablesParseError("Publication timestamp has no timezone")
        vendor = raw.get("vendor")
        barcode = selected.get("barcode")
        return Product(
            retailer="Ozzie Collectables", retailer_product_id=sku or shopify_id,
            name=title, url=urljoin(self.base_url + "/", f"products/{handle}"),
            image_url=image, price=price, original_price=original_price, currency="AUD",
            availability=availability, product_type=product_type or self.classify_product_type(raw) or "Other Bag",
            franchise=(str(raw.get("product_type") or raw.get("type") or "").strip() or None),
            exclusive=exclusive, exclusive_retailer="Ozzie Collectables" if retailer_exclusive else None,
            exclusive_region=exclusive_region, preorder=preorder, sku=sku or None,
            variant_id=variant_id, barcode=str(barcode).strip() if barcode not in (None, "") else None,
            vendor=vendor.strip() if isinstance(vendor, str) and vendor.strip() else None,
            tags=tags, listing_published_at=published, release=release,
            estimated_arrival_date=eta_date, estimated_arrival_text=eta_text,
        )

"""LF Lovers adapter using the shop's public Shopify collection feed."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html import unescape
import re
from typing import Any
from urllib.parse import quote, urljoin

from app.http import AsyncHttpClient, HttpClientError
from app.models import Availability, Product, ReleaseInfo, ReleasePrecision
from app.monitors.base import RetailerMonitor
from app.release import MONTHS, MONTH_PATTERN, parse_release_text

BASE_URL = "https://www.lflovers.com"
COLLECTION = "backpacks"
PAGE_SIZE = 250
MAX_PAGES = 10


class LFLoversParseError(ValueError):
    """LF Lovers returned incomplete or malformed commerce data."""


class LFLoversMonitor(RetailerMonitor):
    """Monitor only Loungefly mini backpacks in LF Lovers' backpack collection."""

    def __init__(self, http: AsyncHttpClient, *, base_url: str = BASE_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def discover_products(self) -> list[Product]:
        found: dict[str, Product] = {}
        for page in range(1, MAX_PAGES + 1):
            payload = await self.http.get_json(
                f"{self.base_url}/collections/{COLLECTION}/products.json"
                f"?limit={PAGE_SIZE}&page={page}"
            )
            products = self._product_list(payload)
            for raw in products:
                if self._is_mini_backpack(raw):
                    product = self.parse_product(raw)
                    # Collection overlap/reordering cannot create duplicate observations.
                    found[product.retailer_product_id] = product
            if len(products) < PAGE_SIZE:
                break
        else:
            raise LFLoversParseError("LF Lovers collection exceeded pagination limit")
        return list(found.values())

    async def check_product(self, product: Product) -> Product:
        handle = product.url.rstrip("/").rsplit("/", 1)[-1]
        try:
            if not handle:
                raise LFLoversParseError("Product handle is missing")
            return self.parse_product(
                await self.http.get_json(f"{self.base_url}/products/{quote(handle)}.js")
            )
        except HttpClientError as exc:
            return replace(product, availability=(
                Availability.UNAVAILABLE if exc.status == 404 else Availability.ERROR
            ))
        except (LFLoversParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            self._product_list(await self.http.get_json(
                f"{self.base_url}/collections/{COLLECTION}/products.json?limit=1&page=1"
            ))
            return True
        except (HttpClientError, LFLoversParseError):
            return False

    @staticmethod
    def _product_list(payload: object) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise LFLoversParseError("LF Lovers response has no product list")
        if not all(isinstance(item, dict) for item in payload["products"]):
            raise LFLoversParseError("LF Lovers product list has invalid entries")
        return payload["products"]

    @staticmethod
    def _plain(value: object) -> str:
        if not isinstance(value, str):
            raise LFLoversParseError("Product description is invalid")
        return " ".join(unescape(re.sub(r"<[^>]*>", " ", value)).split())

    @classmethod
    def _is_mini_backpack(cls, raw: dict[str, Any]) -> bool:
        title, vendor, product_type = raw.get("title"), raw.get("vendor"), raw.get("product_type")
        tags = raw.get("tags", [])
        if (not isinstance(title, str) or not isinstance(vendor, str)
                or not isinstance(product_type, str) or not isinstance(tags, list)):
            return False
        text = title.casefold().replace("-", " ")
        excluded = ("keychain", "key chain", "charm", "wallet", "crossbody", "tote")
        brand_metadata = " ".join((vendor, product_type, *(str(tag) for tag in tags))).casefold()
        return ("loungefly" in brand_metadata and "mini backpack" in text
                and not any(term in text for term in excluded))

    @staticmethod
    def _estimated_date(text: str, concept: str) -> date | None:
        if concept == "arrival":
            prefix = r"(?:estimated|expected|due)(?:\s+to)?\s+arriv(?:al|e)"
        else:
            prefix = r"(?:estimated|expected)?\s*(?:to\s+)?dispatch(?:ed)?"
        match = re.search(
            rf"\b{prefix}\s*(?::|-|on)?\s*(\d{{1,2}})(?:st|nd|rd|th)?\s+"
            rf"({MONTH_PATTERN})\s+(\d{{4}})\b", text, re.I,
        )
        if not match:
            return None
        try:
            return date(int(match.group(3)), MONTHS[match.group(2).casefold()], int(match.group(1)))
        except ValueError:
            return None

    def parse_product(self, raw: object) -> Product:
        if not isinstance(raw, dict):
            raise LFLoversParseError("Product is not an object")
        try:
            product_id = str(raw["id"]).strip()
            title = raw["title"].strip()
            handle = raw["handle"].strip()
            variants = raw["variants"]
        except (KeyError, AttributeError) as exc:
            raise LFLoversParseError("Product identity is incomplete") from exc
        if not all((product_id, title, handle)) or not isinstance(variants, list) or not variants:
            raise LFLoversParseError("Product identity or variants are incomplete")
        if not all(isinstance(v, dict) and isinstance(v.get("available"), bool) for v in variants):
            raise LFLoversParseError("Variant availability is missing")
        selected = next((v for v in variants if v["available"]), variants[0])
        variant_id = str(selected.get("id", "")).strip()
        if not variant_id:
            raise LFLoversParseError("Variant identity is missing")
        try:
            price = Decimal(str(selected["price"]))
            compare_raw = selected.get("compare_at_price")
            original_price = None if compare_raw in (None, "") else Decimal(str(compare_raw))
            if (not price.is_finite() or price < 0 or
                    (original_price is not None and (not original_price.is_finite() or original_price < 0))):
                raise InvalidOperation
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise LFLoversParseError("Variant pricing is invalid") from exc

        description = self._plain(raw.get("body_html", "") or "")
        tags_raw = raw.get("tags", [])
        if not isinstance(tags_raw, list) or not all(isinstance(tag, str) for tag in tags_raw):
            raise LFLoversParseError("Product tags are invalid")
        tags = tuple(tag.strip() for tag in tags_raw if tag.strip())
        evidence = " ".join((title, description, *tags))
        preorder = bool(re.search(r"\bpre[ -]?order(?:ed|ing)?\b", evidence, re.I))
        availability = (Availability.PREORDER if preorder else Availability.IN_STOCK
                        if any(v["available"] for v in variants) else Availability.OUT_OF_STOCK)

        release = parse_release_text(
            description, source="LF Lovers Shopify product data",
            local_timezone="Europe/London", date_order="DMY",
        )
        if release is None:
            monthly_pattern = re.compile(
                r"(jan|feb|mar|apr|may|jun|june|jul|july|aug|sep|sept|oct|nov|dec)(\d{2})",
                re.I,
            )
            tag_match = next((match for tag in tags if (match := monthly_pattern.fullmatch(tag))), None)
            if tag_match:
                aliases = {name[:3].casefold(): number for name, number in MONTHS.items()}
                aliases.update({"june": 6, "july": 7, "sept": 9})
                release = ReleaseInfo(
                    ReleasePrecision.MONTH_ONLY, text=tag_match.group(0),
                    source="LF Lovers monthly collection tag",
                    release_month=aliases[tag_match.group(1).casefold()],
                    release_year=2000 + int(tag_match.group(2)),
                )

        published = raw.get("published_at")
        try:
            published_at = (datetime.fromisoformat(str(published).replace("Z", "+00:00"))
                            if published is not None else None)
        except ValueError as exc:
            raise LFLoversParseError("Product publication timestamp is invalid") from exc
        if published_at is not None and published_at.tzinfo is None:
            raise LFLoversParseError("Product publication timestamp needs a timezone")
        images = raw.get("images", [])
        if not isinstance(images, list):
            raise LFLoversParseError("Product images are invalid")
        image = images[0].get("src") if images and isinstance(images[0], dict) else None
        if isinstance(image, str) and image.startswith("//"):
            image = "https:" + image
        if image is not None and not isinstance(image, str):
            raise LFLoversParseError("Product image is invalid")
        lowered_tags = {tag.casefold() for tag in tags}
        exclusive = any("exclusive" in tag for tag in lowered_tags) or bool(
            re.search(r"\b(?:LF Lovers|UK|Europe|EMEA)\s+exclusive\b", evidence, re.I)
        )
        return Product(
            retailer="LF Lovers", retailer_product_id=product_id, variant_id=variant_id,
            sku=str(selected["sku"]).strip() if selected.get("sku") not in (None, "") else None,
            barcode=str(selected["barcode"]).strip() if selected.get("barcode") not in (None, "") else None,
            name=title, url=urljoin(self.base_url + "/", f"products/{handle}"), image_url=image,
            price=price, original_price=original_price, currency="GBP", availability=availability,
            vendor=str(raw.get("vendor", "")).strip() or None, product_type="Mini Backpack", tags=tags,
            listing_published_at=published_at, preorder=preorder,
            new_release=bool(lowered_tags & {"new", "lfnew", "loungefly new"}),
            exclusive=exclusive, exclusive_retailer="LF Lovers" if exclusive else None,
            release=release,
            estimated_arrival_date=self._estimated_date(description, "arrival"),
            estimated_dispatch_date=self._estimated_date(description, "dispatch"),
        )

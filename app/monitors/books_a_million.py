"""Books-A-Million US availability status and anti-bot-safe monitor boundary."""

from __future__ import annotations

from dataclasses import replace

from app.http import AsyncHttpClient, HttpClientError
from app.models import Availability, Product
from app.monitors.base import RetailerMonitor

BASE_URL = "https://www.booksamillion.com"
SEARCH_URL = f"{BASE_URL}/search?query=loungefly"


class BooksAMillionParseError(ValueError):
    """Books-A-Million did not return a verifiable retailer product representation."""


class BooksAMillionMonitor(RetailerMonitor):
    """Fail closed while BAM's public pages require a Cloudflare browser challenge.

    The project has no approved isolated browser subsystem. Treating challenge HTML as an
    empty category would incorrectly remove every saved product, so this adapter deliberately
    raises and lets MonitorService preserve state and retailer health.
    """

    def __init__(self, http: AsyncHttpClient, *, search_url: str = SEARCH_URL) -> None:
        self.http = http
        self.search_url = search_url

    async def discover_products(self) -> list[Product]:
        return self.parse_listing(await self.http.get_text(self.search_url))

    async def check_product(self, product: Product) -> Product:
        try:
            self.parse_listing(await self.http.get_text(product.url))
        except (HttpClientError, BooksAMillionParseError, ValueError, TypeError):
            return replace(product, availability=Availability.ERROR)
        # No product-page contract has been verified, so never turn an unrecognized response
        # into inventory evidence.
        return replace(product, availability=Availability.ERROR)

    async def health_check(self) -> bool:
        try:
            self.parse_listing(await self.http.get_text(self.search_url))
        except (HttpClientError, BooksAMillionParseError):
            return False
        return True

    @staticmethod
    def parse_listing(html: object) -> list[Product]:
        if not isinstance(html, str) or not html.strip():
            raise BooksAMillionParseError("Books-A-Million response was empty")
        normalized = html.casefold()
        if ("challenges.cloudflare.com" in normalized or "cf-mitigated" in normalized
                or "<title>just a moment...</title>" in normalized):
            raise BooksAMillionParseError(
                "Books-A-Million returned a Cloudflare browser challenge; monitoring is blocked"
            )
        raise BooksAMillionParseError(
            "Books-A-Million product markup has not been verified; refusing to infer products"
        )

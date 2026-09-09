"""Optional Playwright lifecycle isolated from HTTP retailer adapters."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.config import BrowserConfig

LOGGER = logging.getLogger("monitor.browser")


class PageStatus(StrEnum):
    PAGE_LOADED = "PAGE_LOADED"
    CLOUDFLARE_CHALLENGE = "CLOUDFLARE_CHALLENGE"
    ACCESS_DENIED = "ACCESS_DENIED"
    NAVIGATION_ERROR = "NAVIGATION_ERROR"
    PARSER_ERROR = "PARSER_ERROR"


@dataclass(frozen=True, slots=True)
class BrowserPage:
    status: PageStatus
    url: str
    html: str = ""
    http_status: int | None = None
    detail: str | None = None


def classify_page(html: str, *, http_status: int | None = None) -> PageStatus:
    """Classify explicit denial/challenge pages before any stock parsing."""
    text = html.casefold()
    cloudflare_markers = (
        "cf-chl-", "challenge-platform", "just a moment...", "checking your browser",
        "verify you are human", "cloudflare ray id",
    )
    if any(marker in text for marker in cloudflare_markers):
        return PageStatus.CLOUDFLARE_CHALLENGE
    denied_markers = ("access denied", "request blocked", "you don't have permission to access")
    if http_status in {401, 403, 451} or any(marker in text for marker in denied_markers):
        return PageStatus.ACCESS_DENIED
    return PageStatus.PAGE_LOADED


class BrowserService:
    """Own one Chromium process and permit a bounded number of short-lived pages."""

    def __init__(self, config: BrowserConfig) -> None:
        self.config = config
        self._playwright: Any = None
        self._browser: Any = None
        self._lock = asyncio.Lock()
        self._pages = asyncio.Semaphore(config.max_concurrent_pages)

    @property
    def available(self) -> bool:
        return self._browser is not None

    async def start(self) -> bool:
        if not self.config.enabled:
            return False
        async with self._lock:
            if self._browser is not None:
                return True
            try:
                # Delayed import keeps Playwright optional for HTTP-only deployments.
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(headless=True)
                return True
            except Exception:
                LOGGER.exception("Chromium startup failed; browser retailers will remain disabled")
                if self._playwright is not None:
                    await self._playwright.stop()
                self._playwright = self._browser = None
                return False

    async def fetch(self, url: str) -> BrowserPage:
        if self._browser is None:
            return BrowserPage(PageStatus.NAVIGATION_ERROR, url, detail="browser is unavailable")
        async with self._pages:
            context = None
            try:
                context = await self._browser.new_context()
                page = await context.new_page()
                page.set_default_navigation_timeout(self.config.navigation_timeout_seconds * 1000)
                response = await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_timeout(1500)
                html = await page.content()
                status_code = response.status if response is not None else None
                return BrowserPage(classify_page(html, http_status=status_code), page.url, html, status_code)
            except Exception as exc:
                LOGGER.warning("Browser navigation failed", extra={"url": url, "error": str(exc)})
                return BrowserPage(PageStatus.NAVIGATION_ERROR, url, detail=str(exc))
            finally:
                if context is not None:
                    await context.close()

    async def close(self) -> None:
        async with self._lock:
            browser, playwright = self._browser, self._playwright
            self._browser = self._playwright = None
            if browser is not None:
                await browser.close()
            if playwright is not None:
                await playwright.stop()

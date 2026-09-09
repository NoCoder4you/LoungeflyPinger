import pytest

from app.browser import BrowserService, PageStatus
from app.config import BrowserConfig


@pytest.mark.asyncio
async def test_browser_startup_failure_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    class Startup:
        async def start(self): raise RuntimeError("chromium missing")
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: Startup())
    service = BrowserService(BrowserConfig(enabled=True))
    assert await service.start() is False
    assert service.available is False


@pytest.mark.asyncio
async def test_browser_timeout_is_navigation_error() -> None:
    class Page:
        def set_default_navigation_timeout(self, _timeout): pass
        async def goto(self, *_args, **_kwargs): raise TimeoutError("timed out")
    class Context:
        async def new_page(self): return Page()
        async def close(self): self.closed = True
    class Chromium:
        async def new_context(self): return Context()
    service = BrowserService(BrowserConfig(enabled=True))
    service._browser = Chromium()
    result = await service.fetch("https://hmv.com")
    assert result.status == PageStatus.NAVIGATION_ERROR
    assert "timed out" in result.detail

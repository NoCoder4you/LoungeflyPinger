from aiohttp import web
import pytest

from app.http import AsyncHttpClient


@pytest.mark.asyncio
async def test_http_client_can_restart_after_close() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.Response(text="available")

    application = web.Application()
    application.router.add_get("/product", handler)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/product"
    client = AsyncHttpClient(
        timeout_seconds=1,
        concurrency_limit=1,
        user_agent="LoungeflyMonitor/Test",
        max_retries=1,
    )

    try:
        assert await client.get_text(url) == "available"
        await client.close()

        assert await client.get_text(url) == "available"
    finally:
        await client.close()
        await runner.cleanup()

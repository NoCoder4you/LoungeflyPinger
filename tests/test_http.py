import asyncio

from aiohttp import web
import pytest

from app.http import AsyncHttpClient, HttpClientError, HttpErrorKind


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


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "kind", "requests"), [
    (403, HttpErrorKind.FORBIDDEN, 1),
    (429, HttpErrorKind.RATE_LIMITED, 2),
    (500, HttpErrorKind.SERVER, 2),
])
async def test_http_failures_are_classified_and_temporary_errors_retry(status, kind, requests):
    calls = 0
    async def handler(_: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        return web.Response(status=status, headers={"Retry-After": "0"})
    application = web.Application()
    application.router.add_get("/failure", handler)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = AsyncHttpClient(timeout_seconds=1, concurrency_limit=1, user_agent="test",
                             max_retries=1, rate_limit_requests_per_second=1000)
    try:
        with pytest.raises(HttpClientError) as caught:
            await client.get_text(f"http://127.0.0.1:{port}/failure")
        assert caught.value.kind == kind
        assert calls == requests
    finally:
        await client.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_http_timeout_retries_then_reports_network_failure():
    async def handler(_: web.Request) -> web.Response:
        await asyncio.sleep(.05)
        return web.Response(text="late")
    application = web.Application()
    application.router.add_get("/slow", handler)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = AsyncHttpClient(timeout_seconds=.01, concurrency_limit=1, user_agent="test",
                             max_retries=1, backoff_seconds=.001,
                             rate_limit_requests_per_second=1000)
    try:
        with pytest.raises(HttpClientError) as caught:
            await client.get_text(f"http://127.0.0.1:{port}/slow")
        assert caught.value.kind == HttpErrorKind.NETWORK
    finally:
        await client.close()
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("chunked", [False, True])
async def test_http_rejects_oversized_responses(chunked: bool) -> None:
    async def handler(_: web.Request) -> web.StreamResponse:
        if not chunked:
            return web.Response(body=b"x" * 33)
        response = web.StreamResponse()
        await response.prepare(_)
        await response.write(b"x" * 16)
        await response.write(b"y" * 17)
        await response.write_eof()
        return response

    application = web.Application()
    application.router.add_get("/large", handler)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    client = AsyncHttpClient(timeout_seconds=1, concurrency_limit=1, user_agent="test",
                             max_retries=0, rate_limit_requests_per_second=1000,
                             max_response_bytes=32)
    try:
        with pytest.raises(HttpClientError) as caught:
            await client.get_text(f"http://127.0.0.1:{port}/large")
        assert caught.value.kind is HttpErrorKind.RESPONSE_TOO_LARGE
    finally:
        await client.close()
        await runner.cleanup()

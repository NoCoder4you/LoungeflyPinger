"""Bounded, retrying asynchronous HTTP transport."""

import asyncio
from enum import StrEnum

import aiohttp


class HttpErrorKind(StrEnum):
    NETWORK = "network_failure"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    SERVER = "server_error"
    CLIENT = "client_error"
    PARSER = "parser_failure"


class HttpClientError(RuntimeError):
    def __init__(self, kind: HttpErrorKind, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


class AsyncHttpClient:
    def __init__(self, *, timeout_seconds: float, concurrency_limit: int, user_agent: str, max_retries: int) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._concurrency_limit = concurrency_limit
        # Use the conventional general-purpose request accept value. Individual
        # parsers still validate the returned representation before trusting it.
        self._headers = {"User-Agent": user_agent, "Accept": "*/*"}
        self._semaphore = asyncio.Semaphore(concurrency_limit)
        self._max_retries = max_retries
        self._connector: aiohttp.TCPConnector | None = None
        self._session: aiohttp.ClientSession | None = None
        self._lifecycle_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._session is not None and not self._session.closed:
                return
            # ClientSession owns and closes its connector. Always create these as
            # a pair so restarting the client never reuses a closed connector.
            self._connector = aiohttp.TCPConnector(limit=self._concurrency_limit, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                connector=self._connector,
                headers=self._headers,
                trust_env=True,
            )

    async def get_text(self, url: str) -> str:
        await self.start()
        assert self._session is not None
        for attempt in range(self._max_retries + 1):
            try:
                async with self._semaphore, self._session.get(url) as response:
                    if response.status == 404:
                        raise HttpClientError(HttpErrorKind.NOT_FOUND, "Resource not found", 404)
                    if response.status == 403:
                        raise HttpClientError(HttpErrorKind.FORBIDDEN, "Request forbidden", 403)
                    if response.status == 429 or response.status >= 500:
                        if attempt < self._max_retries:
                            await asyncio.sleep(min(2**attempt, 30))
                            continue
                        kind = HttpErrorKind.RATE_LIMITED if response.status == 429 else HttpErrorKind.SERVER
                        raise HttpClientError(kind, "Temporary HTTP error; retry limit reached", response.status)
                    if response.status >= 400:
                        raise HttpClientError(HttpErrorKind.CLIENT, "HTTP client error", response.status)
                    return await response.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt < self._max_retries:
                    await asyncio.sleep(min(2**attempt, 30))
                    continue
                raise HttpClientError(HttpErrorKind.NETWORK, "Network request failed") from exc
        raise AssertionError("unreachable")

    async def get_json(self, url: str) -> object:
        text = await self.get_text(url)
        try:
            import json
            return json.loads(text)
        except (ValueError, TypeError) as exc:
            raise HttpClientError(HttpErrorKind.PARSER, "Response was not valid JSON") from exc

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._session is not None:
                await self._session.close()
            self._session = None
            self._connector = None

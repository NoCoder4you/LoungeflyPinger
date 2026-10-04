"""Bounded, retrying asynchronous HTTP transport with per-host isolation."""

from __future__ import annotations

import asyncio
import contextvars
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from urllib.parse import urlsplit

import aiohttp


class HttpErrorKind(StrEnum):
    TIMEOUT = "timeout"
    DNS = "dns_failure"
    CONNECTION = "connection_failure"
    TLS = "tls_failure"
    NETWORK = "network_failure"  # retained for API compatibility
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    SERVER = "server_error"
    CLIENT = "client_error"
    PARSER = "parser_failure"
    RESPONSE_TOO_LARGE = "response_too_large"


class HttpClientError(RuntimeError):
    """A sanitized, typed transport failure safe to persist and log."""

    def __init__(self, kind: HttpErrorKind, message: str, status: int | None = None, *,
                 hostname: str | None = None, method: str = "GET", attempts: int = 1,
                 retry_after_supplied: bool = False, duration: float | None = None,
                 content_type: str | None = None) -> None:
        super().__init__(message)
        self.kind, self.status = kind, status
        self.hostname, self.method, self.attempts = hostname, method, attempts
        self.retry_after_supplied, self.duration = retry_after_supplied, duration
        self.content_type = content_type

    def diagnostic(self) -> str:
        parts = [f"category={self.kind.value}", f"method={self.method}"]
        if self.hostname: parts.append(f"host={self.hostname}")
        if self.status is not None: parts.append(f"status={self.status}")
        parts.extend((f"attempts={self.attempts}",
                      f"retry_after={'yes' if self.retry_after_supplied else 'no'}"))
        if self.duration is not None: parts.append(f"duration={self.duration:.3f}s")
        if self.content_type: parts.append(f"content_type={self.content_type[:80]}")
        return f"{self}; " + " ".join(parts)


@dataclass
class _HostState:
    semaphore: asyncio.Semaphore
    lock: asyncio.Lock
    next_request_at: float = 0.0


class AsyncHttpClient:
    def __init__(self, *, timeout_seconds: float, concurrency_limit: int, user_agent: str,
                 max_retries: int, backoff_seconds: float = 1,
                 rate_limit_requests_per_second: float = 5,
                 max_response_bytes: int = 10_485_760,
                 per_host_concurrency_limit: int = 2,
                 jitter: callable = random.random) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._concurrency_limit = concurrency_limit
        self._headers = {"User-Agent": user_agent, "Accept": "*/*"}
        self._semaphore = asyncio.Semaphore(concurrency_limit)
        self._max_retries, self._backoff_seconds = max_retries, backoff_seconds
        self._minimum_request_interval = 1 / rate_limit_requests_per_second
        self._max_response_bytes = max_response_bytes
        self._per_host_concurrency_limit = max(1, min(per_host_concurrency_limit, concurrency_limit))
        self._hosts: dict[str, _HostState] = {}
        self._hosts_lock = asyncio.Lock()
        self._jitter = jitter
        self._status: contextvars.ContextVar[int | None] = contextvars.ContextVar("http_response_status", default=None)
        self._last_error: contextvars.ContextVar[HttpClientError | None] = contextvars.ContextVar("http_last_error", default=None)
        self._connector: aiohttp.TCPConnector | None = None
        self._session: aiohttp.ClientSession | None = None
        self._lifecycle_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._session is not None and not self._session.closed: return
            self._connector = aiohttp.TCPConnector(limit=self._concurrency_limit, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(timeout=self._timeout, connector=self._connector,
                                                  headers=self._headers, trust_env=True)

    async def _host_state(self, hostname: str) -> _HostState:
        async with self._hosts_lock:
            return self._hosts.setdefault(hostname, _HostState(
                asyncio.Semaphore(self._per_host_concurrency_limit), asyncio.Lock()))

    async def get_text(self, url: str) -> str:
        await self.start()
        assert self._session is not None
        hostname = (urlsplit(url).hostname or "unknown").lower()
        host = await self._host_state(hostname)
        started, retry_after_seen = time.monotonic(), False
        for attempt in range(self._max_retries + 1):
            try:
                async with host.lock:
                    delay = host.next_request_at - time.monotonic()
                    if delay > 0: await asyncio.sleep(delay)
                    host.next_request_at = time.monotonic() + self._minimum_request_interval
                # Host semaphore is acquired before the global slot so one host can
                # never queue all workers behind its own slow requests.
                async with host.semaphore, self._semaphore, self._session.get(url) as response:
                    self._status.set(response.status)
                    content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0]
                    retry_after = response.headers.get("Retry-After")
                    retry_after_seen |= retry_after is not None
                    kind = self._status_kind(response.status)
                    if kind is not None:
                        if response.status == 429 or response.status in {500, 502, 503, 504}:
                            if attempt < self._max_retries:
                                await asyncio.sleep(self._retry_delay(retry_after, attempt))
                                continue
                        raise self._error(kind, self._status_message(kind), response.status,
                                          hostname, attempt + 1, retry_after_seen, started, content_type)
                    declared = response.content_length
                    if declared is not None and declared > self._max_response_bytes:
                        raise self._error(HttpErrorKind.RESPONSE_TOO_LARGE, "HTTP response exceeds size limit",
                                          response.status, hostname, attempt + 1, False, started, content_type)
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        body.extend(chunk)
                        if len(body) > self._max_response_bytes:
                            raise self._error(HttpErrorKind.RESPONSE_TOO_LARGE, "HTTP response exceeds size limit",
                                              response.status, hostname, attempt + 1, False, started, content_type)
                    try: return bytes(body).decode(response.charset or "utf-8")
                    except (LookupError, UnicodeDecodeError) as exc:
                        raise self._error(HttpErrorKind.PARSER, "Response text encoding is invalid",
                                          response.status, hostname, attempt + 1, False, started, content_type) from exc
            except HttpClientError as exc:
                self._last_error.set(exc)
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                kind = self._network_kind(exc)
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_delay(None, attempt))
                    continue
                error = self._error(kind, self._status_message(kind), None, hostname,
                                    attempt + 1, False, started, None)
                self._last_error.set(error)
                raise error from exc
        raise AssertionError("unreachable")

    @staticmethod
    def _status_kind(status: int) -> HttpErrorKind | None:
        if status < 400: return None
        if status == 403: return HttpErrorKind.FORBIDDEN
        if status == 404: return HttpErrorKind.NOT_FOUND
        if status == 429: return HttpErrorKind.RATE_LIMITED
        if status >= 500: return HttpErrorKind.SERVER
        return HttpErrorKind.CLIENT

    @staticmethod
    def _network_kind(exc: BaseException) -> HttpErrorKind:
        if isinstance(exc, asyncio.TimeoutError): return HttpErrorKind.TIMEOUT
        if isinstance(exc, aiohttp.ClientConnectorCertificateError) or isinstance(exc, aiohttp.ClientSSLError):
            return HttpErrorKind.TLS
        if isinstance(exc, aiohttp.ClientConnectorDNSError): return HttpErrorKind.DNS
        if isinstance(exc, aiohttp.ClientConnectorError): return HttpErrorKind.CONNECTION
        return HttpErrorKind.NETWORK

    @staticmethod
    def _status_message(kind: HttpErrorKind) -> str:
        return {HttpErrorKind.FORBIDDEN: "Request forbidden", HttpErrorKind.NOT_FOUND: "Resource not found",
                HttpErrorKind.RATE_LIMITED: "Rate limit retry exhausted", HttpErrorKind.SERVER: "Server retry exhausted",
                HttpErrorKind.TIMEOUT: "Request timed out", HttpErrorKind.DNS: "DNS lookup failed",
                HttpErrorKind.CONNECTION: "Connection failed", HttpErrorKind.TLS: "TLS connection failed",
                HttpErrorKind.NETWORK: "Network request failed"}.get(kind, "HTTP client error")

    @staticmethod
    def _error(kind, message, status, hostname, attempts, retry_after, started, content_type):
        return HttpClientError(kind, message, status, hostname=hostname, attempts=attempts,
                               retry_after_supplied=retry_after, duration=time.monotonic() - started,
                               content_type=content_type)

    @property
    def response_status(self) -> int | None: return self._status.get()

    @property
    def last_error(self) -> HttpClientError | None: return self._last_error.get()

    def reset_metrics(self) -> None:
        self._status.set(None); self._last_error.set(None)

    def _retry_delay(self, retry_after: str | None, attempt: int) -> float:
        if retry_after:
            try: return min(max(float(retry_after), 0), 300)
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(retry_after)
                    return min(max((parsed - datetime.now(UTC)).total_seconds(), 0), 300)
                except (TypeError, ValueError, OverflowError): pass
        base = min(self._backoff_seconds * 2**attempt, 30)
        return min(base * (0.5 + self._jitter()), 30)

    async def get_json(self, url: str) -> object:
        text = await self.get_text(url)
        try:
            import json
            return json.loads(text)
        except (ValueError, TypeError) as exc:
            hostname = (urlsplit(url).hostname or "unknown").lower()
            error = HttpClientError(HttpErrorKind.PARSER, "Response was not valid JSON",
                                    self.response_status, hostname=hostname)
            self._last_error.set(error)
            raise error from exc

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._session is not None: await self._session.close()
            self._session = self._connector = None

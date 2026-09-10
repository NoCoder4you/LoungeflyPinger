"""Small asynchronous interval scheduler with independent job timing."""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

LOGGER = logging.getLogger(__name__)


class Scheduler:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = asyncio.Event()

    def add_interval_job(
        self,
        name: str,
        callback: Callable[[], Awaitable[None]],
        interval_seconds: float,
        *,
        jitter_fraction: float = 0.0,
        timeout_seconds: float | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if self._stopping.is_set():
            raise RuntimeError("scheduler is stopping")
        if not 0 <= jitter_fraction <= 1:
            raise ValueError("jitter_fraction must be between zero and one")
        task = asyncio.create_task(
            self._run_job(name, callback, interval_seconds, jitter_fraction, timeout_seconds),
            name=f"scheduler:{name}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_job(
        self, name: str, callback: Callable[[], Awaitable[None]], interval: float,
        jitter_fraction: float, timeout_seconds: float | None,
    ) -> None:
        while not self._stopping.is_set():
            try:
                if timeout_seconds is None:
                    await callback()
                else:
                    await asyncio.wait_for(callback(), timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Scheduled job failed", extra={"job": name})
            try:
                jitter = random.uniform(-jitter_fraction, jitter_fraction) * interval
                await asyncio.wait_for(self._stopping.wait(), timeout=max(0.1, interval + jitter))
            except TimeoutError:
                pass

    async def stop(self) -> None:
        self._stopping.set()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

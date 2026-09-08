"""Small asynchronous interval scheduler with independent job timing."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

LOGGER = logging.getLogger(__name__)


class Scheduler:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = asyncio.Event()

    def add_interval_job(self, name: str, callback: Callable[[], Awaitable[None]], interval_seconds: float) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if self._stopping.is_set():
            raise RuntimeError("scheduler is stopping")
        task = asyncio.create_task(self._run_job(name, callback, interval_seconds), name=f"scheduler:{name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_job(self, name: str, callback: Callable[[], Awaitable[None]], interval: float) -> None:
        while not self._stopping.is_set():
            try:
                await callback()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Scheduled job failed", extra={"job": name})
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
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

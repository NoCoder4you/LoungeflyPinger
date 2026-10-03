"""Small asynchronous interval scheduler with independent job timing."""

import asyncio
import logging
import random
from dataclasses import dataclass
from collections.abc import Awaitable, Callable

LOGGER = logging.getLogger(__name__)

@dataclass
class _Job:
    task: asyncio.Task[None]
    running: bool = False
    active: bool = True


class Scheduler:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()
        self._jobs: dict[str, _Job] = {}
        self._stopping = asyncio.Event()

    def add_interval_job(
        self,
        name: str,
        callback: Callable[[], Awaitable[None]],
        interval_seconds: float,
        *,
        jitter_fraction: float = 0.0,
        timeout_seconds: float | None = None,
        initial_delay_seconds: float = 0.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if self._stopping.is_set():
            raise RuntimeError("scheduler is stopping")
        if not 0 <= jitter_fraction <= 1:
            raise ValueError("jitter_fraction must be between zero and one")
        if initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds must not be negative")
        if name in self._jobs:
            raise ValueError(f"scheduler job already exists: {name}")
        task = asyncio.create_task(
            self._run_job(
                name, callback, interval_seconds, jitter_fraction, timeout_seconds,
                initial_delay_seconds,
            ),
            name=f"scheduler:{name}",
        )
        self._tasks.add(task)
        job = _Job(task)
        self._jobs[name] = job
        def finished(done: asyncio.Task[None]) -> None:
            self._tasks.discard(done)
            if self._jobs.get(name) is job:
                self._jobs.pop(name, None)
        task.add_done_callback(finished)

    def has_job(self, name: str) -> bool:
        return name in self._jobs

    def is_running(self, name: str) -> bool:
        job = self._jobs.get(name)
        return bool(job and job.running)

    async def remove_job(self, name: str) -> bool:
        """Remove future executions, allowing an in-flight callback to finish."""
        job = self._jobs.pop(name, None)
        if job is None:
            return False
        job.active = False
        if not job.running:
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
        return True

    async def reschedule_interval_job(self, name: str, callback: Callable[[], Awaitable[None]],
                                      interval_seconds: float, **kwargs: float | None) -> None:
        await self.remove_job(name)
        self.add_interval_job(name, callback, interval_seconds, **kwargs)  # type: ignore[arg-type]

    async def _run_job(
        self, name: str, callback: Callable[[], Awaitable[None]], interval: float,
        jitter_fraction: float, timeout_seconds: float | None,
        initial_delay_seconds: float,
    ) -> None:
        if initial_delay_seconds:
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=initial_delay_seconds)
                return
            except TimeoutError:
                pass
        while not self._stopping.is_set():
            job = self._jobs.get(name)
            if job is None or not job.active:
                return
            try:
                job.running = True
                if timeout_seconds is None:
                    await callback()
                else:
                    await asyncio.wait_for(callback(), timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Scheduled job failed", extra={"job": name})
            finally:
                job.running = False
            if not job.active:
                return
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
        self._jobs.clear()

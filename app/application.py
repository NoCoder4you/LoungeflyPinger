"""Composition root and lifecycle for the long-running monitor."""

import asyncio
import logging

from app.config import AppConfig
from app.database import Database
from app.http import AsyncHttpClient
from app.monitors import DisneyStoreUKMonitor, GeekCoreMonitor, LoungeflyUKMonitor, TruffleShuffleMonitor
from app.notifications import DiscordNotifier
from app.scheduler import Scheduler
from app.services.monitor_service import MonitorService

LOGGER = logging.getLogger("monitor")


class Application:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.database = Database(config.database_path)
        self.http = AsyncHttpClient(
            timeout_seconds=config.monitor.request_timeout_seconds,
            concurrency_limit=config.monitor.concurrency_limit,
            user_agent=config.monitor.user_agent,
            max_retries=config.monitor.max_retries,
        )
        self.scheduler = Scheduler()
        self.notifier = DiscordNotifier(config.notifications, self.database)
        self.stop_event = asyncio.Event()

    async def start(self) -> None:
        LOGGER.info("Application starting")
        await self.database.connect()
        await self.database.initialize()
        await self.http.start()
        geekcore = self.config.retailers.get("geekcore", {})
        if isinstance(geekcore, dict) and geekcore.get("enabled", False):
            interval = float(geekcore.get("interval_minutes", self.config.monitor.default_interval_minutes))
            service = MonitorService(
                GeekCoreMonitor(self.http), self.database, self.notifier, retailer_name="GeekCore",
                watchlist=self.config.watchlist,
            )
            self.scheduler.add_interval_job(
                "geekcore", service.synchronize, interval * 60, jitter_fraction=0.05
            )
        truffleshuffle = self.config.retailers.get("truffleshuffle", {})
        if isinstance(truffleshuffle, dict) and truffleshuffle.get("enabled", False):
            interval = float(
                truffleshuffle.get("interval_minutes", self.config.monitor.default_interval_minutes)
            )
            service = MonitorService(
                TruffleShuffleMonitor(self.http),
                self.database,
                self.notifier,
                retailer_name="TruffleShuffle",
                watchlist=self.config.watchlist,
            )
            self.scheduler.add_interval_job(
                "truffleshuffle", service.synchronize, interval * 60, jitter_fraction=0.05
            )
        loungefly_uk = self.config.retailers.get("loungefly_uk", {})
        if isinstance(loungefly_uk, dict) and loungefly_uk.get("enabled", False):
            interval = float(
                loungefly_uk.get("interval_minutes", self.config.monitor.default_interval_minutes)
            )
            service = MonitorService(
                LoungeflyUKMonitor(self.http),
                self.database,
                self.notifier,
                retailer_name="Loungefly UK",
                watchlist=self.config.watchlist,
            )
            self.scheduler.add_interval_job(
                "loungefly_uk", service.synchronize, interval * 60, jitter_fraction=0.05
            )
        disney_store_uk = self.config.retailers.get("disney_store_uk", {})
        if isinstance(disney_store_uk, dict) and disney_store_uk.get("enabled", False):
            interval = float(
                disney_store_uk.get("interval_minutes", self.config.monitor.default_interval_minutes)
            )
            service = MonitorService(
                DisneyStoreUKMonitor(self.http), self.database, self.notifier,
                retailer_name="Disney Store UK",
                watchlist=self.config.watchlist,
            )
            self.scheduler.add_interval_job(
                "disney_store_uk", service.synchronize, interval * 60, jitter_fraction=0.05
            )
        LOGGER.info("Application ready", extra={"database": str(self.config.database_path)})

    def request_shutdown(self) -> None:
        if not self.stop_event.is_set():
            LOGGER.info("Shutdown requested")
            self.stop_event.set()

    async def run(self) -> None:
        try:
            await self.start()
            await self.stop_event.wait()
        finally:
            await self.close()

    async def close(self) -> None:
        LOGGER.info("Application stopping")
        await self.scheduler.stop()
        await self.notifier.close()
        await self.http.close()
        await self.database.close()
        LOGGER.info("Application stopped")

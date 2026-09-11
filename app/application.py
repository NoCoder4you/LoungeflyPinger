"""Composition root and lifecycle for the long-running monitor."""

import asyncio
import logging

from app.config import AppConfig
from app.database import Database
from app.http import AsyncHttpClient
from app.monitors import (
    DisneyStoreUKMonitor, DisneyStoreUSMonitor, GeekCoreMonitor, LoungeflyCanadaMonitor, LoungeflyUKMonitor,
    LoungeflyUSMonitor, TruffleShuffleMonitor,
)
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
            backoff_seconds=config.monitor.retry_backoff_seconds,
            rate_limit_requests_per_second=config.monitor.rate_limit_requests_per_second,
        )
        self.scheduler = Scheduler()
        self.notifier = DiscordNotifier(config.notifications, self.database)
        self.stop_event = asyncio.Event()

    async def start(self) -> None:
        LOGGER.info("Application starting")
        await self.database.connect()
        await self.database.initialize()
        await self.http.start()
        retailer_names = {
            "geekcore": "GeekCore", "truffleshuffle": "TruffleShuffle",
            "loungefly_uk": "Loungefly UK", "disney_store_uk": "Disney Store UK",
            "loungefly_us": "Loungefly US",
            "loungefly_canada": "Loungefly Canada",
            "disney_store_us": "Disney Store US",
        }
        assert self.database.connection is not None
        for key, name in retailer_names.items():
            settings = self.config.retailers.get(key, {})
            enabled = isinstance(settings, dict) and bool(settings.get("enabled", False))
            await self.database.connection.execute(
                """INSERT INTO retailers(name, enabled, health) VALUES (?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,
                     health=CASE WHEN excluded.enabled=0 THEN 'DISABLED'
                                 WHEN retailers.health='DISABLED' THEN 'HEALTHY'
                                 ELSE retailers.health END""",
                (name, enabled, "HEALTHY" if enabled else "DISABLED"),
            )
        await self.database.connection.commit()
        geekcore = self.config.retailers.get("geekcore", {})
        if isinstance(geekcore, dict) and geekcore.get("enabled", False):
            interval = float(geekcore.get("interval_minutes", self.config.monitor.default_interval_minutes))
            service = MonitorService(
                GeekCoreMonitor(self.http), self.database, self.notifier, retailer_name="GeekCore",
                watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts,
                release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "geekcore", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
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
                price_alerts=self.config.price_alerts,
                release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "truffleshuffle", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
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
                price_alerts=self.config.price_alerts,
                release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "loungefly_uk", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        loungefly_us = self.config.retailers.get("loungefly_us", {})
        if isinstance(loungefly_us, dict) and loungefly_us.get("enabled", False):
            interval = float(loungefly_us.get("interval_minutes", self.config.monitor.default_interval_minutes))
            service = MonitorService(
                LoungeflyUSMonitor(self.http), self.database, self.notifier,
                retailer_name="Loungefly US", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "loungefly_us", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        loungefly_canada = self.config.retailers.get("loungefly_canada", {})
        if isinstance(loungefly_canada, dict) and loungefly_canada.get("enabled", False):
            interval = float(loungefly_canada.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                LoungeflyCanadaMonitor(self.http), self.database, self.notifier,
                retailer_name="Loungefly Canada", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "loungefly_canada", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
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
                price_alerts=self.config.price_alerts,
                release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "disney_store_uk", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        disney_store_us = self.config.retailers.get("disney_store_us", {})
        if isinstance(disney_store_us, dict) and disney_store_us.get("enabled", False):
            interval = float(disney_store_us.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                DisneyStoreUSMonitor(self.http), self.database, self.notifier,
                retailer_name="Disney Store US", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "disney_store_us", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
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

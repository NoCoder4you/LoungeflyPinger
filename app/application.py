"""Composition root and lifecycle for the long-running monitor."""

import asyncio
import logging

from app.config import AppConfig
from app.database import Database
from app.http import AsyncHttpClient
from app.notifications import DiscordNotifier
from app.scheduler import Scheduler
from app.services.watchlist_manager import WatchlistManager
from app.services.retailer_manager import RetailerManager
from app.services.notification_settings import NotificationSettingsService
from app.discord_control import DiscordControlService
from app.watchlist import Watchlist

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
            max_response_bytes=config.monitor.max_response_bytes,
        )
        self.scheduler = Scheduler()
        self.notification_settings = NotificationSettingsService(
            self.database, config.notifications
        )
        self.notifier = DiscordNotifier(
            config.notifications, self.database, settings=self.notification_settings
        )
        self.watchlists = WatchlistManager(self.database)
        self.retailers = RetailerManager(
            config, self.database, self.scheduler, self.http, self.notifier, self.watchlists
        )
        self.discord_control = DiscordControlService(
            config.discord_bot, self.watchlists, self.database, config, self.retailers,
            self.scheduler, self.notification_settings
        )
        self.retailers.set_scan_activity_callback(
            self.discord_control.update_scan_presence
        )
        self.stop_event = asyncio.Event()
        self._close_lock = asyncio.Lock()
        self._closed = False

    async def start(self) -> None:
        LOGGER.info("Application starting")
        await self.database.connect()
        await self.database.initialize()
        await self.notification_settings.initialize()
        await self.watchlists.initialize(self.config.watchlist or Watchlist())
        self.scheduler.add_interval_job(
            "database_backup",
            self._backup_database,
            self.config.backup_interval_hours * 3600,
            initial_delay_seconds=self.config.backup_initial_delay_seconds,
        )
        await self.http.start()
        await self.retailers.initialize()
        await self.discord_control.start()
        LOGGER.info("Application ready", extra={"database": str(self.config.database_path)})

    async def _backup_database(self) -> None:
        backup = await self.database.backup(
            self.config.backup_directory, self.config.backup_count
        )
        LOGGER.info("Database backup completed", extra={"backup": str(backup)})

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
        async with self._close_lock:
            if self._closed:
                return
            LOGGER.info("Application stopping")
            failures: list[tuple[str, BaseException]] = []
            closers = [("scheduler", self.scheduler.stop)]
            if self.config.discord_bot.enabled:
                closers.append(("Discord control", self.discord_control.close))
            closers.extend((
                ("Discord notifier", self.notifier.close),
                ("HTTP client", self.http.close),
                ("SQLite database", self.database.close),
            ))
            for name, closer in closers:
                try:
                    await closer()
                except BaseException as exc:
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise
                    failures.append((name, exc))
                    LOGGER.exception("Resource cleanup failed", extra={"resource": name})
            self._closed = True
            if failures:
                LOGGER.error("Application stopped with cleanup failures", extra={
                    "resources": [name for name, _ in failures]
                })
            else:
                LOGGER.info("Application stopped")

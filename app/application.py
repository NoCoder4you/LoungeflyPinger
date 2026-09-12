"""Composition root and lifecycle for the long-running monitor."""

import asyncio
import logging

from app.config import AppConfig
from app.database import Database
from app.http import AsyncHttpClient
from app.monitors import (
    BoxLunchMonitor, CircleOfHopeMonitor, CMPopMonitor, CoolMerchMonitor, CordysCornerMonitor, DamagedSocietyMonitor, DisneyStoreUKMonitor, DisneyStoreUSMonitor, EntertainmentEarthMonitor,
    EMPMonitor, EMP_REGIONS, ForbiddenPlanetMonitor, GeekCoreMonitor, GeekGarageMonitor, GetReadyComicsMonitor, InfinityCollectablesMonitor, KoolazMonitor,
    LFLoversMonitor, MagicMadhouseMonitor, MerchoidUKMonitor, RazmatazzMonitor,
    HotTopicUSMonitor, LoungeflyCanadaMonitor, LoungeflyUKMonitor,
    LoungeflyUSMonitor, ModernPinUpMonitor, OzzieCollectablesMonitor, PinkALaModeMonitor, PopcultchaMonitor, Street707Monitor,
    SomethingDifferentMonitor, TruffleShuffleMonitor,
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
            "damaged_society_uk": "Damaged Society UK",
            "cool_merch_uk": "Cool-Merch UK",
            "koolaz_uk": "Koolaz UK",
            "cm_pop_uk": "CM POP UK",
            "razmatazz_uk": "Razmatazz UK",
            "geekcore": "GeekCore", "geek_garage_uk": "Geek Garage", "truffleshuffle": "TruffleShuffle",
            "get_ready_comics_uk": "Get Ready Comics UK",
            "loungefly_uk": "Loungefly UK", "disney_store_uk": "Disney Store UK",
            "loungefly_us": "Loungefly US",
            "loungefly_canada": "Loungefly Canada",
            "disney_store_us": "Disney Store US",
            "boxlunch": "BoxLunch",
            "hot_topic_us": "Hot Topic US",
            "entertainment_earth": "Entertainment Earth",
            "modern_pinup": "Modern PinUp",
            "circle_of_hope": "Circle Of Hope Boutique",
            "merchoid_uk": "Merchoid UK",
            "magic_madhouse_uk": "Magic Madhouse UK",
            "pink_a_la_mode": "Pink a la Mode",
            "street_707": "707 Street",
            "cordys_corner": "Cordy's Corner",
            "infinity_collectables": "Infinity Collectables",
            "lf_lovers": "LF Lovers",
            "something_different_uk": "Something Different Gift Shop UK",
            "forbidden_planet_uk": "Forbidden Planet International UK",
            "popcultcha": "Popcultcha",
            "ozzie_collectables": "Ozzie Collectables",
            **{
                ("large_nl" if code == "nl" else f"emp_{code}"): region.name
                for code, region in EMP_REGIONS.items()
            },
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
        # Each storefront is an independent scheduler/service so a regional outage
        # cannot affect the health or synchronization of another storefront.
        damaged_society = self.config.retailers.get("damaged_society_uk", {})
        if isinstance(damaged_society, dict) and damaged_society.get("enabled", False):
            interval = float(damaged_society.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                DamagedSocietyMonitor(self.http), self.database, self.notifier,
                retailer_name="Damaged Society UK", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "damaged_society_uk", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        koolaz = self.config.retailers.get("koolaz_uk", {})
        if isinstance(koolaz, dict) and koolaz.get("enabled", False):
            interval = float(koolaz.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                KoolazMonitor(self.http), self.database, self.notifier,
                retailer_name="Koolaz UK", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "koolaz_uk", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        cool_merch = self.config.retailers.get("cool_merch_uk", {})
        if isinstance(cool_merch, dict) and cool_merch.get("enabled", False):
            interval = float(cool_merch.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                CoolMerchMonitor(self.http), self.database, self.notifier,
                retailer_name="Cool-Merch UK", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "cool_merch_uk", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        for code, region in EMP_REGIONS.items():
            key = "large_nl" if code == "nl" else f"emp_{code}"
            settings = self.config.retailers.get(key, {})
            if isinstance(settings, dict) and settings.get("enabled", False):
                interval = float(settings.get("interval_minutes", self.config.monitor.default_interval_minutes))
                service = MonitorService(
                    EMPMonitor(self.http, region), self.database, self.notifier,
                    retailer_name=region.name, watchlist=self.config.watchlist,
                    price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                    missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                    failure_alert_threshold=self.config.monitor.failure_alert_threshold,
                )
                self.scheduler.add_interval_job(
                    key, service.synchronize, interval * 60, jitter_fraction=0.05,
                    timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
                )
        cm_pop = self.config.retailers.get("cm_pop_uk", {})
        if isinstance(cm_pop, dict) and cm_pop.get("enabled", False):
            interval = float(cm_pop.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                CMPopMonitor(self.http), self.database, self.notifier,
                retailer_name="CM POP UK", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "cm_pop_uk", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
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
        geek_garage = self.config.retailers.get("geek_garage_uk", {})
        if isinstance(geek_garage, dict) and geek_garage.get("enabled", False):
            interval = float(geek_garage.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                GeekGarageMonitor(self.http), self.database, self.notifier,
                retailer_name="Geek Garage", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "geek_garage_uk", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        razmatazz = self.config.retailers.get("razmatazz_uk", {})
        if isinstance(razmatazz, dict) and razmatazz.get("enabled", False):
            interval = float(razmatazz.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                RazmatazzMonitor(self.http), self.database, self.notifier,
                retailer_name="Razmatazz UK", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "razmatazz_uk", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        get_ready_comics = self.config.retailers.get("get_ready_comics_uk", {})
        if isinstance(get_ready_comics, dict) and get_ready_comics.get("enabled", False):
            interval = float(get_ready_comics.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                GetReadyComicsMonitor(self.http), self.database, self.notifier,
                retailer_name="Get Ready Comics UK", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "get_ready_comics_uk", service.synchronize, interval * 60,
                jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        lf_lovers = self.config.retailers.get("lf_lovers", {})
        if isinstance(lf_lovers, dict) and lf_lovers.get("enabled", False):
            interval = float(lf_lovers.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                LFLoversMonitor(self.http), self.database, self.notifier,
                retailer_name="LF Lovers", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "lf_lovers", service.synchronize, interval * 60, jitter_fraction=0.05,
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
        boxlunch = self.config.retailers.get("boxlunch", {})
        if isinstance(boxlunch, dict) and boxlunch.get("enabled", False):
            interval = float(boxlunch.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                BoxLunchMonitor(self.http), self.database, self.notifier,
                retailer_name="BoxLunch", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "boxlunch", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        hot_topic_us = self.config.retailers.get("hot_topic_us", {})
        if isinstance(hot_topic_us, dict) and hot_topic_us.get("enabled", False):
            interval = float(hot_topic_us.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                HotTopicUSMonitor(self.http), self.database, self.notifier,
                retailer_name="Hot Topic US", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "hot_topic_us", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        entertainment_earth = self.config.retailers.get("entertainment_earth", {})
        if isinstance(entertainment_earth, dict) and entertainment_earth.get("enabled", False):
            interval = float(entertainment_earth.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                EntertainmentEarthMonitor(self.http), self.database, self.notifier,
                retailer_name="Entertainment Earth", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "entertainment_earth", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        modern_pinup = self.config.retailers.get("modern_pinup", {})
        circle_of_hope = self.config.retailers.get("circle_of_hope", {})
        merchoid = self.config.retailers.get("merchoid_uk", {})
        if isinstance(merchoid, dict) and merchoid.get("enabled", False):
            interval = float(merchoid.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                MerchoidUKMonitor(self.http), self.database, self.notifier,
                retailer_name="Merchoid UK", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "merchoid_uk", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        magic_madhouse = self.config.retailers.get("magic_madhouse_uk", {})
        if isinstance(magic_madhouse, dict) and magic_madhouse.get("enabled", False):
            interval = float(magic_madhouse.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                MagicMadhouseMonitor(self.http), self.database, self.notifier,
                retailer_name="Magic Madhouse UK", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "magic_madhouse_uk", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        if isinstance(modern_pinup, dict) and modern_pinup.get("enabled", False):
            interval = float(modern_pinup.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                ModernPinUpMonitor(self.http), self.database, self.notifier,
                retailer_name="Modern PinUp", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "modern_pinup", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        if isinstance(circle_of_hope, dict) and circle_of_hope.get("enabled", False):
            interval = float(circle_of_hope.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                CircleOfHopeMonitor(self.http), self.database, self.notifier,
                retailer_name="Circle Of Hope Boutique", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "circle_of_hope", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        pink_a_la_mode = self.config.retailers.get("pink_a_la_mode", {})
        if isinstance(pink_a_la_mode, dict) and pink_a_la_mode.get("enabled", False):
            interval = float(pink_a_la_mode.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                PinkALaModeMonitor(self.http), self.database, self.notifier,
                retailer_name="Pink a la Mode", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "pink_a_la_mode", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        street_707 = self.config.retailers.get("street_707", {})
        if isinstance(street_707, dict) and street_707.get("enabled", False):
            interval = float(street_707.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                Street707Monitor(self.http), self.database, self.notifier,
                retailer_name="707 Street", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "street_707", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        cordys_corner = self.config.retailers.get("cordys_corner", {})
        if isinstance(cordys_corner, dict) and cordys_corner.get("enabled", False):
            interval = float(cordys_corner.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                CordysCornerMonitor(self.http), self.database, self.notifier,
                retailer_name="Cordy's Corner", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "cordys_corner", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        infinity = self.config.retailers.get("infinity_collectables", {})
        if isinstance(infinity, dict) and infinity.get("enabled", False):
            interval = float(infinity.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                InfinityCollectablesMonitor(self.http), self.database, self.notifier,
                retailer_name="Infinity Collectables", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "infinity_collectables", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        something_different = self.config.retailers.get("something_different_uk", {})
        if isinstance(something_different, dict) and something_different.get("enabled", False):
            interval = float(something_different.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                SomethingDifferentMonitor(self.http), self.database, self.notifier,
                retailer_name="Something Different Gift Shop UK", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "something_different_uk", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        forbidden_planet = self.config.retailers.get("forbidden_planet_uk", {})
        if isinstance(forbidden_planet, dict) and forbidden_planet.get("enabled", False):
            interval = float(forbidden_planet.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                ForbiddenPlanetMonitor(self.http), self.database, self.notifier,
                retailer_name="Forbidden Planet International UK", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "forbidden_planet_uk", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        popcultcha = self.config.retailers.get("popcultcha", {})
        if isinstance(popcultcha, dict) and popcultcha.get("enabled", False):
            interval = float(popcultcha.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            service = MonitorService(
                PopcultchaMonitor(self.http), self.database, self.notifier,
                retailer_name="Popcultcha", watchlist=self.config.watchlist,
                price_alerts=self.config.price_alerts, release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "popcultcha", service.synchronize, interval * 60, jitter_fraction=0.05,
                timeout_seconds=self.config.monitor.retailer_job_timeout_seconds,
            )
        ozzie = self.config.retailers.get("ozzie_collectables", {})
        if isinstance(ozzie, dict) and ozzie.get("enabled", False):
            interval = float(ozzie.get(
                "interval_minutes", self.config.monitor.default_interval_minutes
            ))
            detail_batch_size = int(ozzie.get("detail_batch_size", 20))
            service = MonitorService(
                OzzieCollectablesMonitor(self.http, detail_batch_size=detail_batch_size),
                self.database, self.notifier, retailer_name="Ozzie Collectables",
                watchlist=self.config.watchlist, price_alerts=self.config.price_alerts,
                release_alerts=self.config.release_alerts,
                missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold,
            )
            self.scheduler.add_interval_job(
                "ozzie_collectables", service.synchronize, interval * 60,
                jitter_fraction=0.05,
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

"""Trusted retailer registry and live retailer lifecycle management."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from app.config import AppConfig
from app.database import Database
from app.http import AsyncHttpClient
from app.monitors import (
    AmyDavidMagicMonitor, BagDudeMonitor, BoxLunchMonitor, CircleOfHopeMonitor,
    CMPopMonitor, CoolMerchMonitor, CordysCornerMonitor, DamagedSocietyMonitor,
    DisneyMadMonitor, DisneyStoreUKMonitor, DisneyStoreUSMonitor, EMPMonitor,
    EMP_REGIONS, EntertainmentEarthMonitor, ForbiddenPlanetMonitor,
    GeekCoreMonitor, GeekGarageMonitor, GetReadyComicsMonitor,
    GwensMermaidCoveMonitor, HotTopicUSMonitor, InfinityCollectablesMonitor,
    KoolazMonitor, LFLoversMonitor, LoungeflyCanadaMonitor, LoungeflyUKMonitor,
    LoungeflyUSMonitor, MagicMadhouseMonitor, MerchoidUKMonitor, ModernPinUpMonitor,
    OzzieCollectablesMonitor, PinkALaModeMonitor, PopPelicanMonitor,
    PopcultchaMonitor, RazmatazzMonitor, SomethingDifferentMonitor,
    Street707Monitor, TruffleShuffleMonitor, World11GamesMonitor,
)
from app.notifications.base import NotificationProvider
from app.scheduler import Scheduler
from app.services.monitor_service import MonitorService
from app.services.watchlist_manager import WatchlistManager

MIN_INTERVAL_MINUTES = 1.0
MAX_INTERVAL_MINUTES = 1440.0


@dataclass(frozen=True, slots=True)
class RetailerDefinition:
    key: str
    display_name: str
    monitor_factory: Callable[[AsyncHttpClient, dict[str, Any]], Any]
    region: str | None = None
    manual_scan_supported: bool = True
    specialized_settings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetailerState:
    key: str
    display_name: str
    enabled: bool
    interval_minutes: float
    default_enabled: bool
    default_interval_minutes: float
    enabled_override: bool | None
    interval_override: float | None
    specialized_settings: dict[str, Any]
    health: str = "DISABLED"
    last_success: str | None = None
    last_failure: str | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    response_status: int | None = None
    request_duration: float | None = None
    running: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_retailer_registry() -> dict[str, RetailerDefinition]:
    entries: list[tuple[str, str, type[Any]]] = [
        ("amy_david_magic", "AmyDavidMagic", AmyDavidMagicMonitor), ("bag_dude", "The Bag Dude", BagDudeMonitor),
        ("boxlunch", "BoxLunch", BoxLunchMonitor), ("circle_of_hope", "Circle Of Hope Boutique", CircleOfHopeMonitor),
        ("cm_pop_uk", "CM POP UK", CMPopMonitor), ("cool_merch_uk", "Cool-Merch UK", CoolMerchMonitor),
        ("cordys_corner", "Cordy's Corner", CordysCornerMonitor), ("damaged_society_uk", "Damaged Society UK", DamagedSocietyMonitor),
        ("disney_mad_uk", "Disney Mad", DisneyMadMonitor), ("disney_store_uk", "Disney Store UK", DisneyStoreUKMonitor),
        ("disney_store_us", "Disney Store US", DisneyStoreUSMonitor), ("entertainment_earth", "Entertainment Earth", EntertainmentEarthMonitor),
        ("forbidden_planet_uk", "Forbidden Planet International UK", ForbiddenPlanetMonitor), ("geek_garage_uk", "Geek Garage", GeekGarageMonitor),
        ("geekcore", "GeekCore", GeekCoreMonitor), ("get_ready_comics_uk", "Get Ready Comics UK", GetReadyComicsMonitor),
        ("gwens_mermaid_cove", "Gwen's Mermaid Cove", GwensMermaidCoveMonitor), ("hot_topic_us", "Hot Topic US", HotTopicUSMonitor),
        ("infinity_collectables", "Infinity Collectables", InfinityCollectablesMonitor), ("koolaz_uk", "Koolaz UK", KoolazMonitor),
        ("lf_lovers", "LF Lovers", LFLoversMonitor), ("loungefly_canada", "Loungefly Canada", LoungeflyCanadaMonitor),
        ("loungefly_uk", "Loungefly UK", LoungeflyUKMonitor), ("loungefly_us", "Loungefly US", LoungeflyUSMonitor),
        ("magic_madhouse_uk", "Magic Madhouse UK", MagicMadhouseMonitor), ("merchoid_uk", "Merchoid UK", MerchoidUKMonitor),
        ("modern_pinup", "Modern PinUp", ModernPinUpMonitor), ("pink_a_la_mode", "Pink a la Mode", PinkALaModeMonitor),
        ("pop_pelican", "Pop Pelican", PopPelicanMonitor), ("popcultcha", "Popcultcha", PopcultchaMonitor),
        ("razmatazz_uk", "Razmatazz UK", RazmatazzMonitor), ("something_different_uk", "Something Different Gift Shop UK", SomethingDifferentMonitor),
        ("street_707", "707 Street", Street707Monitor), ("truffleshuffle", "TruffleShuffle", TruffleShuffleMonitor),
        ("world_1_1_games", "WORLD 1-1 GAMES", World11GamesMonitor),
    ]
    registry = {key: RetailerDefinition(key, name, lambda http, settings, cls=cls: cls(http),
                specialized_settings=("incoming_interval_minutes", "reconciliation_interval_minutes", "high_priority_interval_minutes") if key == "amy_david_magic" else ())
                for key, name, cls in entries}
    registry["ozzie_collectables"] = RetailerDefinition("ozzie_collectables", "Ozzie Collectables",
        lambda http, settings: OzzieCollectablesMonitor(http, detail_batch_size=int(settings.get("detail_batch_size", 20))),
        specialized_settings=("detail_batch_size",))
    for code, region in EMP_REGIONS.items():
        key = "large_nl" if code == "nl" else f"emp_{code}"
        registry[key] = RetailerDefinition(key, region.name,
            lambda http, settings, selected=region: EMPMonitor(http, selected), region=code.upper())
    return registry


class ScanAlreadyRunning(RuntimeError): pass
class UnknownRetailer(KeyError): pass


class RetailerManager:
    """Single authority for effective configuration, jobs and scan coordination."""
    def __init__(self, config: AppConfig, database: Database, scheduler: Scheduler,
                 http: AsyncHttpClient, notifier: NotificationProvider, watchlists: WatchlistManager,
                 registry: dict[str, RetailerDefinition] | None = None) -> None:
        self.config, self.database, self.scheduler = config, database, scheduler
        self.http, self.notifier, self.watchlists = http, notifier, watchlists
        self.registry = registry or build_retailer_registry()
        self._overrides: dict[str, tuple[bool | None, float | None]] = {}
        self._services: dict[str, MonitorService] = {}
        self._scan_locks = {key: asyncio.Lock() for key in self.registry}
        self._mutation_lock = asyncio.Lock()

    async def initialize(self) -> None:
        assert self.database.connection is not None
        rows = await (await self.database.connection.execute(
            "SELECT retailer_key,enabled,interval_minutes FROM retailer_runtime_overrides")).fetchall()
        self._overrides = {r[0]: (None if r[1] is None else bool(r[1]), r[2]) for r in rows if r[0] in self.registry}
        for key, definition in self.registry.items():
            state = self.effective(key)
            await self.database.connection.execute(
                """INSERT INTO retailers(name,enabled,health) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET
                enabled=excluded.enabled,health=CASE WHEN excluded.enabled=0 THEN 'DISABLED'
                WHEN retailers.health='DISABLED' THEN 'HEALTHY' ELSE retailers.health END""",
                (definition.display_name, state.enabled, "HEALTHY" if state.enabled else "DISABLED"))
        await self.database.connection.commit()
        for key in self.registry:
            if self.effective(key).enabled:
                self._schedule(key)

    def _defaults(self, key: str) -> tuple[bool, float, dict[str, Any]]:
        if key not in self.registry: raise UnknownRetailer(key)
        raw = self.config.retailers.get(key, {})
        settings = raw if isinstance(raw, dict) else {}
        return bool(settings.get("enabled", False)), float(settings.get("interval_minutes", self.config.monitor.default_interval_minutes)), settings

    def effective(self, key: str) -> RetailerState:
        enabled, interval, settings = self._defaults(key)
        enabled_override, interval_override = self._overrides.get(key, (None, None))
        definition = self.registry[key]
        return RetailerState(key, definition.display_name, enabled if enabled_override is None else enabled_override,
            interval if interval_override is None else interval_override, enabled, interval, enabled_override, interval_override,
            {name: settings.get(name) for name in definition.specialized_settings if name in settings}, running=self._scan_locks[key].locked())

    def _service(self, key: str) -> MonitorService:
        if key not in self._services:
            definition = self.registry[key]
            _, _, settings = self._defaults(key)
            self._services[key] = MonitorService(definition.monitor_factory(self.http, settings), self.database, self.notifier,
                retailer_name=definition.display_name, watchlist=self.watchlists, price_alerts=self.config.price_alerts,
                release_alerts=self.config.release_alerts, missing_scan_threshold=self.config.monitor.missing_scan_threshold,
                failure_alert_threshold=self.config.monitor.failure_alert_threshold)
        return self._services[key]

    def _schedule(self, key: str) -> None:
        if self.scheduler.has_job(key): return
        state = self.effective(key)
        self.scheduler.add_interval_job(key, lambda: self._run_scan(key, "scheduled", None), state.interval_minutes * 60,
            jitter_fraction=.05, timeout_seconds=self.config.monitor.retailer_job_timeout_seconds)

    async def _persist(self, key: str, enabled: bool | None, interval: float | None, actor: str) -> None:
        assert self.database.connection is not None
        async with self.database.write_lock:
            await self.database.connection.execute("""INSERT INTO retailer_runtime_overrides
                (retailer_key,enabled,interval_minutes,updated_at,updated_by_discord_user_id) VALUES(?,?,?,?,?)
                ON CONFLICT(retailer_key) DO UPDATE SET enabled=excluded.enabled,interval_minutes=excluded.interval_minutes,
                updated_at=excluded.updated_at,updated_by_discord_user_id=excluded.updated_by_discord_user_id""",
                (key, enabled, interval, datetime.now(UTC).isoformat(), actor))
            await self.database.connection.commit()
        self._overrides[key] = (enabled, interval)

    async def set_enabled(self, key: str, enabled: bool, actor: str) -> RetailerState:
        async with self._mutation_lock:
            current = self.effective(key); _, interval = self._overrides.get(key, (None, None))
            await self._persist(key, enabled, interval, actor)
            assert self.database.connection is not None
            async with self.database.write_lock:
                await self.database.connection.execute("UPDATE retailers SET enabled=?,health=? WHERE name=?",
                    (enabled, "HEALTHY" if enabled else "DISABLED", current.display_name))
                await self.database.connection.commit()
            if enabled: self._schedule(key)
            else: await self.scheduler.remove_job(key)
            return await self.get_state(key)

    async def set_interval(self, key: str, minutes: float, actor: str) -> RetailerState:
        if isinstance(minutes, bool) or not MIN_INTERVAL_MINUTES <= minutes <= MAX_INTERVAL_MINUTES:
            raise ValueError(f"interval must be between {MIN_INTERVAL_MINUTES:g} and {MAX_INTERVAL_MINUTES:g} minutes")
        async with self._mutation_lock:
            enabled, _ = self._overrides.get(key, (None, None)); await self._persist(key, enabled, float(minutes), actor)
            if self.effective(key).enabled:
                await self.scheduler.reschedule_interval_job(key, lambda: self._run_scan(key, "scheduled", None), minutes * 60,
                    jitter_fraction=.05, timeout_seconds=self.config.monitor.retailer_job_timeout_seconds)
            return await self.get_state(key)

    async def reset(self, key: str) -> RetailerState:
        self._defaults(key)
        async with self._mutation_lock:
            assert self.database.connection is not None
            async with self.database.write_lock:
                await self.database.connection.execute("DELETE FROM retailer_runtime_overrides WHERE retailer_key=?", (key,)); await self.database.connection.commit()
            self._overrides.pop(key, None); state = self.effective(key)
            await self.scheduler.remove_job(key)
            if state.enabled: self._schedule(key)
            async with self.database.write_lock:
                await self.database.connection.execute("UPDATE retailers SET enabled=?,health=? WHERE name=?", (state.enabled, "HEALTHY" if state.enabled else "DISABLED", state.display_name))
                await self.database.connection.commit()
            return await self.get_state(key)

    async def scan(self, key: str, actor: str) -> dict[str, Any]:
        self._defaults(key)
        if not self.registry[key].manual_scan_supported: raise ValueError("manual scan is not supported")
        return await self._run_scan(key, "manual", actor)

    async def _run_scan(self, key: str, source: str, actor: str | None) -> dict[str, Any]:
        lock = self._scan_locks[key]
        if lock.locked(): raise ScanAlreadyRunning(f"{key} scan is already running")
        async with lock:
            started_at = datetime.now(UTC); started = time.monotonic()
            alerts = await self._service(key).synchronize(); duration = time.monotonic() - started
            state = await self.get_state(key); success = state.last_failure is None or state.last_success is not None and state.last_success > state.last_failure
            error = (state.last_error or "")[:500] or None
            assert self.database.connection is not None
            async with self.database.write_lock:
                await self.database.connection.execute("""INSERT INTO retailer_scan_history
                    (retailer_key,started_at,completed_at,trigger_source,discord_user_id,success,alert_count,duration,http_status,error_summary)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""", (key, started_at.isoformat(), datetime.now(UTC).isoformat(), source, actor,
                    success, len(alerts), duration, state.response_status, error)); await self.database.connection.commit()
            return {"retailer": key, "success": success, "duration": duration, "alert_count": len(alerts),
                    "health": state.health, "error": error, "enabled": self.effective(key).enabled}

    async def get_state(self, key: str) -> RetailerState:
        state = self.effective(key); assert self.database.connection is not None
        row = await (await self.database.connection.execute("SELECT health,last_success,last_failure,consecutive_failures,last_error,response_status,request_duration FROM retailers WHERE name=?", (state.display_name,))).fetchone()
        if not row: return state
        values = state.as_dict(); values.update(health=row[0], last_success=row[1], last_failure=row[2], consecutive_failures=row[3], last_error=(row[4] or "")[:500] or None, response_status=row[5], request_duration=row[6], running=self._scan_locks[key].locked())
        return RetailerState(**values)

    async def list_states(self) -> list[RetailerState]:
        return [await self.get_state(key) for key in sorted(self.registry)]

    async def failures(self, key: str, limit: int = 5) -> list[dict[str, Any]]:
        self._defaults(key); assert self.database.connection is not None
        rows = await (await self.database.connection.execute("SELECT completed_at,trigger_source,http_status,error_summary,duration FROM retailer_scan_history WHERE retailer_key=? AND success=0 ORDER BY id DESC LIMIT ?", (key, min(max(limit, 1), 10)))).fetchall()
        return [dict(zip(("completed_at","trigger","http_status","error","duration"), row)) for row in rows]

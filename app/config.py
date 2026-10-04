"""Validated YAML and environment configuration loading."""

from __future__ import annotations

import os
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from dotenv import load_dotenv

if TYPE_CHECKING:
    from app.watchlist import Watchlist


class ConfigurationError(ValueError):
    """Raised when application configuration is missing or invalid."""


SUPPORTED_RETAILERS = frozenset({
    "amy_david_magic", "bag_dude", "boxlunch", "circle_of_hope", "cm_pop_uk",
    "cool_merch_uk", "cordys_corner", "damaged_society_uk", "disney_mad_uk",
    "disney_store_uk", "disney_store_us", "emp_de", "emp_es", "emp_fr", "emp_it",
    "entertainment_earth", "forbidden_planet_uk", "geek_garage_uk", "geekcore",
    "get_ready_comics_uk", "gwens_mermaid_cove", "hot_topic_us", "infinity_collectables",
    "koolaz_uk", "large_nl", "lf_lovers", "loungefly_canada", "loungefly_uk",
    "loungefly_us", "magic_madhouse_uk", "merchoid_uk", "modern_pinup",
    "ozzie_collectables", "pink_a_la_mode", "pop_pelican", "popcultcha", "razmatazz_uk",
    "something_different_uk", "street_707", "truffleshuffle", "world_1_1_games",
})


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    default_interval_minutes: float = 5
    request_timeout_seconds: float = 20
    concurrency_limit: int = 5
    max_retries: int = 3
    user_agent: str = "LoungeflyMonitor/0.1"
    missing_scan_threshold: int = 3
    failure_alert_threshold: int = 5
    retry_backoff_seconds: float = 1
    rate_limit_requests_per_second: float = 5
    # Full-scan deadlines are optional because a fixed wall-clock limit can
    # cancel healthy retailers whose paginated requests are still progressing.
    # Individual HTTP requests remain bounded independently.
    retailer_job_timeout_seconds: float | None = None
    max_response_bytes: int = 10_485_760


@dataclass(frozen=True, slots=True)
class PriceAlertConfig:
    enabled: bool = True
    minimum_drop_percent: float = 10
    minimum_drop_value: float = 5


@dataclass(frozen=True, slots=True)
class ReleaseAlertConfig:
    enabled: bool = True
    reminders_seconds: tuple[int, ...] = (86400, 3600)
    notify_existing_on_upgrade: bool = False


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str = "INFO"
    path: Path = Path("logs/loungefly-monitor.log")
    max_bytes: int = 5_242_880
    backup_count: int = 3


@dataclass(frozen=True, slots=True)
class NotificationConfig:
    """Secret notification endpoints loaded exclusively from the environment."""

    discord_webhook_url: str | None = None
    discord_admin_webhook_url: str | None = None
    discord_alert_mention_mode: str = "everyone"
    discord_alert_role_id: int | None = None
    product_removed_enabled: bool = True


@dataclass(frozen=True, slots=True)
class DiscordBotConfig:
    enabled: bool = False
    token: str | None = None
    guild_id: int | None = None
    allowed_user_ids: frozenset[int] = frozenset()
    allowed_role_ids: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class AppConfig:
    monitor: MonitorConfig
    database_path: Path
    logging: LoggingConfig
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    retailers: dict[str, Any] = field(default_factory=dict)
    watchlist: Watchlist | None = None
    price_alerts: PriceAlertConfig = field(default_factory=PriceAlertConfig)
    release_alerts: ReleaseAlertConfig = field(default_factory=ReleaseAlertConfig)
    backup_directory: Path = Path("data/backups")
    backup_interval_hours: float = 24
    backup_initial_delay_seconds: float = 30
    backup_count: int = 7
    discord_bot: DiscordBotConfig = field(default_factory=DiscordBotConfig)


def _positive(value: Any, name: str, cast: type = float) -> Any:
    if isinstance(value, bool):
        raise ConfigurationError(f"{name} must be a number")
    try:
        converted = cast(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if converted <= 0 or not math.isfinite(converted):
        raise ConfigurationError(f"{name} must be greater than zero")
    return converted


def load_config(
    path: str | Path = "config/retailers.yaml",
    env_path: str | Path = ".env",
    watchlist_path: str | Path = "config/watchlist.yaml",
) -> AppConfig:
    load_dotenv(env_path, override=False)
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Unable to load configuration: {config_path}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("Configuration root must be a mapping")
    monitor_raw = raw.get("monitor", {})
    logging_raw = raw.get("logging", {})
    if not isinstance(monitor_raw, dict) or not isinstance(logging_raw, dict):
        raise ConfigurationError("monitor and logging settings must be mappings")
    monitor = MonitorConfig(
        default_interval_minutes=_positive(monitor_raw.get("default_interval_minutes", 5), "default_interval_minutes"),
        request_timeout_seconds=_positive(monitor_raw.get("request_timeout_seconds", 20), "request_timeout_seconds"),
        concurrency_limit=_positive(monitor_raw.get("concurrency_limit", 5), "concurrency_limit", int),
        max_retries=_positive(monitor_raw.get("max_retries", 3), "max_retries", int),
        user_agent=str(monitor_raw.get("user_agent", "LoungeflyMonitor/0.1")).strip(),
        missing_scan_threshold=_positive(
            monitor_raw.get("missing_scan_threshold", 3), "missing_scan_threshold", int
        ),
        failure_alert_threshold=_positive(
            monitor_raw.get("failure_alert_threshold", 5), "failure_alert_threshold", int
        ),
        retry_backoff_seconds=_positive(
            monitor_raw.get("retry_backoff_seconds", 1), "retry_backoff_seconds"
        ),
        rate_limit_requests_per_second=_positive(
            monitor_raw.get("rate_limit_requests_per_second", 5),
            "rate_limit_requests_per_second",
        ),
        retailer_job_timeout_seconds=(
            None
            if monitor_raw.get("retailer_job_timeout_seconds") is None
            else _positive(
                monitor_raw["retailer_job_timeout_seconds"],
                "retailer_job_timeout_seconds",
            )
        ),
        max_response_bytes=_positive(
            monitor_raw.get("max_response_bytes", 10_485_760), "max_response_bytes", int
        ),
    )
    if not monitor.user_agent:
        raise ConfigurationError("user_agent must not be empty")
    configured_level = os.getenv("LOUNGEFLY_LOG_LEVEL")
    if configured_level is None:
        configured_level = logging_raw.get("level", "INFO")
    if not isinstance(configured_level, str):
        raise ConfigurationError("logging level must be a string")
    level = configured_level.strip().upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError("logging level is invalid")
    logging_config = LoggingConfig(
        level=level,
        path=Path(os.getenv("LOUNGEFLY_LOG_PATH", logging_raw.get("path", "logs/loungefly-monitor.log"))),
        max_bytes=_positive(logging_raw.get("max_bytes", 5_242_880), "max_bytes", int),
        backup_count=_positive(logging_raw.get("backup_count", 3), "backup_count", int),
    )
    database_raw = raw.get("database", {})
    if not isinstance(database_raw, dict):
        raise ConfigurationError("database settings must be a mapping")
    database_path = Path(os.getenv("LOUNGEFLY_DATABASE_PATH", database_raw.get("path", "data/loungefly.db")))
    backup_directory = Path(os.getenv(
        "LOUNGEFLY_BACKUP_DIRECTORY", database_raw.get("backup_directory", "data/backups")
    ))
    backup_interval_hours = _positive(
        os.getenv("LOUNGEFLY_BACKUP_INTERVAL_HOURS", database_raw.get("backup_interval_hours", 24)),
        "backup_interval_hours",
    )
    backup_initial_delay_seconds = _positive(
        os.getenv(
            "LOUNGEFLY_BACKUP_INITIAL_DELAY_SECONDS",
            database_raw.get("backup_initial_delay_seconds", 30),
        ),
        "backup_initial_delay_seconds",
    )
    backup_count = _positive(
        os.getenv("LOUNGEFLY_BACKUP_COUNT", database_raw.get("backup_count", 7)),
        "backup_count", int,
    )
    notifications_raw = raw.get("notifications", {})
    retailers = raw.get("retailers", {})
    if not isinstance(notifications_raw, dict) or not isinstance(retailers, dict):
        raise ConfigurationError("notifications and retailers must be mappings")
    unknown_retailers = set(retailers) - SUPPORTED_RETAILERS
    if unknown_retailers:
        raise ConfigurationError(
            f"unknown retailer key(s): {', '.join(sorted(unknown_retailers))}"
        )
    allowed_retailer_keys = {"enabled", "interval_minutes", "incoming_interval_minutes",
                             "reconciliation_interval_minutes", "high_priority_interval_minutes",
                             "detail_batch_size", "status"}
    for retailer, settings in retailers.items():
        if not isinstance(settings, dict):
            raise ConfigurationError(f"retailers.{retailer} must be a mapping")
        unknown = set(settings) - allowed_retailer_keys
        if unknown:
            raise ConfigurationError(
                f"retailers.{retailer} has unknown setting(s): {', '.join(sorted(unknown))}"
            )
        if "enabled" not in settings or not isinstance(settings["enabled"], bool):
            raise ConfigurationError(f"retailers.{retailer}.enabled must be a boolean")
        for key in ("interval_minutes", "incoming_interval_minutes",
                    "reconciliation_interval_minutes", "high_priority_interval_minutes"):
            if key in settings:
                _positive(settings[key], f"retailers.{retailer}.{key}")
        if "detail_batch_size" in settings:
            _positive(settings["detail_batch_size"], f"retailers.{retailer}.detail_batch_size", int)
    mention_mode = os.getenv("DISCORD_ALERT_MENTION_MODE", "everyone").strip().casefold()
    if mention_mode not in {"none", "role", "everyone"}:
        raise ConfigurationError("DISCORD_ALERT_MENTION_MODE must be none, role, or everyone")
    role_value = os.getenv("DISCORD_ALERT_ROLE_ID", "").strip()
    if role_value and (not role_value.isdecimal() or int(role_value) <= 0):
        raise ConfigurationError("DISCORD_ALERT_ROLE_ID must be a positive Discord ID")
    role_id = int(role_value) if role_value else None
    if mention_mode == "role" and role_id is None:
        raise ConfigurationError("DISCORD_ALERT_ROLE_ID is required when mention mode is role")
    product_removed_value = os.getenv(
        "DISCORD_PRODUCT_REMOVED_ENABLED", "true"
    ).strip().casefold()
    if product_removed_value not in {"true", "false"}:
        raise ConfigurationError("DISCORD_PRODUCT_REMOVED_ENABLED must be true or false")
    notifications = NotificationConfig(
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL") or None,
        discord_admin_webhook_url=os.getenv("DISCORD_ADMIN_WEBHOOK_URL") or None,
        discord_alert_mention_mode=mention_mode,
        discord_alert_role_id=role_id,
        product_removed_enabled=product_removed_value == "true",
    )
    def discord_id(name: str) -> int | None:
        raw_value = os.getenv(name, "").strip()
        if not raw_value:
            return None
        if not raw_value.isdecimal() or int(raw_value) <= 0:
            raise ConfigurationError(f"{name} must be a positive Discord ID")
        return int(raw_value)

    def discord_ids(name: str) -> frozenset[int]:
        raw_value = os.getenv(name, "").strip()
        if not raw_value:
            return frozenset()
        values = [part.strip() for part in raw_value.split(",")]
        if any(not part.isdecimal() or int(part) <= 0 for part in values):
            raise ConfigurationError(f"{name} must be comma-separated positive Discord IDs")
        return frozenset(map(int, values))

    enabled_value = os.getenv("DISCORD_BOT_ENABLED", "false").strip().casefold()
    if enabled_value not in {"true", "false"}:
        raise ConfigurationError("DISCORD_BOT_ENABLED must be true or false")
    discord_bot = DiscordBotConfig(
        enabled=enabled_value == "true",
        token=os.getenv("DISCORD_BOT_TOKEN") or None,
        guild_id=discord_id("DISCORD_BOT_GUILD_ID"),
        allowed_user_ids=discord_ids("DISCORD_BOT_ALLOWED_USER_IDS"),
        allowed_role_ids=discord_ids("DISCORD_BOT_ALLOWED_ROLE_IDS"),
    )
    if discord_bot.enabled and not discord_bot.token:
        raise ConfigurationError("DISCORD_BOT_TOKEN is required when the bot is enabled")
    # Local import avoids coupling the configuration dataclasses to matching internals.
    from app.watchlist import load_watchlist
    watchlist = load_watchlist(watchlist_path)
    price_raw = raw.get("price_alerts", {})
    if not isinstance(price_raw, dict):
        raise ConfigurationError("price_alerts settings must be a mapping")
    enabled = price_raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigurationError("price_alerts.enabled must be a boolean")
    price_alerts = PriceAlertConfig(
        enabled=enabled,
        minimum_drop_percent=_positive(
            price_raw.get("minimum_drop_percent", 10), "minimum_drop_percent"
        ),
        minimum_drop_value=_positive(
            price_raw.get("minimum_drop_value", 5), "minimum_drop_value"
        ),
    )
    release_raw = raw.get("release_alerts", {})
    if not isinstance(release_raw, dict):
        raise ConfigurationError("release_alerts settings must be a mapping")
    release_enabled = release_raw.get("enabled", True)
    notify_existing = release_raw.get("notify_existing_on_upgrade", False)
    reminders = release_raw.get("reminders", ["24h", "1h"])
    if not isinstance(release_enabled, bool) or not isinstance(notify_existing, bool):
        raise ConfigurationError("release alert switches must be booleans")
    if not isinstance(reminders, list) or not all(isinstance(item, str) for item in reminders):
        raise ConfigurationError("release_alerts.reminders must be a list such as ['24h', '1h']")
    parsed_reminders: list[int] = []
    for item in reminders:
        match = __import__("re").fullmatch(r"\s*(\d+)\s*([hm])\s*", item, __import__("re").I)
        if not match or int(match.group(1)) <= 0:
            raise ConfigurationError(f"invalid release reminder: {item}")
        parsed_reminders.append(int(match.group(1)) * (3600 if match.group(2).lower() == "h" else 60))
    release_alerts = ReleaseAlertConfig(
        release_enabled, tuple(dict.fromkeys(parsed_reminders)), notify_existing
    )
    return AppConfig(
        monitor, database_path, logging_config, notifications, retailers, watchlist, price_alerts,
        release_alerts, backup_directory, backup_interval_hours, backup_initial_delay_seconds,
        backup_count, discord_bot,
    )

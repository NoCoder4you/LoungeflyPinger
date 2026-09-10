"""Validated YAML and environment configuration loading."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from dotenv import load_dotenv

if TYPE_CHECKING:
    from app.watchlist import Watchlist


class ConfigurationError(ValueError):
    """Raised when application configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    default_interval_minutes: float = 5
    request_timeout_seconds: float = 20
    concurrency_limit: int = 5
    max_retries: int = 3
    user_agent: str = "LoungeflyMonitor/0.1"
    missing_scan_threshold: int = 3


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


def _positive(value: Any, name: str, cast: type = float) -> Any:
    try:
        converted = cast(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if converted <= 0:
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
        path=Path(logging_raw.get("path", "logs/loungefly-monitor.log")),
        max_bytes=_positive(logging_raw.get("max_bytes", 5_242_880), "max_bytes", int),
        backup_count=_positive(logging_raw.get("backup_count", 3), "backup_count", int),
    )
    database_raw = raw.get("database", {})
    if not isinstance(database_raw, dict):
        raise ConfigurationError("database settings must be a mapping")
    database_path = Path(os.getenv("LOUNGEFLY_DATABASE_PATH", database_raw.get("path", "data/loungefly.db")))
    notifications_raw = raw.get("notifications", {})
    retailers = raw.get("retailers", {})
    if not isinstance(notifications_raw, dict) or not isinstance(retailers, dict):
        raise ConfigurationError("notifications and retailers must be mappings")
    notifications = NotificationConfig(
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL") or None,
        discord_admin_webhook_url=os.getenv("DISCORD_ADMIN_WEBHOOK_URL") or None,
    )
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
        release_alerts,
    )

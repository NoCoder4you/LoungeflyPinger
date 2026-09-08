"""Validated YAML and environment configuration loading."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when application configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    default_interval_minutes: float = 5
    request_timeout_seconds: float = 20
    concurrency_limit: int = 5
    max_retries: int = 3
    user_agent: str = "LoungeflyMonitor/0.1"


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str = "INFO"
    path: Path = Path("logs/loungefly-monitor.log")
    max_bytes: int = 5_242_880
    backup_count: int = 3


@dataclass(frozen=True, slots=True)
class AppConfig:
    monitor: MonitorConfig
    database_path: Path
    logging: LoggingConfig
    notifications: dict[str, Any] = field(default_factory=dict)
    retailers: dict[str, Any] = field(default_factory=dict)


def _positive(value: Any, name: str, cast: type = float) -> Any:
    try:
        converted = cast(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if converted <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return converted


def load_config(path: str | Path = "config/retailers.yaml", env_path: str | Path = ".env") -> AppConfig:
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
    )
    if not monitor.user_agent:
        raise ConfigurationError("user_agent must not be empty")
    level = os.getenv("LOUNGEFLY_LOG_LEVEL", logging_raw.get("level", "INFO")).upper()
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
    notifications = raw.get("notifications", {})
    retailers = raw.get("retailers", {})
    if not isinstance(notifications, dict) or not isinstance(retailers, dict):
        raise ConfigurationError("notifications and retailers must be mappings")
    return AppConfig(monitor, database_path, logging_config, notifications, retailers)

from pathlib import Path

import pytest

from app.config import ConfigurationError, load_config


def test_disney_store_uk_is_enabled_with_conservative_interval() -> None:
    config = load_config()

    assert config.retailers["disney_store_uk"] == {
        "enabled": True,
        "interval_minutes": 10,
        "status": "WORKING",
    }


def test_disney_mad_is_enabled_with_high_priority_interval() -> None:
    config = load_config()

    assert config.retailers["disney_mad_uk"] == {
        "enabled": True,
        "interval_minutes": 10,
        "status": "WORKING",
    }


def test_load_config(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("monitor:\n  concurrency_limit: 2\ndatabase:\n  path: custom.db\nretailers: {}\n", encoding="utf-8")
    config = load_config(path, tmp_path / ".env")
    assert config.monitor.concurrency_limit == 2
    assert config.database_path == Path("custom.db")
    assert config.monitor.missing_scan_threshold == 3
    assert config.price_alerts.minimum_drop_percent == 10


def test_price_alert_configuration(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(
        "price_alerts:\n  enabled: false\n  minimum_drop_percent: 15\n  minimum_drop_value: 7\n",
        encoding="utf-8",
    )
    config = load_config(path, tmp_path / ".env")
    assert not config.price_alerts.enabled
    assert config.price_alerts.minimum_drop_percent == 15
    assert config.price_alerts.minimum_drop_value == 7


def test_discord_webhooks_are_loaded_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/product")
    monkeypatch.setenv("DISCORD_ADMIN_WEBHOOK_URL", "https://discord.example/admin")

    config = load_config(path, tmp_path / ".env")

    assert config.notifications.discord_webhook_url == "https://discord.example/product"
    assert config.notifications.discord_admin_webhook_url == "https://discord.example/admin"


def test_invalid_config_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("monitor:\n  concurrency_limit: 0\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="greater than zero"):
        load_config(path, tmp_path / ".env")


@pytest.mark.parametrize("value", ['"yes"', "1", "null"])
def test_retailer_enabled_requires_yaml_boolean(tmp_path: Path, value: str) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(f"retailers:\n  geekcore:\n    enabled: {value}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="enabled must be a boolean"):
        load_config(path, tmp_path / ".env")


@pytest.mark.parametrize("field", [
    "interval_minutes", "incoming_interval_minutes", "reconciliation_interval_minutes",
    "high_priority_interval_minutes",
])
@pytest.mark.parametrize("value", ["0", "-1", ".inf", ".nan"])
def test_retailer_intervals_must_be_positive_and_finite(
    tmp_path: Path, field: str, value: str
) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(
        f"retailers:\n  geekcore:\n    enabled: true\n    {field}: {value}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigurationError, match=field):
        load_config(path, tmp_path / ".env")


def test_retailer_entry_and_keys_are_validated(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("retailers:\n  geekcore: enabled\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="must be a mapping"):
        load_config(path, tmp_path / ".env")
    path.write_text("retailers:\n  geekcore:\n    enabled: true\n    intervl_minutes: 5\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="unknown setting.*intervl_minutes"):
        load_config(path, tmp_path / ".env")


def test_unknown_retailer_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("retailers:\n  geek_core:\n    enabled: true\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="unknown retailer key.*geek_core"):
        load_config(path, tmp_path / ".env")


@pytest.mark.parametrize("level", ["10", "null"])
def test_non_string_logging_level_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    monkeypatch.delenv("LOUNGEFLY_LOG_LEVEL", raising=False)
    path = tmp_path / "settings.yaml"
    path.write_text(f"logging:\n  level: {level}\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="logging level must be a string"):
        load_config(path, tmp_path / ".env")

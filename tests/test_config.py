from pathlib import Path

import pytest

from app.config import ConfigurationError, load_config


def test_disney_store_uk_is_enabled_with_conservative_interval(tmp_path: Path) -> None:
    config = load_config(env_path=tmp_path / ".env")

    assert config.retailers["disney_store_uk"] == {
        "enabled": True,
        "interval_minutes": 10,
        "status": "WORKING",
    }


def test_disney_mad_is_enabled_with_high_priority_interval(tmp_path: Path) -> None:
    config = load_config(env_path=tmp_path / ".env")

    assert config.retailers["disney_mad_uk"] == {
        "enabled": True,
        "interval_minutes": 10,
        "status": "WORKING",
    }


def test_load_config(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("monitor:\n  concurrency_limit: 2\ndatabase:\n  path: custom.db\nretailers: {}\n", encoding="utf-8")
    config = load_config(path, env_path=tmp_path / ".env")
    assert config.monitor.concurrency_limit == 2
    assert config.database_path == Path("custom.db")
    assert config.monitor.missing_scan_threshold == 3
    assert config.monitor.retailer_job_timeout_seconds is None
    assert config.price_alerts.minimum_drop_percent == 10


def test_optional_retailer_job_timeout_configuration(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(
        "monitor:\n  retailer_job_timeout_seconds: 300\n",
        encoding="utf-8",
    )

    assert load_config(path, tmp_path / ".env").monitor.retailer_job_timeout_seconds == 300

    path.write_text(
        "monitor:\n  retailer_job_timeout_seconds: null\n",
        encoding="utf-8",
    )
    assert load_config(path, tmp_path / ".env").monitor.retailer_job_timeout_seconds is None


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


def test_discord_webhooks_load_from_json_not_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    path = tmp_path / "settings.yaml"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/ignored")
    initial = load_config(path, tmp_path / ".env")
    assert initial.notifications.discord_webhook_url is None
    json_path = tmp_path / "data" / "discord_channels.json"
    data = json.loads(json_path.read_text())
    data["fallback_webhooks"]["product"] = "https://discord.example/product"
    data["fallback_webhooks"]["admin"] = "https://discord.example/admin"
    json_path.write_text(json.dumps(data))
    loaded = load_config(path, tmp_path / ".env")
    assert loaded.notifications.discord_webhook_url == "https://discord.example/product"
    assert loaded.notifications.discord_admin_webhook_url == "https://discord.example/admin"


def test_product_removed_notifications_can_be_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("DISCORD_PRODUCT_REMOVED_ENABLED", "false")

    assert not load_config(path, tmp_path / ".env").notifications.product_removed_enabled


def test_invalid_product_removed_notification_toggle_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("DISCORD_PRODUCT_REMOVED_ENABLED", "yes")

    with pytest.raises(ConfigurationError, match="DISCORD_PRODUCT_REMOVED_ENABLED"):
        load_config(path, tmp_path / ".env")


def test_sold_out_notifications_can_be_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("DISCORD_SOLD_OUT_ENABLED", "false")

    assert not load_config(path, tmp_path / ".env").notifications.sold_out_enabled


def test_invalid_sold_out_notification_toggle_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("DISCORD_SOLD_OUT_ENABLED", "yes")

    with pytest.raises(ConfigurationError, match="DISCORD_SOLD_OUT_ENABLED"):
        load_config(path, tmp_path / ".env")


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


def test_discord_bot_defaults_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DISCORD_BOT_ENABLED", "DISCORD_BOT_TOKEN", "DISCORD_BOT_GUILD_ID",
                 "DISCORD_BOT_ALLOWED_USER_IDS", "DISCORD_BOT_ALLOWED_ROLE_IDS"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "settings.yaml"; path.write_text("{}", encoding="utf-8")
    assert not load_config(path, tmp_path / ".env").discord_bot.enabled


def test_discord_bot_configuration_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "settings.yaml"; path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("DISCORD_BOT_ENABLED", "true")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret")
    monkeypatch.setenv("DISCORD_BOT_GUILD_ID", "123")
    monkeypatch.setenv("DISCORD_BOT_ALLOWED_USER_IDS", "1, 2")
    monkeypatch.setenv("DISCORD_BOT_ALLOWED_ROLE_IDS", "3")
    config = load_config(path, tmp_path / ".env").discord_bot
    assert config.guild_id == 123
    assert config.allowed_user_ids == frozenset({1, 2})
    assert config.allowed_role_ids == frozenset({3})

def test_event_specific_webhooks_are_loaded_from_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from app.models import AlertType
    path = tmp_path / "settings.yaml"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("DISCORD_WEBHOOK_RESTOCK_URL", "https://discord.example/ignored")
    config = load_config(path, tmp_path / ".env")
    assert not config.notifications.discord_type_webhooks
    json_path = tmp_path / "data" / "discord_channels.json"
    data = json.loads(json_path.read_text())
    data["channels"]["lf-restocks"]["webhook_url"] = "https://discord.example/restocks"
    json_path.write_text(json.dumps(data))
    loaded = load_config(path, tmp_path / ".env")
    assert loaded.notifications.discord_type_webhooks == {
        AlertType.RESTOCK: "https://discord.example/restocks",
        AlertType.AVAILABILITY: "https://discord.example/restocks",
    }


def test_invalid_json_webhook_raises_config_error_without_leaking_secret(
    tmp_path: Path
) -> None:
    import json
    path = tmp_path / "settings.yaml"
    path.write_text("{}", encoding="utf-8")
    load_config(path, tmp_path / ".env")
    json_path = tmp_path / "data" / "discord_channels.json"
    data = json.loads(json_path.read_text())
    data["channels"]["lf-restocks"]["webhook_url"] = "http://bad.example/secret"
    json_path.write_text(json.dumps(data))
    with pytest.raises(ConfigurationError) as exc:
        load_config(path, tmp_path / ".env")
    assert "secret" not in str(exc.value)


def test_auto_channel_setup_defaults_on_and_can_be_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.delenv("DISCORD_AUTO_CREATE_ALERT_CHANNELS", raising=False)
    assert load_config(path, tmp_path / ".env").discord_bot.auto_create_alert_channels
    monkeypatch.setenv("DISCORD_AUTO_CREATE_ALERT_CHANNELS", "false")
    assert not load_config(path, tmp_path / ".env").discord_bot.auto_create_alert_channels
    monkeypatch.setenv("DISCORD_AUTO_CREATE_ALERT_CHANNELS", "invalid")
    with pytest.raises(ConfigurationError, match="DISCORD_AUTO_CREATE_ALERT_CHANNELS"):
        load_config(path, tmp_path / ".env")

def test_mention_defaults_to_category_role_without_everyone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_ALERT_MENTION_MODE", raising=False)
    monkeypatch.delenv("DISCORD_ALERT_ROLE_ID", raising=False)
    config = load_config(env_path=tmp_path / ".env")
    assert config.notifications.discord_alert_mention_mode == "category"
    assert config.notifications.discord_alert_role_id is None


def test_explicit_none_mention_mode_remains_supported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_ALERT_MENTION_MODE", "none")
    config = load_config(env_path=tmp_path / ".env")
    assert config.notifications.discord_alert_mention_mode == "none"

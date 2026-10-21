import json
import os
from pathlib import Path

import pytest

from app.models import AlertType
from app.notifications.channel_config import (
    ChannelConfigurationError, ChannelRoutingStore,
)


def test_bootstraps_complete_secret_private_json(tmp_path: Path) -> None:
    path = tmp_path / "data" / "discord_channels.json"
    store = ChannelRoutingStore(path)
    store.load_or_create()
    data = json.loads(path.read_text())
    assert data["version"] == 1
    assert data["category_name"] == "Loungefly Alerts"
    assert set(data["channels"]) == set(store.channel_ids)
    assert all(item["channel_id"] is None for item in data["channels"].values())
    assert os.stat(path).st_mode & 0o077 == 0


def test_manual_json_edit_switches_channel_ids_and_webhook_urls(tmp_path: Path) -> None:
    path = tmp_path / "discord_channels.json"
    store = ChannelRoutingStore(path)
    store.load_or_create()
    data = json.loads(path.read_text())
    data["channels"]["lf-restocks"] = {
        "channel_id": 1122334455,
        "webhook_url": "https://discord.com/api/webhooks/123/token",
    }
    data["fallback_webhooks"]["admin"] = "https://discord.com/api/webhooks/admin/token"
    path.write_text(json.dumps(data))
    store.load_or_create()
    assert store.channel_ids["lf-restocks"] == 1122334455
    assert store.type_webhooks[AlertType.RESTOCK] == "https://discord.com/api/webhooks/123/token"
    assert store.type_webhooks[AlertType.AVAILABILITY] == "https://discord.com/api/webhooks/123/token"
    assert store.fallback_admin_webhook == "https://discord.com/api/webhooks/admin/token"
    assert os.stat(path).st_mode & 0o077 == 0


def test_auto_ids_only_fill_missing_and_preserve_manual_entries(tmp_path: Path) -> None:
    path = tmp_path / "discord_channels.json"
    store = ChannelRoutingStore(path)
    store.load_or_create()
    data = json.loads(path.read_text())
    data["channels"]["lf-restocks"]["channel_id"] = 9999
    path.write_text(json.dumps(data))
    store.save_discovered_ids({"lf-restocks": 1234, "lf-new-products": 5678})
    updated = json.loads(path.read_text())
    assert updated["channels"]["lf-restocks"]["channel_id"] == 9999
    assert updated["channels"]["lf-new-products"]["channel_id"] == 5678


@pytest.mark.parametrize("bad_value", [0, -3, "123", True])
def test_reject_invalid_channel_id_without_modifying_file(tmp_path: Path, bad_value) -> None:
    path = tmp_path / "discord_channels.json"
    store = ChannelRoutingStore(path)
    store.load_or_create()
    data = json.loads(path.read_text())
    data["channels"]["lf-releases"]["channel_id"] = bad_value
    path.write_text(json.dumps(data))
    with pytest.raises(ChannelConfigurationError, match="channel_id"):
        store.load_or_create()


def test_invalid_webhook_does_not_leak_token(tmp_path: Path) -> None:
    path = tmp_path / "discord_channels.json"
    store = ChannelRoutingStore(path)
    store.load_or_create()
    data = json.loads(path.read_text())
    data["channels"]["lf-preorders"]["webhook_url"] = "http://unsafe.invalid/token-secret"
    path.write_text(json.dumps(data))
    with pytest.raises(ChannelConfigurationError) as exc:
        store.load_or_create()
    assert "token-secret" not in str(exc.value)

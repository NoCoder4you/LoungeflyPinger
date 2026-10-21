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


def test_auto_created_roles_and_webhooks_persist_without_overwriting_manual_routes(
    tmp_path: Path
) -> None:
    path = tmp_path / "data" / "discord_channels.json"
    store = ChannelRoutingStore(path)
    store.load_or_create()
    doc = json.loads(path.read_text())
    doc["channels"]["lf-preorders"]["webhook_url"] = "https://discord.example/manual"
    doc["channels"]["lf-restocks"]["role_id"] = 9999
    path.write_text(json.dumps(doc))

    store.save_discovered_resources(
        channel_ids={"lf-restocks": 100, "lf-preorders": 200},
        role_ids={"lf-restocks": 111, "lf-preorders": 222},
        managed_webhooks={
            "lf-restocks": (100, "https://discord.example/restocks"),
            "lf-preorders": (200, "https://discord.example/automatic"),
        },
    )
    doc = json.loads(path.read_text())
    assert doc["channels"]["lf-restocks"]["role_id"] == 9999
    assert doc["channels"]["lf-preorders"]["role_id"] == 222
    assert doc["channels"]["lf-preorders"]["webhook_url"] == "https://discord.example/manual"
    assert doc["channels"]["lf-restocks"]["webhook_url"] == "https://discord.example/restocks"
    assert doc["channels"]["lf-restocks"]["webhook_channel_id"] == 100


def test_moving_channel_rotates_managed_webhook_but_keeps_manual_url(tmp_path: Path) -> None:
    path = tmp_path / "discord_channels.json"
    store = ChannelRoutingStore(path)
    store.load_or_create()
    store.save_discovered_resources(
        channel_ids={"lf-restocks": 100},
        managed_webhooks={"lf-restocks": (100, "https://discord.example/original")},
    )
    doc = json.loads(path.read_text())
    doc["channels"]["lf-restocks"]["channel_id"] = 200
    path.write_text(json.dumps(doc))
    store.load_or_create()
    assert AlertType.RESTOCK not in store.type_webhooks
    store.save_discovered_resources(
        managed_webhooks={"lf-restocks": (200, "https://discord.example/new")}
    )
    assert store.type_webhooks[AlertType.RESTOCK] == "https://discord.example/new"


def test_old_json_without_role_and_webhook_metadata_is_upgraded(tmp_path: Path) -> None:
    path = tmp_path / "discord_channels.json"
    store = ChannelRoutingStore(path)
    store.load_or_create()
    doc = json.loads(path.read_text())
    for row in doc["channels"].values():
        row.pop("role_id")
        row.pop("webhook_channel_id")
    path.write_text(json.dumps(doc))
    store.load_or_create()
    assert all(role_id is None for role_id in store.role_ids.values())
    store.save_discovered_resources(role_ids={"lf-restocks": 123})
    assert json.loads(path.read_text())["channels"]["lf-restocks"]["role_id"] == 123

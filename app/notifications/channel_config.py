"""Private JSON store for Discord channel IDs and webhook destinations."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from app.models import AlertType
from app.notifications.channel_setup import CATEGORY_NAME, CHANNEL_ALERT_TYPES

LOGGER = logging.getLogger("monitor.discord_channel_config")
DEFAULT_PATH = Path("data/discord_channels.json")


class ChannelConfigurationError(ValueError):
    """Bad operator-edited routing configuration without exposing secrets."""


def _default_document() -> dict:
    return {
        "version": 1,
        "category_name": CATEGORY_NAME,
        "fallback_webhooks": {"product": None, "admin": None},
        "channels": {
            name: {"channel_id": None, "webhook_url": None}
            for name in CHANNEL_ALERT_TYPES
        },
    }


def _webhook(value: object, label: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ChannelConfigurationError(f"{label} must be an HTTPS URL or null")
    stripped = value.strip()
    try:
        parsed = urlsplit(stripped)
        valid = parsed.scheme == "https" and bool(parsed.hostname)
    except ValueError:
        valid = False
    if not valid:
        # Never include webhook values in errors or logs.
        raise ChannelConfigurationError(f"{label} must be an HTTPS URL or null")
    return stripped


def _validate(doc: object) -> dict:
    if not isinstance(doc, dict):
        raise ChannelConfigurationError("Discord channel configuration must be a JSON object")
    if set(doc) != {"version", "category_name", "fallback_webhooks", "channels"}:
        raise ChannelConfigurationError("Discord channel configuration has missing or unknown keys")
    if type(doc["version"]) is not int or doc["version"] != 1:
        raise ChannelConfigurationError("Unsupported Discord channel configuration version")
    category = doc["category_name"]
    if not isinstance(category, str) or not 1 <= len(category.strip()) <= 100:
        raise ChannelConfigurationError("category_name must be a non-empty channel category")
    fallback = doc["fallback_webhooks"]
    if not isinstance(fallback, dict) or set(fallback) != {"product", "admin"}:
        raise ChannelConfigurationError("fallback_webhooks must contain product and admin")
    fallbacks = {
        key: _webhook(value, f"fallback_webhooks.{key}")
        for key, value in fallback.items()
    }
    channels = doc["channels"]
    if not isinstance(channels, dict):
        raise ChannelConfigurationError("channels must be a JSON object")
    unknown = set(channels) - set(CHANNEL_ALERT_TYPES)
    if unknown:
        raise ChannelConfigurationError("Unknown Discord channel key(s): " + ", ".join(sorted(unknown)))
    normalized = {}
    for name in CHANNEL_ALERT_TYPES:
        route = channels.get(name, {"channel_id": None, "webhook_url": None})
        if not isinstance(route, dict) or set(route) != {"channel_id", "webhook_url"}:
            raise ChannelConfigurationError(f"channels.{name} must contain channel_id and webhook_url")
        channel_id = route["channel_id"]
        if channel_id is not None and (type(channel_id) is not int or channel_id <= 0):
            raise ChannelConfigurationError(f"channels.{name}.channel_id must be a positive integer or null")
        normalized[name] = {
            "channel_id": channel_id,
            "webhook_url": _webhook(route["webhook_url"], f"channels.{name}.webhook_url"),
        }
    return {
        "version": 1,
        "category_name": category.strip(),
        "fallback_webhooks": fallbacks,
        "channels": normalized,
    }


class ChannelRoutingStore:
    """Auto-bootstrap channel configuration and persist IDs without changing overrides."""

    def __init__(self, path: str | Path = DEFAULT_PATH) -> None:
        self.path = Path(path)
        self._document = _default_document()

    def load_or_create(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # 'x' prevents overwriting an operator-edited file at startup.
            with self.path.open("x", encoding="utf-8") as stream:
                os.chmod(self.path, 0o600)
                json.dump(_default_document(), stream, indent=2)
                stream.write("\n")
        except FileExistsError:
            pass
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, ValueError) as exc:
            raise ChannelConfigurationError("Unable to read Discord channel JSON configuration") from exc
        self._document = _validate(value)
        # Tokens are secrets, so remove group/other access if inherited from an edit.
        os.chmod(self.path, 0o600)

    @property
    def category_name(self) -> str:
        return self._document["category_name"]

    @property
    def channel_ids(self) -> dict[str, int | None]:
        return {name: item["channel_id"] for name, item in self._document["channels"].items()}

    @property
    def fallback_product_webhook(self) -> str | None:
        return self._document["fallback_webhooks"]["product"]

    @property
    def fallback_admin_webhook(self) -> str | None:
        return self._document["fallback_webhooks"]["admin"]

    @property
    def type_webhooks(self) -> dict[AlertType, str]:
        return {
            alert_type: item["webhook_url"]
            for name, item in self._document["channels"].items()
            if item["webhook_url"]
            for alert_type in CHANNEL_ALERT_TYPES[name]
        }

    def save_discovered_ids(self, discovered: dict[str, int]) -> None:
        """Only fill missing IDs, reloading first to protect manual overrides."""
        self.load_or_create()
        changed = False
        for name, channel_id in discovered.items():
            if name in self._document["channels"] and self._document["channels"][name]["channel_id"] is None:
                self._document["channels"][name]["channel_id"] = channel_id
                changed = True
        if not changed:
            return
        fd, temp_path = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self._document, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

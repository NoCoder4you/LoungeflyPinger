"""Tests for automatic Discord alert channel provisioning."""

from types import SimpleNamespace

import discord
import pytest

from app.models import AlertType
from app.notifications.channel_setup import (
    CATEGORY_NAME, CHANNEL_ALERT_TYPES, ensure_alert_channels,
)


class FakeCategory:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeChannel:
    def __init__(self, name: str, channel_id: int, category=None) -> None:
        self.name, self.id, self.category = name, channel_id, category


class FakeGuild:
    def __init__(self) -> None:
        self.id = 9988
        self.categories: list[FakeCategory] = []
        self.text_channels: list[FakeChannel] = []
        self.created_categories = 0
        self.created_channels = 0
        self.fail_channel: str | None = None

    async def create_category(self, name, *, reason):
        self.created_categories += 1
        category = FakeCategory(name)
        self.categories.append(category)
        return category

    async def create_text_channel(self, name, *, category, reason):
        if name == self.fail_channel:
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"),
                {"message": "Missing Permissions", "code": 50013},
            )
        self.created_channels += 1
        channel = FakeChannel(name, self.created_channels + 1000, category)
        self.text_channels.append(channel)
        return channel


def test_all_alert_types_have_exactly_one_auto_channel() -> None:
    flattened = [alert_type for types in CHANNEL_ALERT_TYPES.values() for alert_type in types]
    assert set(flattened) == set(AlertType)
    assert len(flattened) == len(set(flattened))


@pytest.mark.asyncio
async def test_channel_setup_creates_once_and_is_idempotent() -> None:
    guild = FakeGuild()
    first = await ensure_alert_channels(guild)
    assert len(first) == len(AlertType)
    assert guild.created_categories == 1
    assert guild.created_channels == len(CHANNEL_ALERT_TYPES)
    assert guild.categories[0].name == CATEGORY_NAME
    second = await ensure_alert_channels(guild)
    assert second == first
    assert guild.created_categories == 1
    assert guild.created_channels == len(CHANNEL_ALERT_TYPES)
    assert first[AlertType.RESTOCK] is first[AlertType.AVAILABILITY]


@pytest.mark.asyncio
async def test_setup_reuses_existing_channels_and_skips_duplicates() -> None:
    guild = FakeGuild()
    existing = FakeChannel("lf-restocks", 42)
    guild.text_channels = [
        existing, FakeChannel("lf-preorders", 100), FakeChannel("lf-preorders", 101),
    ]
    routes = await ensure_alert_channels(guild)
    assert routes[AlertType.RESTOCK] is existing
    assert routes[AlertType.AVAILABILITY] is existing
    assert AlertType.PREORDER_OPEN not in routes
    assert guild.created_channels == len(CHANNEL_ALERT_TYPES) - 2


@pytest.mark.asyncio
async def test_missing_permissions_dont_prevent_other_channels() -> None:
    guild = FakeGuild()
    guild.fail_channel = "lf-releases"
    routes = await ensure_alert_channels(guild)
    assert AlertType.RELEASING_SOON not in routes
    assert AlertType.RESTOCK in routes


@pytest.mark.asyncio
async def test_configured_channel_ids_take_precedence_over_names() -> None:
    guild = FakeGuild()
    custom = FakeChannel("my-personal-restock-channel", 8765)
    guild.text_channels = [custom]
    routes = await ensure_alert_channels(guild, existing_ids={"lf-restocks": 8765})
    assert routes[AlertType.RESTOCK] is custom
    assert guild.created_channels == len(CHANNEL_ALERT_TYPES) - 1


@pytest.mark.asyncio
async def test_stale_manual_channel_id_is_not_silently_replaced() -> None:
    guild = FakeGuild()
    guild.text_channels = [FakeChannel("lf-restocks", 100)]
    routes = await ensure_alert_channels(guild, existing_ids={"lf-restocks": 999})
    assert AlertType.RESTOCK not in routes
    assert len([ch for ch in guild.text_channels if ch.name == "lf-restocks"]) == 1


@pytest.mark.asyncio
async def test_disabled_auto_creation_still_resolves_manual_channel_ids() -> None:
    guild = FakeGuild()
    existing = FakeChannel("custom", 1234)
    guild.text_channels.append(existing)
    routes = await ensure_alert_channels(
        guild, existing_ids={"lf-restocks": 1234}, create_missing=False
    )
    assert routes[AlertType.RESTOCK] is existing
    assert guild.created_categories == 0
    assert guild.created_channels == 0

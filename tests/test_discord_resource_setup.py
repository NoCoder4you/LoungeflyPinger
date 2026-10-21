"""Role and webhook provisioning works repeatedly without altering other resources."""

from types import SimpleNamespace

import discord
import pytest

from app.notifications.resource_setup import (
    ROLE_NAMES, WEBHOOK_NAMES, ensure_alert_roles, ensure_alert_webhooks,
)


class FakeGuild:
    id = 88

    def __init__(self):
        self.roles = []
        self.created = 0

    async def create_role(self, *, name, mentionable, reason):
        self.created += 1
        role = SimpleNamespace(id=1000 + self.created, name=name)
        self.roles.append(role)
        return role


class FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.hooks = []
        self.created = 0

    async def webhooks(self):
        return list(self.hooks)

    async def create_webhook(self, *, name, reason):
        self.created += 1
        hook = SimpleNamespace(
            id=self.created, name=name, user=SimpleNamespace(id=55),
            url=f"https://discord.example/{self.id}/{self.created}",
        )
        self.hooks.append(hook)
        return hook


@pytest.mark.asyncio
async def test_roles_are_created_once_and_reused_without_renaming() -> None:
    guild = FakeGuild()
    first = await ensure_alert_roles(guild)
    assert set(first) == set(ROLE_NAMES)
    assert guild.created == len(ROLE_NAMES)
    again = await ensure_alert_roles(guild)
    assert again == first
    assert guild.created == len(ROLE_NAMES)
    manual = SimpleNamespace(id=5555, name="Custom role")
    guild.roles.append(manual)
    found = await ensure_alert_roles(guild, configured_ids={"lf-restocks": 5555})
    assert found["lf-restocks"] is manual
    assert guild.created == len(ROLE_NAMES)


@pytest.mark.asyncio
async def test_stale_manual_roles_are_not_silently_replaced() -> None:
    guild = FakeGuild()
    resolved = await ensure_alert_roles(
        guild, configured_ids={"lf-restocks": 987654321},
    )
    assert "lf-restocks" not in resolved
    assert guild.created == len(ROLE_NAMES) - 1


@pytest.mark.asyncio
async def test_webhooks_created_once_with_separate_names_and_bot_ownership() -> None:
    channels = {
        "lf-restocks": FakeChannel(500),
        "lf-releases": FakeChannel(501),
    }
    first = await ensure_alert_webhooks(channels, bot_user_id=55)
    assert first["lf-restocks"] == (500, "https://discord.example/500/1")
    assert channels["lf-restocks"].hooks[0].name == WEBHOOK_NAMES["lf-restocks"]
    assert len(set(WEBHOOK_NAMES.values())) == len(WEBHOOK_NAMES)
    second = await ensure_alert_webhooks(
        channels, bot_user_id=55,
        configured_urls={key: value[1] for key, value in first.items()},
        managed_channel_ids={key: value[0] for key, value in first.items()},
    )
    assert second == first
    assert all(ch.created == 1 for ch in channels.values())


@pytest.mark.asyncio
async def test_manual_urls_skip_creation_and_bot_does_not_reuse_other_owners() -> None:
    channel = FakeChannel(500)
    channel.hooks.append(SimpleNamespace(
        id=9, name=WEBHOOK_NAMES["lf-restocks"],
        user=SimpleNamespace(id=123), url="https://discord.example/other",
    ))
    assert await ensure_alert_webhooks(
        {"lf-restocks": channel}, bot_user_id=55,
        configured_urls={"lf-restocks": "https://discord.example/manual"},
    ) == {}
    assert channel.created == 0
    result = await ensure_alert_webhooks({"lf-restocks": channel}, bot_user_id=55)
    assert result["lf-restocks"] == (500, "https://discord.example/500/1")


@pytest.mark.asyncio
async def test_missing_manage_webhooks_isolated_per_channel() -> None:
    failing = FakeChannel(100)
    async def denied():
        raise discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            {"message": "Missing Permissions", "code": 50013},
        )
    failing.webhooks = denied
    healthy = FakeChannel(200)
    result = await ensure_alert_webhooks(
        {"lf-restocks": failing, "lf-releases": healthy}, bot_user_id=55,
    )
    assert "lf-restocks" not in result
    assert "lf-releases" in result

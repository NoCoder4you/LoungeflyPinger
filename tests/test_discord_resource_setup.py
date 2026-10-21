"""Role and webhook provisioning works repeatedly without altering other resources."""

from types import SimpleNamespace

import discord
import pytest

from app.notifications.resource_setup import (
    ROLE_NAMES, WEBHOOK_NAMES, LEGACY_ROLE_NAMES, LEGACY_WEBHOOK_NAMES,
    ensure_alert_roles, ensure_alert_webhooks,
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


@pytest.mark.asyncio
async def test_role_mentions_are_opt_in_and_creation_is_mentionable() -> None:
    guild = FakeGuild()
    original = guild.create_role
    mentionable_values = []

    async def create(*, name, mentionable, reason):
        mentionable_values.append(mentionable)
        return await original(name=name, mentionable=mentionable, reason=reason)

    guild.create_role = create
    await ensure_alert_roles(guild)
    assert mentionable_values == [True] * len(ROLE_NAMES)


class LegacyRole:
    def __init__(self, role_id: int, name: str) -> None:
        self.id = role_id
        self.name = name
        self.edits = 0

    async def edit(self, *, name: str, reason: str):
        self.edits += 1
        self.name = name
        return self


class LegacyWebhook:
    def __init__(self, name: str, channel_id: int) -> None:
        self.name = name
        self.id = 777
        self.user = SimpleNamespace(id=55)
        self.url = f"https://discord.example/{channel_id}/777"
        self.edits = 0

    async def edit(self, *, name: str, reason: str):
        self.edits += 1
        self.name = name
        return self


def test_resource_names_have_no_brand_prefixes() -> None:
    assert ROLE_NAMES["lf-new-products"] == "New Products"
    assert ROLE_NAMES["lf-restocks"] == "Restocks"
    assert WEBHOOK_NAMES == ROLE_NAMES
    assert all(not name.startswith("LF Alerts | ") for name in ROLE_NAMES.values())
    assert all(not name.startswith("Loungefly ") for name in WEBHOOK_NAMES.values())


@pytest.mark.asyncio
async def test_legacy_roles_renamed_and_reused_without_creating_duplicates() -> None:
    guild = FakeGuild()
    old = LegacyRole(4001, LEGACY_ROLE_NAMES["lf-restocks"])
    guild.roles.append(old)
    first = await ensure_alert_roles(guild)
    assert first["lf-restocks"] is old
    assert old.name == "Restocks"
    assert old.edits == 1
    assert guild.created == len(ROLE_NAMES) - 1
    second = await ensure_alert_roles(guild, configured_ids={"lf-restocks": old.id})
    assert second["lf-restocks"] is old
    assert old.edits == 1
    assert guild.created == len(ROLE_NAMES) - 1


@pytest.mark.asyncio
async def test_legacy_owned_webhook_renamed_and_reused_without_duplication() -> None:
    channel = FakeChannel(500)
    old = LegacyWebhook(LEGACY_WEBHOOK_NAMES["lf-restocks"], channel.id)
    channel.hooks.append(old)
    first = await ensure_alert_webhooks({"lf-restocks": channel}, bot_user_id=55)
    assert first["lf-restocks"] == (500, old.url)
    assert old.name == "Restocks"
    assert old.edits == 1
    assert channel.created == 0
    second = await ensure_alert_webhooks(
        {"lf-restocks": channel}, bot_user_id=55,
        configured_urls={"lf-restocks": old.url},
        managed_channel_ids={"lf-restocks": channel.id},
    )
    assert second == first
    assert old.edits == 1
    assert channel.created == 0

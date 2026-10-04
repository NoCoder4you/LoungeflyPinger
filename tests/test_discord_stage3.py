import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from app.config import AppConfig, DiscordBotConfig, LoggingConfig, MonitorConfig
from app.discord_control import (
    DeleteConfirmation,
    DiscordControlService,
    RetailerConfirmation,
)
from app.logging_config import _DiscordVoiceWarningFilter


def make_control(guild_id: int | None = None) -> DiscordControlService:
    config = DiscordBotConfig(enabled=True, token="test-token", guild_id=guild_id)
    app_config = AppConfig(MonitorConfig(), Path("test.db"), LoggingConfig())
    return DiscordControlService(config, Mock(), Mock(), app_config, retailers=Mock())


def test_discord_control_uses_only_required_gateway_intents():
    config = DiscordBotConfig(enabled=True, token="test-token")
    app_config = AppConfig(MonitorConfig(), Path("test.db"), LoggingConfig())

    service = DiscordControlService(config, Mock(), Mock(), app_config)

    assert service.client is not None
    assert service.client.intents.guilds is True
    assert service.client.intents.message_content is False
    assert service.client.intents.members is False
    assert service.client.intents.presences is False


def test_discord_control_registers_on_ready_event_and_expected_commands():
    service = make_control(guild_id=123456789)

    assert service.client is not None
    assert service.tree is not None
    assert service.client.on_ready == service.on_ready
    assert "_on_ready" not in service.client.__dict__
    command_names = {
        command.name
        for command in service.tree.get_commands(guild=discord.Object(id=123456789))
    }
    assert command_names == {"watch", "retailer", "product", "alerts", "status", "retailers"}
    alerts = service.tree.get_command("alerts", guild=discord.Object(id=123456789))
    assert isinstance(alerts, discord.app_commands.Group)
    assert {command.name for command in alerts.commands} == {"recent", "list", "enable", "disable"}


@pytest.mark.asyncio
async def test_ready_synchronizes_guild_commands_once():
    service = make_control(guild_id=123456789)
    assert service.tree is not None
    service.tree.sync = AsyncMock(return_value=[Mock(), Mock()])

    await service.on_ready()
    await service.on_ready()

    service.tree.sync.assert_awaited_once()
    guild = service.tree.sync.await_args.kwargs["guild"]
    assert isinstance(guild, discord.Object)
    assert guild.id == 123456789
    assert service._commands_synced is True
    assert service._commands_synced_count == 2


@pytest.mark.asyncio
async def test_ready_synchronizes_global_commands_without_guild_argument():
    service = make_control()
    assert service.tree is not None
    service.tree.sync = AsyncMock(return_value=[Mock()])

    await service.on_ready()

    service.tree.sync.assert_awaited_once_with()
    assert service._commands_synced is True


@pytest.mark.asyncio
async def test_failed_sync_is_contained_and_retried_on_next_ready(caplog):
    service = make_control(guild_id=123456789)
    assert service.tree is not None
    service.tree.sync = AsyncMock(side_effect=[RuntimeError("Discord unavailable"), [Mock()]])

    with caplog.at_level(logging.INFO, logger="monitor.discord_control"):
        await service.on_ready()
    assert service._commands_synced is False
    assert "discord_command_sync_failed" in caplog.text

    await service.on_ready()

    assert service.tree.sync.await_count == 2
    assert service._commands_synced is True
    assert service._commands_synced_count == 1


@pytest.mark.asyncio
async def test_ready_warns_when_configured_guild_is_not_visible(caplog):
    service = make_control(guild_id=123456789)
    assert service.tree is not None
    service.tree.sync = AsyncMock(return_value=[])

    with caplog.at_level(logging.WARNING, logger="monitor.discord_control"):
        await service.on_ready()

    assert "discord_configured_guild_not_found" in caplog.text


def test_discord_voice_warning_filter_is_narrowly_scoped():
    warning_filter = _DiscordVoiceWarningFilter()
    pynacl_warning = logging.LogRecord(
        "discord.client", logging.WARNING, "", 0,
        "PyNaCl is not installed, voice will NOT be supported", (), None,
    )
    other_warning = logging.LogRecord(
        "discord.client", logging.WARNING, "", 0,
        "Discord gateway connection failed", (), None,
    )

    assert warning_filter.filter(pynacl_warning) is False
    assert warning_filter.filter(other_warning) is True


@pytest.mark.asyncio
async def test_retailer_confirmation_rechecks_current_authorization():
    service = SimpleNamespace(authorized=Mock(return_value=False), audit_retailer=AsyncMock())
    interaction = SimpleNamespace(user=SimpleNamespace(id=42), response=SimpleNamespace(
        send_message=AsyncMock()))
    view = RetailerConfirmation(service, 42, "shop", "disable")

    assert await view.interaction_check(interaction) is False
    service.authorized.assert_called_once_with(interaction)
    service.audit_retailer.assert_awaited_once()
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_watch_deletion_confirmation_rechecks_current_authorization():
    manager = SimpleNamespace(get=Mock(return_value=None))
    service = SimpleNamespace(authorized=Mock(return_value=False), audit=AsyncMock(), manager=manager)
    interaction = SimpleNamespace(user=SimpleNamespace(id=42), response=SimpleNamespace(
        send_message=AsyncMock()))
    view = DeleteConfirmation(service, 42, 9)

    assert await view.interaction_check(interaction) is False
    service.audit.assert_awaited_once()

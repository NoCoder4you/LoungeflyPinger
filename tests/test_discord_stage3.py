import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.config import AppConfig, DiscordBotConfig, LoggingConfig, MonitorConfig
from app.discord_control import (
    DeleteConfirmation,
    DiscordControlService,
    RetailerConfirmation,
)
from app.logging_config import _DiscordVoiceWarningFilter


def test_discord_control_uses_only_required_gateway_intents():
    config = DiscordBotConfig(enabled=True, token="test-token")
    app_config = AppConfig(MonitorConfig(), Path("test.db"), LoggingConfig())

    service = DiscordControlService(config, Mock(), Mock(), app_config)

    assert service.client is not None
    assert service.client.intents.guilds is True
    assert service.client.intents.message_content is False
    assert service.client.intents.members is False
    assert service.client.intents.presences is False


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

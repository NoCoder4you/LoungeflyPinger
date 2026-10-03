from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.discord_control import DeleteConfirmation, RetailerConfirmation


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

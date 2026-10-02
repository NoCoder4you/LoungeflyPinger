import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.config import AppConfig, DiscordBotConfig, LoggingConfig, MonitorConfig
from app.database import Database
from app.discord_control import DiscordControlService
from app.models import Availability, Product
from app.services.monitor_service import MonitorService
from app.services.watchlist_manager import WatchlistManager
from app.watchlist import WatchRule, Watchlist


@pytest_asyncio.fixture
async def manager(tmp_path: Path):
    database = Database(tmp_path / "watch.db")
    await database.connect(); await database.initialize()
    value = WatchlistManager(database)
    await value.initialize(Watchlist((WatchRule("Stitch", keywords=("stitch",)),)))
    try: yield value
    finally: await database.close()


@pytest.mark.asyncio
async def test_bootstrap_runs_exactly_once_and_persists(tmp_path: Path) -> None:
    database = Database(tmp_path / "watch.db")
    await database.connect(); await database.initialize()
    first = WatchlistManager(database)
    await first.initialize(Watchlist((WatchRule("Original"),)))
    await first.delete(first.items[0].id)
    second = WatchlistManager(database)
    await second.initialize(Watchlist((WatchRule("Must not return"),)))
    assert second.items == ()
    await database.close()


@pytest.mark.asyncio
async def test_crud_enable_disable_validation_and_reconstruction(manager: WatchlistManager) -> None:
    added = await manager.add({"name": "Pooh", "keywords": ["pooh"], "max_price": "50"}, "123")
    edited = await manager.edit(added.id, {"priority": "high", "characters": ["Winnie the Pooh"]}, "123")
    assert edited.rule.priority.value == "high"
    await manager.set_enabled(added.id, False, "123")
    assert all(rule.name != "Pooh" for rule in manager.current.rules)
    await manager.set_enabled(added.id, True, "123")
    rebuilt = WatchlistManager(manager.database)
    await rebuilt.initialize(Watchlist((WatchRule("ignored"),)))
    assert rebuilt.get(added.id).rule.name == "Pooh"  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        await manager.add({"name": "bad", "priority": "urgent"})
    await manager.delete(added.id)
    assert manager.get(added.id) is None


@pytest.mark.asyncio
async def test_existing_monitor_service_sees_live_snapshot(manager: WatchlistManager) -> None:
    product = Product("Shop", "1", "New Tinker Bell Bag", "https://example/a",
                      availability=Availability.IN_STOCK, currency="USD", product_type="mini_backpack")
    service = MonitorService(SimpleNamespace(), manager.database, AsyncMock(),
                             retailer_name="Shop", watchlist=manager)
    assert not service._matches(product)
    await manager.add({"name": "Tink", "keywords": ["tinker bell"]})
    assert service._matches(product)[0].name == "Tink"


@pytest.mark.asyncio
async def test_authorization_and_durable_audit(manager: WatchlistManager) -> None:
    config = DiscordBotConfig(enabled=True, token="not-used", allowed_user_ids=frozenset({7}),
                              allowed_role_ids=frozenset({9}))
    app_config = AppConfig(MonitorConfig(), manager.database.path, LoggingConfig())
    service = DiscordControlService(config, manager, manager.database, app_config)
    denied = SimpleNamespace(user=SimpleNamespace(id=8, roles=[]), guild_id=1, channel_id=2,
                             response=SimpleNamespace(send_message=AsyncMock()))
    allowed = SimpleNamespace(user=SimpleNamespace(id=7, roles=[]), guild_id=1, channel_id=2)
    assert service.authorized(allowed)
    assert not await service.require_authorized(denied, "add")
    denied.response.send_message.assert_awaited_once_with(
        "You are not authorized to manage watches.", ephemeral=True)
    row = await (await manager.database.connection.execute(
        "SELECT action,success FROM discord_audit_log"  # type: ignore[union-attr]
    )).fetchone()
    assert row == ("denied:add", 0)
    # Avoid an unclosed client warning; no gateway was started.
    await service.close()


@pytest.mark.asyncio
async def test_gateway_failure_is_isolated_and_close_is_graceful(manager: WatchlistManager) -> None:
    config = DiscordBotConfig(enabled=True, token="bad")
    app_config = AppConfig(MonitorConfig(), manager.database.path, LoggingConfig())
    service = DiscordControlService(config, manager, manager.database, app_config)
    closed = False
    async def fail(_: str): raise RuntimeError("authentication failed")
    async def close():
        nonlocal closed; closed = True
    service.client.start = fail  # type: ignore[method-assign,union-attr]
    service.client.close = close  # type: ignore[method-assign,union-attr]
    service.client.is_closed = lambda: False  # type: ignore[method-assign,union-attr]
    await service.start(); await asyncio.sleep(0)
    await service.close()
    assert closed

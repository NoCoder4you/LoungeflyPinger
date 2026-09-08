import asyncio
from decimal import Decimal
from pathlib import Path

import aiohttp
import pytest

from app.config import NotificationConfig
from app.database import Database
from app.models import Alert, AlertType, Availability, Product
from app.notifications.discord import DiscordNotifier, build_discord_payload


class FakeResponse:
    def __init__(self, status: int = 204) -> None:
        self.status = status
        self.released = False

    def release(self) -> None:
        self.released = True


class FakeSession:
    def __init__(self, statuses: list[int] | None = None, error: Exception | None = None) -> None:
        self.closed = False
        self.statuses = statuses or [204]
        self.error = error
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def post(self, url: str, *, json: dict[str, object]) -> FakeResponse:
        self.calls.append((url, json))
        if self.error:
            error, self.error = self.error, None
            raise error
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return FakeResponse(status)


class BlockingSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.request_started = asyncio.Event()

    async def post(self, url: str, *, json: dict[str, object]) -> FakeResponse:
        self.calls.append((url, json))
        self.request_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.fixture
def product() -> Product:
    return Product(
        retailer="GeekCore",
        retailer_product_id="stitch-1",
        name="Disney Stitch Floral Mini Backpack",
        url="https://shop.example/products/stitch",
        image_url="https://shop.example/images/stitch.jpg",
        availability=Availability.IN_STOCK,
        price=Decimal("79.99"),
        currency="GBP",
        franchise="Disney",
        character="Stitch",
        exclusive=True,
    )


def test_discord_payload_contains_formatted_product_details(product: Product) -> None:
    payload = build_discord_payload(Alert(
        AlertType.RESTOCK,
        product=product,
        product_id=12,
        previous_price=Decimal("89.99"),
        previous_availability=Availability.OUT_OF_STOCK,
    ))

    embed = payload["embeds"][0]
    assert embed["title"] == "🎒 LOUNGEFLY RESTOCK"
    assert embed["description"] == product.name
    assert embed["url"] == product.url
    assert embed["thumbnail"] == {"url": product.image_url}
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert fields == {
        "Retailer": "GeekCore", "Price": "£79.99", "Previous Price": "£89.99",
        "Status": "In Stock", "Previous Status": "Out Of Stock", "Franchise": "Disney",
        "Character": "Stitch", "Exclusive": "Yes", "Preorder": "No",
    }
    assert payload["allowed_mentions"] == {"parse": []}


@pytest.mark.parametrize("alert_type", [
    AlertType.NEW_PRODUCT, AlertType.RESTOCK, AlertType.PREORDER_OPEN,
    AlertType.PRICE_DROP, AlertType.LOW_STOCK, AlertType.MONITOR_ERROR,
    AlertType.MONITOR_RECOVERED,
])
def test_each_supported_alert_has_a_readable_embed(alert_type: AlertType, product: Product) -> None:
    alert = Alert(alert_type, message="Health changed") if alert_type.name.startswith("MONITOR") else Alert(alert_type, product)
    embed = build_discord_payload(alert)["embeds"][0]
    assert alert_type.value.replace("_", " ") in embed["title"]
    assert embed["timestamp"].endswith("+00:00")


@pytest.mark.asyncio
async def test_webhook_routing(tmp_path: Path, product: Product) -> None:
    async with Database(tmp_path / "routing.db") as database:
        session = FakeSession()
        notifier = DiscordNotifier(NotificationConfig("https://normal", "https://admin"), database, session=session)
        assert await notifier.send(Alert(AlertType.NEW_PRODUCT, product, new_state="new"))
        assert await notifier.send(Alert(AlertType.MONITOR_ERROR, message="Store unavailable", new_state="down"))
        assert [call[0] for call in session.calls] == ["https://normal", "https://admin"]


@pytest.mark.asyncio
async def test_missing_webhook_configuration_does_not_attempt_delivery(tmp_path: Path, product: Product) -> None:
    async with Database(tmp_path / "missing.db") as database:
        session = FakeSession()
        notifier = DiscordNotifier(NotificationConfig(), database, session=session)
        assert not await notifier.send(Alert(AlertType.RESTOCK, product))
        assert not session.calls


@pytest.mark.asyncio
async def test_duplicate_is_suppressed_across_notifier_instances(tmp_path: Path, product: Product) -> None:
    path = tmp_path / "dedup.db"
    alert = Alert(AlertType.RESTOCK, product, product_id=42, occurrence_id="restock-episode-1")
    first_session = FakeSession()
    async with Database(path) as database:
        notifier = DiscordNotifier(NotificationConfig("https://normal"), database, session=first_session)
        assert await notifier.send(alert)
    second_session = FakeSession()
    async with Database(path) as database:
        notifier = DiscordNotifier(NotificationConfig("https://normal"), database, session=second_session)
        restored_alert = Alert(
            AlertType.RESTOCK, product, product_id=42, occurrence_id="restock-episode-1"
        )
        assert await notifier.send(restored_alert)
    assert len(first_session.calls) == 1
    assert second_session.calls == []


@pytest.mark.asyncio
async def test_separate_alert_episodes_with_same_state_are_delivered(tmp_path: Path, product: Product) -> None:
    async with Database(tmp_path / "episodes.db") as database:
        session = FakeSession()
        notifier = DiscordNotifier(NotificationConfig("https://normal"), database, session=session)
        first = Alert(AlertType.RESTOCK, product, product_id=42, occurrence_id="restock-episode-1")
        second = Alert(AlertType.RESTOCK, product, product_id=42, occurrence_id="restock-episode-2")

        assert await notifier.send(first)
        assert await notifier.send(second)

        assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_separate_identical_monitor_failure_episodes_are_delivered(tmp_path: Path) -> None:
    async with Database(tmp_path / "monitor-episodes.db") as database:
        session = FakeSession()
        notifier = DiscordNotifier(
            NotificationConfig(discord_admin_webhook_url="https://admin"), database, session=session
        )
        first = Alert(AlertType.MONITOR_ERROR, message="Store unavailable", occurrence_id="failure-episode-1")
        second = Alert(AlertType.MONITOR_ERROR, message="Store unavailable", occurrence_id="failure-episode-2")

        assert await notifier.send(first)
        assert await notifier.send(second)

        assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_webhook_failure_returns_false_and_remains_retryable(tmp_path: Path, product: Product) -> None:
    async with Database(tmp_path / "failure.db") as database:
        session = FakeSession(error=aiohttp.ClientConnectionError("offline"))
        notifier = DiscordNotifier(NotificationConfig("https://normal"), database, session=session)
        alert = Alert(AlertType.PRICE_DROP, product, product_id=7)
        assert not await notifier.send(alert)
        assert await notifier.send(alert)
        assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_cancelled_delivery_is_not_persisted_and_can_retry_after_restart(
    tmp_path: Path, product: Product
) -> None:
    path = tmp_path / "cancelled.db"
    alert = Alert(AlertType.RESTOCK, product, product_id=42, occurrence_id="cancelled-episode")
    blocking_session = BlockingSession()
    async with Database(path) as database:
        notifier = DiscordNotifier(
            NotificationConfig("https://normal"), database, session=blocking_session
        )
        delivery = asyncio.create_task(notifier.send(alert))
        await blocking_session.request_started.wait()
        delivery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await delivery
        count = await (
            await database.connection.execute("SELECT count(*) FROM notification_deliveries")
        ).fetchone()
        assert count == (0,)

    retry_session = FakeSession()
    async with Database(path) as database:
        notifier = DiscordNotifier(
            NotificationConfig("https://normal"), database, session=retry_session
        )
        assert await notifier.send(alert)
    assert len(retry_session.calls) == 1

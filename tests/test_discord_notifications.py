import asyncio
from decimal import Decimal
from pathlib import Path

import aiohttp
import pytest

from app.config import NotificationConfig
from app.database import Database
from app.models import Alert, AlertType, Availability, Priority, Product, WatchMatch
from app.notifications.discord import DiscordNotifier, build_discord_payload
from app.services.notification_settings import NotificationSettingsService


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
    assert embed["title"] == product.name
    assert embed["description"] == "🟢 RESTOCK"
    assert embed["url"] == product.url
    assert embed["thumbnail"] == {"url": product.image_url}
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert fields["Price"] == "£89.99 → £79.99"
    assert fields["Status"] == "🟢 In Stock"
    assert fields["Previous Status"] == "🔴 Out Of Stock"
    assert fields["Exclusive"] == "⭐ Retailer Exclusive"
    assert fields["Quick Links"] == f"[View Product]({product.url})"
    assert payload["content"] == "@everyone"
    assert payload["allowed_mentions"] == {"parse": ["everyone"]}


def test_discord_payload_does_not_mention_everyone_for_removed_product(product: Product) -> None:
    payload = build_discord_payload(Alert(AlertType.PRODUCT_REMOVED, product=product))

    assert "content" not in payload
    assert payload["allowed_mentions"] == {"parse": []}


def test_operational_alert_does_not_repeat_headline_in_description() -> None:
    payload = build_discord_payload(Alert(
        AlertType.MONITOR_RECOVERED,
        message="Retailer: Cool-Merch UK\nThe retailer monitor is responding normally again.",
        new_state="HEALTHY",
    ))

    embed = payload["embeds"][0]
    assert embed["title"] == "🟢 MONITOR RECOVERED"
    assert embed["description"] == (
        "Retailer: Cool-Merch UK\nThe retailer monitor is responding normally again."
    )


def test_operational_alert_preserves_diagnostic_line_breaks() -> None:
    message = (
        "Retailer: Damaged Society UK\n"
        "Failures: 5\n"
        "Last Successful Scan: 2026-10-04T20:04:46+00:00\n"
        "Error: Rate limit retry exhausted"
    )

    embed = build_discord_payload(Alert(
        AlertType.MONITOR_ERROR, message=message, new_state="FAILED"
    ))["embeds"][0]

    assert embed["title"] == "🔴 MONITOR ERROR"
    assert embed["description"] == message


@pytest.mark.parametrize("alert_type", list(AlertType))
def test_every_alert_type_obeys_everyone_mention_policy(
    alert_type: AlertType, product: Product
) -> None:
    alert = (Alert(alert_type, message="Retailer state changed")
             if alert_type in {AlertType.MONITOR_ERROR, AlertType.MONITOR_RECOVERED}
             else Alert(alert_type, product=product))
    payload = build_discord_payload(alert)
    if alert_type in {AlertType.PRODUCT_REMOVED, AlertType.MONITOR_ERROR, AlertType.MONITOR_RECOVERED}:
        assert "content" not in payload
        assert payload["allowed_mentions"] == {"parse": []}
    else:
        assert payload["content"] == "@everyone"
        assert payload["allowed_mentions"] == {"parse": ["everyone"]}


def test_discord_payload_shows_watches_and_highest_priority(product: Product) -> None:
    payload = build_discord_payload(Alert(
        AlertType.NEW_PRODUCT, product=product,
        watch_matches=(WatchMatch("Any Mini", Priority.LOW), WatchMatch("Stitch Backpacks", Priority.HIGH)),
    ))
    fields = {field["name"]: field["value"] for field in payload["embeds"][0]["fields"]}
    assert fields["Matched"] == "Any Mini Stitch Backpacks"
    assert fields["Priority"] == "HIGH"


@pytest.mark.parametrize("alert_type", [
    AlertType.NEW_PRODUCT, AlertType.RESTOCK, AlertType.PREORDER_OPEN,
    AlertType.PRICE_DROP, AlertType.LOW_STOCK, AlertType.MONITOR_ERROR,
    AlertType.MONITOR_RECOVERED,
])
def test_each_supported_alert_has_a_readable_embed(alert_type: AlertType, product: Product) -> None:
    alert = Alert(alert_type, message="Health changed") if alert_type.name.startswith("MONITOR") else Alert(alert_type, product)
    embed = build_discord_payload(alert)["embeds"][0]
    rendered_text = f'{embed["title"]}\n{embed["description"]}'
    assert alert_type.value.replace("_OPEN", "").replace("_", " ") in rendered_text
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
async def test_disabled_product_removed_alert_does_not_attempt_delivery(
    tmp_path: Path, product: Product
) -> None:
    async with Database(tmp_path / "removed-disabled.db") as database:
        session = FakeSession()
        notifier = DiscordNotifier(
            NotificationConfig(
                discord_webhook_url="https://normal", product_removed_enabled=False
            ),
            database,
            session=session,
        )

        assert await notifier.send(Alert(AlertType.PRODUCT_REMOVED, product))
        assert session.calls == []
        deliveries = await (
            await database.connection.execute("SELECT count(*) FROM notification_deliveries")
        ).fetchone()
        assert deliveries == (0,)

        assert await notifier.send(Alert(AlertType.RESTOCK, product))
        assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_runtime_settings_can_disable_any_alert_type(
    tmp_path: Path, product: Product
) -> None:
    async with Database(tmp_path / "runtime-alerts.db") as database:
        settings = NotificationSettingsService(database, NotificationConfig())
        await settings.initialize()
        await settings.set_enabled(AlertType.RESTOCK, False, "42")
        session = FakeSession()
        notifier = DiscordNotifier(
            NotificationConfig(discord_webhook_url="https://normal"), database,
            session=session, settings=settings
        )

        assert await notifier.send(Alert(AlertType.RESTOCK, product))
        assert session.calls == []

        await settings.set_enabled(AlertType.RESTOCK, True, "42")
        assert await notifier.send(Alert(AlertType.RESTOCK, product))
        assert len(session.calls) == 1


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

@pytest.mark.parametrize(("mode", "role_id", "content", "allowed"), [
    ("none", None, None, {"parse": []}),
    ("everyone", None, "@everyone", {"parse": ["everyone"]}),
    ("role", 123456, "<@&123456>", {"parse": [], "roles": ["123456"]}),
])
def test_configurable_mentions_are_explicitly_allowlisted(product, mode, role_id, content, allowed):
    config = NotificationConfig(discord_alert_mention_mode=mode, discord_alert_role_id=role_id)
    payload = build_discord_payload(Alert(AlertType.NEW_PRODUCT, product), config)
    assert payload.get("content") == content
    assert payload["allowed_mentions"] == allowed


@pytest.mark.parametrize(("alert_type", "mode", "role_id"), [
    (AlertType.PRODUCT_REMOVED, "everyone", None),
    (AlertType.MONITOR_ERROR, "everyone", None),
    (AlertType.MONITOR_RECOVERED, "role", 123456),
])
def test_operational_alerts_never_mention(alert_type, mode, role_id, product):
    config = NotificationConfig(discord_alert_mention_mode=mode, discord_alert_role_id=role_id)
    alert = (Alert(alert_type, product)
             if alert_type is AlertType.PRODUCT_REMOVED
             else Alert(alert_type, message="Retailer state changed"))

    payload = build_discord_payload(alert, config)

    assert "content" not in payload
    assert payload["allowed_mentions"] == {"parse": []}


def test_optional_cart_sku_and_exclusive_metadata(product):
    enriched = Product(
        retailer=product.retailer, retailer_product_id=product.retailer_product_id,
        name=product.name, url=product.url, availability=product.availability,
        price=product.price, currency=product.currency, sku="WDBK2380", exclusive=True,
        exclusivity_text="GeekCore Exclusive", cart_url="https://shop.example/cart/123",
    )
    fields = {field["name"]: field["value"] for field in
              build_discord_payload(Alert(AlertType.RESTOCK, enriched))["embeds"][0]["fields"]}
    assert fields["SKU"] == "WDBK2380"
    assert fields["Exclusive"] == "⭐ GeekCore Exclusive"
    assert "[Add to Cart](https://shop.example/cart/123)" in fields["Quick Links"]


def test_missing_optional_data_and_bad_image_are_omitted(product):
    minimal = Product(retailer="Store", retailer_product_id="1", name="Bag",
                      url="https://example.com/bag", availability=Availability.COMING_SOON)
    object.__setattr__(minimal, "image_url", "javascript:alert(1)")
    embed = build_discord_payload(Alert(AlertType.COMING_SOON, minimal))["embeds"][0]
    assert "thumbnail" not in embed
    assert {field["name"] for field in embed["fields"]} == {
        "Retailer", "Status", "Product Type", "Quick Links"
    }


def test_price_change_uses_decimal_math(product):
    cheaper = Product(retailer=product.retailer, retailer_product_id=product.retailer_product_id,
                      name=product.name, url=product.url, availability=product.availability,
                      price=Decimal("44.99"), currency="GBP")
    fields = {field["name"]: field["value"] for field in build_discord_payload(Alert(
        AlertType.PRICE_DROP, cheaper, previous_price=Decimal("59.99")))["embeds"][0]["fields"]}
    assert fields["Price"] == "£59.99 → £44.99"
    assert fields["Saving"] == "£15.00 (25.0%)"


def test_long_adapter_data_stays_within_discord_limits(product):
    long_product = Product(retailer="R" * 500, retailer_product_id="long", name="N" * 1000,
                           url="https://example.com/product", availability=Availability.IN_STOCK,
                           sku="S" * 2000)
    embed = build_discord_payload(Alert(AlertType.NEW_PRODUCT, long_product))["embeds"][0]
    assert len(embed["title"]) <= 256
    assert len(embed["author"]["name"]) <= 256
    assert len(embed["fields"]) <= 25
    assert all(len(field["name"]) <= 256 and len(field["value"]) <= 1024
               for field in embed["fields"])
    assert sum(len(embed.get(key, "")) for key in ("title", "description")) + sum(
        len(field["name"]) + len(field["value"]) for field in embed["fields"]) <= 6000

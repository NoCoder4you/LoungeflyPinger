"""Discord webhook notification provider."""

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import aiohttp

from app.config import NotificationConfig
from app.database import Database
from app.models import Alert, AlertType
from app.release import format_release
from app.notifications.base import NotificationProvider

LOGGER = logging.getLogger("monitor.notifications.discord")

TITLES = {
    AlertType.NEW_PRODUCT: "🎒 LOUNGEFLY NEW PRODUCT",
    AlertType.RESTOCK: "🎒 LOUNGEFLY RESTOCK",
    AlertType.PREORDER_OPEN: "📅 LOUNGEFLY PREORDER OPEN",
    AlertType.PRICE_DROP: "💷 LOUNGEFLY PRICE DROP",
    AlertType.LOW_STOCK: "⚠️ LOUNGEFLY LOW STOCK",
    AlertType.AVAILABILITY: "🎒 LOUNGEFLY NOW AVAILABLE",
    AlertType.PRODUCT_REMOVED: "🗑️ LOUNGEFLY PRODUCT REMOVED",
    AlertType.MONITOR_ERROR: "⚠️ RETAILER MONITOR ERROR / FAILURE",
    AlertType.MONITOR_RECOVERED: "✅ RETAILER MONITOR RECOVERED",
    AlertType.RELEASE_DATE_FOUND: "📅 LOUNGEFLY RELEASE DATE FOUND",
    AlertType.RELEASE_DATE_CHANGED: "📅 LOUNGEFLY RELEASE UPDATED",
    AlertType.RELEASE_TIME_FOUND: "📅 LOUNGEFLY RELEASE TIME FOUND",
    AlertType.RELEASE_TIME_CHANGED: "📅 LOUNGEFLY RELEASE UPDATED",
    AlertType.RELEASE_DATETIME_CHANGED: "📅 LOUNGEFLY RELEASE UPDATED",
    AlertType.RELEASING_SOON: "⏰ LOUNGEFLY RELEASING SOON",
    AlertType.RELEASED: "🎉 LOUNGEFLY RELEASED",
    AlertType.ETA_CHANGED: "📦 RETAILER ETA CHANGED",
}

COLORS = {
    AlertType.NEW_PRODUCT: 0x5865F2,
    AlertType.RESTOCK: 0x57F287,
    AlertType.PREORDER_OPEN: 0xFEE75C,
    AlertType.PRICE_DROP: 0xEB459E,
    AlertType.LOW_STOCK: 0xF0A000,
    AlertType.AVAILABILITY: 0x57F287,
    AlertType.PRODUCT_REMOVED: 0x747F8D,
    AlertType.MONITOR_ERROR: 0xED4245,
    AlertType.MONITOR_RECOVERED: 0x57F287,
    AlertType.RELEASE_DATE_FOUND: 0xFEE75C,
    AlertType.RELEASE_DATE_CHANGED: 0xFEE75C,
    AlertType.RELEASE_TIME_FOUND: 0xFEE75C,
    AlertType.RELEASE_TIME_CHANGED: 0xFEE75C,
    AlertType.RELEASE_DATETIME_CHANGED: 0xFEE75C,
    AlertType.RELEASING_SOON: 0xF0A000,
    AlertType.RELEASED: 0x57F287,
    AlertType.ETA_CHANGED: 0xFEE75C,
}


def _display(value: str) -> str:
    return value.replace("_", " ").title()


def _money(value: Decimal, currency: str) -> str:
    symbol = {"GBP": "£", "USD": "$", "AUD": "$", "EUR": "€"}.get(currency, f"{currency} ")
    return f"{symbol}{value:.2f}"


def build_discord_payload(alert: Alert) -> dict[str, Any]:
    """Build a Discord-compatible embed without performing network I/O."""
    product = alert.product
    description = alert.message or (product.name if product else "Loungefly monitor status update")
    embed: dict[str, Any] = {
        "title": TITLES[alert.alert_type],
        "description": description[:4096],
        "color": COLORS[alert.alert_type],
        "timestamp": alert.timestamp.isoformat(),
        "fields": [],
    }
    fields: list[dict[str, Any]] = embed["fields"]

    def add(name: str, value: object) -> None:
        fields.append({"name": name, "value": str(value)[:1024], "inline": True})

    if product:
        add("Retailer", product.retailer)
        if product.price is not None:
            add("Price", _money(product.price, product.currency))
        if product.original_price is not None and product.original_price != product.price:
            add("RRP" if product.retailer == "Ozzie Collectables" else "Original Price",
                _money(product.original_price, product.currency))
        if alert.previous_price is not None:
            add("Previous Price", _money(alert.previous_price, product.currency))
        add("Status", _display(product.availability.value))
        if product.retailer == "Ozzie Collectables":
            add("Product Type", _display(product.product_type))
            if product.sku:
                add("SKU", product.sku)
            if product.barcode:
                add("Barcode", product.barcode)
        if alert.previous_availability is not None:
            add("Previous Status", _display(alert.previous_availability.value))
        if product.franchise:
            add("Franchise", product.franchise)
        if product.character:
            add("Character", product.character)
        add("Exclusive", "Yes" if product.exclusive else "No")
        if product.exclusive_retailer:
            add("Exclusive Retailer", product.exclusive_retailer)
        if product.exclusive_region:
            add("Exclusive Region", product.exclusive_region)
        if product.new_release:
            add("New Release", "Yes")
        add("Preorder", "Yes" if product.preorder else "No")
        if product.estimated_arrival_text or product.estimated_arrival_date:
            add("Retailer ETA", product.estimated_arrival_text or product.estimated_arrival_date.isoformat())
            if product.preorder and product.release is None:
                add("Official Release Date", "Unknown")
        if product.release is not None:
            if alert.previous_release is not None:
                add("Previous Release", format_release(alert.previous_release))
                add("New Release", format_release(product.release))
            else:
                add("Release", format_release(product.release))
            if product.release.timezone_inferred:
                add("Release Timezone", f"{product.release.timezone} (retailer local timezone inferred)")
        if alert.reminder_seconds is not None:
            hours = alert.reminder_seconds / 3600
            add("Time Remaining", f"approximately {hours:g} hour{'s' if hours != 1 else ''}")
        if alert.watch_matches:
            add("Matched Watch", "\n".join(match.name for match in alert.watch_matches))
            add("Priority", alert.priority.value.upper())
        embed["url"] = product.url
        if product.image_url:
            embed["thumbnail"] = {"url": product.image_url}
    if alert.new_state and not product:
        add("Status", _display(alert.new_state))
    return {"username": "Loungefly Monitor", "allowed_mentions": {"parse": []}, "embeds": [embed]}


class DiscordNotifier(NotificationProvider):
    """Routes embeds to product/admin webhooks with persistent deduplication."""

    def __init__(
        self,
        config: NotificationConfig,
        database: Database,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._config = config
        self._database = database
        self._session = session
        self._owns_session = session is None
        self._delivery_lock = asyncio.Lock()

    def _webhook(self, alert: Alert) -> str | None:
        return (self._config.discord_admin_webhook_url if alert.is_admin
                else self._config.discord_webhook_url)

    @staticmethod
    def deduplication_key(alert: Alert) -> str:
        product_key: int | str | None = alert.product_id
        if product_key is None and alert.product:
            product_key = f"{alert.product.retailer}:{alert.product.retailer_product_id}"
        state = alert.new_state or (alert.product.availability.value if alert.product else alert.message)
        release = alert.product.release if alert.product else None
        price = str(alert.product.price) if alert.product and alert.product.price is not None else None
        identity = json.dumps(
            [alert.occurrence_id, product_key, alert.alert_type.value, state, price,
             format_release(alert.previous_release), format_release(release)],
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode()).hexdigest()

    async def _was_delivered(self, alert: Alert) -> bool:
        connection = self._database.connection
        if connection is None:
            raise RuntimeError("database is not connected")
        row = await (
            await connection.execute(
                "SELECT 1 FROM notification_deliveries WHERE deduplication_key = ?",
                (self.deduplication_key(alert),),
            )
        ).fetchone()
        return row is not None

    async def _record_delivery(self, alert: Alert, destination: str) -> None:
        connection = self._database.connection
        if connection is None:
            raise RuntimeError("database is not connected")
        await connection.execute(
            "INSERT OR IGNORE INTO notification_deliveries "
            "(deduplication_key, alert_type, destination, sent_at) VALUES (?, ?, ?, ?)",
            (self.deduplication_key(alert), alert.alert_type.value, destination, datetime.now(UTC).isoformat()),
        )
        await connection.commit()

    async def send(self, alert: Alert) -> bool:
        webhook = self._webhook(alert)
        if not webhook:
            LOGGER.warning("Discord webhook is not configured", extra={"admin": alert.is_admin})
            return False
        destination = "discord_admin" if alert.is_admin else "discord"
        async with self._delivery_lock:
            if await self._was_delivered(alert):
                LOGGER.info("Suppressing duplicate Discord alert", extra={"alert_type": alert.alert_type.value})
                return True
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
                self._owns_session = True
            try:
                response = await self._session.post(webhook, json=build_discord_payload(alert))
                try:
                    if response.status < 200 or response.status >= 300:
                        LOGGER.error("Discord webhook rejected notification", extra={"status": response.status})
                        return False
                    await self._record_delivery(alert, destination)
                    return True
                finally:
                    response.release()
            except Exception as exc:
                LOGGER.error("Discord webhook delivery failed: %s", type(exc).__name__)
                return False

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._session = None

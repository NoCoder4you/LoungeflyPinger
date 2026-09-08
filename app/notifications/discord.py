"""Discord webhook notification provider."""

import hashlib
import json
import logging
from decimal import Decimal
from typing import Any

import aiohttp

from app.config import NotificationConfig
from app.database import Database
from app.models import Alert, AlertType
from app.notifications.base import NotificationProvider

LOGGER = logging.getLogger("monitor.notifications.discord")

TITLES = {
    AlertType.NEW_PRODUCT: "🎒 LOUNGEFLY NEW PRODUCT",
    AlertType.RESTOCK: "🎒 LOUNGEFLY RESTOCK",
    AlertType.PREORDER_OPEN: "📅 LOUNGEFLY PREORDER OPEN",
    AlertType.PRICE_DROP: "💷 LOUNGEFLY PRICE DROP",
    AlertType.LOW_STOCK: "⚠️ LOUNGEFLY LOW STOCK",
    AlertType.MONITOR_ERROR: "🚨 MONITOR ERROR",
    AlertType.MONITOR_RECOVERED: "✅ MONITOR RECOVERED",
}

COLORS = {
    AlertType.NEW_PRODUCT: 0x5865F2,
    AlertType.RESTOCK: 0x57F287,
    AlertType.PREORDER_OPEN: 0xFEE75C,
    AlertType.PRICE_DROP: 0xEB459E,
    AlertType.LOW_STOCK: 0xF0A000,
    AlertType.MONITOR_ERROR: 0xED4245,
    AlertType.MONITOR_RECOVERED: 0x57F287,
}


def _display(value: str) -> str:
    return value.replace("_", " ").title()


def _money(value: Decimal, currency: str) -> str:
    symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency, f"{currency} ")
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
        if alert.previous_price is not None:
            add("Previous Price", _money(alert.previous_price, product.currency))
        add("Status", _display(product.availability.value))
        if alert.previous_availability is not None:
            add("Previous Status", _display(alert.previous_availability.value))
        if product.franchise:
            add("Franchise", product.franchise)
        if product.character:
            add("Character", product.character)
        add("Exclusive", "Yes" if product.exclusive else "No")
        add("Preorder", "Yes" if product.preorder else "No")
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

    def _webhook(self, alert: Alert) -> str | None:
        return (self._config.discord_admin_webhook_url if alert.is_admin
                else self._config.discord_webhook_url)

    @staticmethod
    def deduplication_key(alert: Alert) -> str:
        product_key: int | str | None = alert.product_id
        if product_key is None and alert.product:
            product_key = f"{alert.product.retailer}:{alert.product.retailer_product_id}"
        state = alert.new_state or (alert.product.availability.value if alert.product else alert.message)
        price = str(alert.product.price) if alert.product and alert.product.price is not None else None
        identity = json.dumps([product_key, alert.alert_type.value, state, price], separators=(",", ":"))
        return hashlib.sha256(identity.encode()).hexdigest()

    async def _claim(self, alert: Alert, destination: str) -> bool:
        connection = self._database.connection
        if connection is None:
            raise RuntimeError("database is not connected")
        cursor = await connection.execute(
            "INSERT OR IGNORE INTO notification_deliveries "
            "(deduplication_key, alert_type, destination, sent_at) VALUES (?, ?, ?, ?)",
            (self.deduplication_key(alert), alert.alert_type.value, destination, alert.timestamp.isoformat()),
        )
        await connection.commit()
        return cursor.rowcount == 1

    async def send(self, alert: Alert) -> bool:
        webhook = self._webhook(alert)
        if not webhook:
            LOGGER.warning("Discord webhook is not configured", extra={"admin": alert.is_admin})
            return False
        destination = "discord_admin" if alert.is_admin else "discord"
        if not await self._claim(alert, destination):
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
                    await self._release(alert)
                    return False
                return True
            finally:
                response.release()
        except Exception as exc:
            LOGGER.error("Discord webhook delivery failed: %s", type(exc).__name__)
            await self._release(alert)
            return False

    async def _release(self, alert: Alert) -> None:
        connection = self._database.connection
        assert connection is not None
        await connection.execute(
            "DELETE FROM notification_deliveries WHERE deduplication_key = ?",
            (self.deduplication_key(alert),),
        )
        await connection.commit()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._session = None

"""Rich Discord webhook rendering and isolated, durable delivery."""

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from dataclasses import replace
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlsplit, urlunsplit

import aiohttp
import discord

from app.config import NotificationConfig
from app.database import Database
from app.models import Alert, AlertType, Availability
from app.notifications.base import NotificationProvider
from app.notifications.grouping import GroupedAlertStore, combined_payload, group_key, offer_for
from app.release import format_release
from app.retailers import retailer_brand
from app.services.notification_settings import NotificationSettingsService

LOGGER = logging.getLogger("monitor.notifications.discord")

DISCORD_LIMITS = {"title": 256, "description": 4096, "field_name": 256,
                  "field_value": 1024, "fields": 25, "embed": 6000}

TITLES = {
    AlertType.NEW_PRODUCT: "🔵 NEW PRODUCT", AlertType.RESTOCK: "🟢 RESTOCK",
    AlertType.PREORDER_OPEN: "🟣 PREORDER", AlertType.BACKORDER: "🟠 BACKORDER",
    AlertType.COMING_SOON: "🔵 COMING SOON", AlertType.PRICE_DROP: "🟢 PRICE DROP",
    AlertType.PRICE_INCREASE: "🟠 PRICE INCREASE", AlertType.OUT_OF_STOCK: "🔴 SOLD OUT",
    AlertType.STATUS_CHANGE: "🔵 STATUS CHANGE", AlertType.PRODUCT_UPDATED: "🔵 PRODUCT UPDATED",
    AlertType.LOW_STOCK: "🟠 LOW STOCK", AlertType.AVAILABILITY: "🟢 NOW AVAILABLE",
    AlertType.PRODUCT_REMOVED: "⚫ PRODUCT REMOVED", AlertType.MONITOR_ERROR: "🔴 MONITOR ERROR",
    AlertType.MONITOR_RECOVERED: "🟢 MONITOR RECOVERED", AlertType.RELEASE_DATE_FOUND: "🔵 RELEASE DATE FOUND",
    AlertType.RELEASE_DATE_CHANGED: "🔵 RELEASE UPDATED", AlertType.RELEASE_TIME_FOUND: "🔵 RELEASE TIME FOUND",
    AlertType.RELEASE_TIME_CHANGED: "🔵 RELEASE UPDATED", AlertType.RELEASE_DATETIME_CHANGED: "🔵 RELEASE UPDATED",
    AlertType.RELEASING_SOON: "🟠 RELEASING SOON", AlertType.RELEASED: "🟢 RELEASED",
    AlertType.ETA_CHANGED: "🔵 RETAILER ETA CHANGED",
}
COLORS = {
    AlertType.NEW_PRODUCT: 0x3498DB, AlertType.RESTOCK: 0x2ECC71,
    AlertType.PREORDER_OPEN: 0x9B59B6, AlertType.BACKORDER: 0xE67E22,
    AlertType.COMING_SOON: 0x3498DB, AlertType.PRICE_DROP: 0x2ECC71,
    AlertType.PRICE_INCREASE: 0xE67E22, AlertType.OUT_OF_STOCK: 0xE74C3C,
    AlertType.STATUS_CHANGE: 0x3498DB, AlertType.PRODUCT_UPDATED: 0x3498DB,
    AlertType.LOW_STOCK: 0xF39C12, AlertType.AVAILABILITY: 0x2ECC71,
    AlertType.PRODUCT_REMOVED: 0x747F8D, AlertType.MONITOR_ERROR: 0xE74C3C,
    AlertType.MONITOR_RECOVERED: 0x2ECC71, AlertType.RELEASE_DATE_FOUND: 0x3498DB,
    AlertType.RELEASE_DATE_CHANGED: 0x3498DB, AlertType.RELEASE_TIME_FOUND: 0x3498DB,
    AlertType.RELEASE_TIME_CHANGED: 0x3498DB, AlertType.RELEASE_DATETIME_CHANGED: 0x3498DB,
    AlertType.RELEASING_SOON: 0xF39C12, AlertType.RELEASED: 0x2ECC71,
    AlertType.ETA_CHANGED: 0x3498DB,
}


def truncate(value: object, limit: int, *, preserve_lines: bool = False) -> str:
    """Safely fit arbitrary adapter text into a Discord component."""
    raw = str(value)
    if preserve_lines:
        # Keep intentional structure while still collapsing arbitrary whitespace
        # within each line and removing empty leading/trailing lines.
        text = "\n".join(" ".join(line.split()) for line in raw.splitlines()).strip()
    else:
        text = " ".join(raw.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _valid_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and len(value) <= 2048


def _component_webhook_url(webhook: str, *, enabled: bool, wait: bool = False) -> str:
    """Enable URL buttons and ask Discord for the posted message ID."""
    parsed = urlsplit(webhook)
    query = [
        (name, value)
        for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        if name not in {"with_components", "wait"}
    ]
    if enabled:
        query.append(("with_components", "true"))
    if wait:
        query.append(("wait", "true"))
    return urlunsplit(parsed._replace(query=urlencode(query)))


def _edit_webhook_url(webhook: str, message_id: str) -> str:
    """PATCH the message using its owning webhook (retain forum thread_id)."""
    parsed = urlsplit(webhook)
    query = [(name, value) for name, value in parse_qsl(parsed.query)
             if name == "thread_id"]
    path = parsed.path.rstrip("/") + "/messages/" + quote(message_id, safe="")
    return urlunsplit(parsed._replace(path=path, query=urlencode(query)))


def _display(value: str) -> str:
    return value.replace("_", " ").title()


def _money(value: Decimal, currency: str) -> str:
    symbol = {"GBP": "£", "USD": "$", "AUD": "A$", "EUR": "€", "CAD": "C$"}.get(currency)
    return f"{symbol or currency + ' '}{value.quantize(Decimal('0.01'))}"


def _status(availability: Availability) -> str:
    icon = {
        Availability.IN_STOCK: "🟢", Availability.LOW_STOCK: "🟠",
        Availability.OUT_OF_STOCK: "🔴", Availability.UNAVAILABLE: "🔴",
        Availability.PREORDER: "🟣", Availability.COMING_SOON: "🔵",
        Availability.BACKORDER: "🟠", Availability.ERROR: "🔴",
    }.get(availability, "⚪")
    return f"{icon} {_display(availability.value)}"


def _mention(config: NotificationConfig, *, removed: bool) -> tuple[str | None, dict[str, Any]]:
    """Never ping everyone; fall back to a configured role or no mention."""
    if not removed and config.discord_alert_mention_mode == "role" and config.discord_alert_role_id:
        role = str(config.discord_alert_role_id)
        return f"<@&{role}>", {"parse": [], "roles": [role]}
    # Includes legacy "everyone" settings, category mode with no available role,
    # disabled mentions, and operational alerts.
    return None, {"parse": []}


def build_discord_payload(
    alert: Alert, config: NotificationConfig | None = None,
    *, category_role_id: int | None = None,
) -> dict[str, Any]:
    """Build a webhook payload while enforcing Discord limits and safe mentions."""
    config = config or NotificationConfig()
    product = alert.product
    headline = TITLES[alert.alert_type]
    # Product alerts use the product name as their title, so the event headline
    # belongs in the description. Operational alerts already use that headline
    # as their title and should not repeat it in the body.
    description = f"{headline}\n{alert.message}" if product and alert.message else alert.message or headline
    embed: dict[str, Any] = {
        "title": truncate(product.name if product else headline, DISCORD_LIMITS["title"]),
        "description": truncate(description, DISCORD_LIMITS["description"], preserve_lines=True),
        "color": COLORS[alert.alert_type], "timestamp": alert.timestamp.astimezone(UTC).isoformat(),
        "fields": [],
    }
    fields: list[dict[str, Any]] = embed["fields"]
    used = len(embed["title"]) + len(embed["description"])

    def add(name: str, value: object, *, inline: bool = True,
            preserve_lines: bool = False) -> None:
        nonlocal used
        if value is None or not str(value).strip() or len(fields) >= DISCORD_LIMITS["fields"]:
            return
        safe_name = truncate(name, DISCORD_LIMITS["field_name"])
        remaining = DISCORD_LIMITS["embed"] - used - len(safe_name)
        if remaining <= 0:
            return
        safe_value = truncate(
            value, min(DISCORD_LIMITS["field_value"], remaining),
            preserve_lines=preserve_lines,
        )
        fields.append({"name": safe_name, "value": safe_value, "inline": inline})
        used += len(safe_name) + len(safe_value)

    retailer = None
    if product:
        retailer = retailer_brand(product.retailer)
        embed["author"] = {"name": truncate(retailer.display_name, 256)}
        if retailer.icon_url and _valid_url(retailer.icon_url):
            embed["author"]["icon_url"] = retailer.icon_url
        if retailer.homepage and _valid_url(retailer.homepage):
            embed["author"]["url"] = retailer.homepage
        if _valid_url(product.url):
            embed["url"] = product.url
        add("Retailer", retailer.display_name)
        add("Status", _status(product.availability))
        if product.price is not None:
            price = _money(product.price, product.currency)
            if alert.previous_price is not None and alert.previous_price != product.price:
                price = f"{_money(alert.previous_price, product.currency)} → {price}"
            add("Price", price)
            if (alert.alert_type is AlertType.PRICE_DROP and alert.previous_price and
                    alert.previous_price > product.price):
                saving = alert.previous_price - product.price
                percent = saving * Decimal("100") / alert.previous_price
                add("Saving", f"{_money(saving, product.currency)} ({percent.quantize(Decimal('0.1'))}%)")
        if product.sku:
            add("SKU", product.sku)
        elif product.loungefly_product_code:
            add("Product ID", product.loungefly_product_code)
        elif product.variant_id:
            add("Product ID", product.variant_id)
        if product.exclusive:
            exclusive = product.exclusivity_text or product.exclusive_retailer or product.exclusive_type or "Retailer Exclusive"
            add("Exclusive", f"⭐ {exclusive}")
        if product.exclusive_retailer:
            add("Exclusive Retailer", product.exclusive_retailer)
        if product.exclusive_region:
            add("Exclusive Region", product.exclusive_region)
        if product.product_type:
            add("Product Type", _display(product.product_type))
        if product.franchise:
            add("Franchise", product.franchise)
        if product.character:
            add("Character", product.character)
        if product.parks_origin:
            add("Parks Origin", product.parks_origin)
        if alert.previous_availability is not None and alert.previous_availability != product.availability:
            add("Previous Status", _status(alert.previous_availability))
        if product.release is not None:
            if alert.previous_release is not None:
                add("Previous Release", format_release(alert.previous_release))
                add("New Release", format_release(product.release))
            else:
                add("Release", format_release(product.release))
        if product.regional_offers:
            offer_lines = []
            for offer in product.regional_offers:
                price = _money(offer.price, offer.currency) if offer.price is not None else "Price unavailable"
                offer_lines.append(
                    f"[{offer.region}]({offer.url}) — {price} — {_status(offer.availability)}"
                )
            add("EMP Regional Stores", "\n".join(offer_lines), inline=False, preserve_lines=True)
        if product.estimated_arrival_text or product.estimated_arrival_date:
            add("Retailer ETA", product.estimated_arrival_text or product.estimated_arrival_date.isoformat())
        if alert.watch_matches:
            add("Matched", "\n".join(match.name for match in alert.watch_matches), inline=False)
            add("Priority", alert.priority.value.upper())
        if alert.first_seen:
            add("First Seen", f"<t:{int(alert.first_seen.timestamp())}:f>")
        if alert.alert_type is AlertType.RESTOCK:
            add("Restocked", f"<t:{int(alert.timestamp.timestamp())}:f>")
        links = []
        if _valid_url(product.url):
            links.append(f"[View Product]({product.url})")
        if _valid_url(product.cart_url):
            links.append(f"[Add to Cart]({product.cart_url})")
        if links:
            add("Quick Links", " • ".join(links), inline=False)
        if _valid_url(product.image_url):
            embed["thumbnail"] = {"url": product.image_url}
    elif alert.new_state:
        add("Status", _display(alert.new_state))
    payload: dict[str, Any] = {
        "username": "Loungefly Monitor",
        "embeds": [embed],
    }
    if product:
        # Webhooks support URL buttons without requiring a running Discord bot.
        # Prefer a retailer-provided cart permalink, while retaining a useful
        # purchase button for storefronts which only expose their product page.
        purchase_url = product.cart_url if _valid_url(product.cart_url) else product.url
        if _valid_url(purchase_url):
            payload["components"] = [{
                "type": 1,
                "components": [{
                    "type": 2,
                    "style": 5,
                    "label": "Add to Cart",
                    "emoji": {"name": "🛒"},
                    "url": purchase_url,
                }],
            }]
    # Operational notifications are deliberately non-mentioning and are routed
    # to the admin webhook. Product events retain the established policy.
    removed_or_operational = alert.alert_type in {
        AlertType.PRODUCT_REMOVED, AlertType.MONITOR_ERROR, AlertType.MONITOR_RECOVERED,
    }
    # Mention only the matching alert-category role (if enabled), otherwise
    # fall back to an explicitly configured role or send without a mention.
    if (category_role_id is not None and config.discord_alert_mention_mode != "none"
            and alert.alert_type not in {AlertType.PRODUCT_REMOVED, AlertType.MONITOR_RECOVERED}):
        role_text = str(category_role_id)
        content, allowed_mentions = f"<@&{role_text}>", {"parse": [], "roles": [role_text]}
    else:
        content, allowed_mentions = _mention(config, removed=removed_or_operational)
    payload["allowed_mentions"] = allowed_mentions
    if content is not None:
        payload["content"] = content
    return payload


class DiscordNotifier(NotificationProvider):
    """Routes embeds to product/admin webhooks with persistent deduplication."""

    def __init__(self, config: NotificationConfig, database: Database, *,
                 session: aiohttp.ClientSession | None = None,
                 settings: NotificationSettingsService | None = None) -> None:
        self._config, self._database, self._session = config, database, session
        self._settings = settings
        self._owns_session = session is None
        self._delivery_lock = asyncio.Lock()
        self._groups = GroupedAlertStore(database)
        self._auto_channels: dict[AlertType, discord.TextChannel] = {}
        self._auto_roles: dict[AlertType, int] = {}

    def set_auto_channels(self, channels: dict[AlertType, discord.TextChannel]) -> None:
        """Atomically publish the currently available bot-managed alert channels."""
        self._auto_channels = dict(channels)

    def set_auto_roles(self, roles: dict[AlertType, int]) -> None:
        self._auto_roles = dict(roles)

    def set_routing(self, *, product_webhook: str | None, admin_webhook: str | None,
                    type_webhooks: dict[AlertType, str]) -> None:
        """Refresh destinations after reloading a manually edited JSON file."""
        self._config = replace(
            self._config, discord_webhook_url=product_webhook,
            discord_admin_webhook_url=admin_webhook,
            discord_type_webhooks=dict(type_webhooks),
        )

    async def _send_to_channel(self, alert: Alert, channel: discord.TextChannel) -> None:
        payload = build_discord_payload(
            alert, self._config, category_role_id=self._auto_roles.get(alert.alert_type)
        )
        destination_key = f"channel:{channel.id}"
        group = await self._groups.find(alert, destination_key)
        if group is not None and group.message_id.isdigit():
            offers = [*group.offers, offer_for(alert)]
            merged = combined_payload(group.base_payload, offers)
            if merged is not None:
                try:
                    message = await channel.fetch_message(int(group.message_id))
                    await message.edit(
                        embed=discord.Embed.from_dict(merged["embeds"][0]), view=None,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOGGER.warning("Unable to edit grouped bot alert, posting separately: %s",
                                   type(exc).__name__)
                else:
                    try:
                        await self._groups.save(
                            alert, destination_key, group.message_id,
                            group.base_payload, offers, created_at=group.created_at,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        LOGGER.exception("Could not persist updated grouped bot alert")
                    return

        mentions = payload["allowed_mentions"]
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            users=False,
            roles=[discord.Object(id=int(role_id)) for role_id in mentions.get("roles", [])],
            replied_user=False,
        )
        view: discord.ui.View | None = None
        if payload.get("components"):
            view = discord.ui.View()
            for row in payload["components"]:
                for button in row["components"]:
                    view.add_item(discord.ui.Button(
                        style=discord.ButtonStyle.link,
                        label=button["label"],
                        url=button["url"],
                        emoji=button.get("emoji", {}).get("name"),
                    ))
        message = await channel.send(
            content=payload.get("content"),
            embed=discord.Embed.from_dict(payload["embeds"][0]),
            allowed_mentions=allowed_mentions,
            view=view,
        )
        if group_key(alert) is not None and getattr(message, "id", None) is not None:
            try:
                await self._groups.save(
                    alert, destination_key, str(message.id), payload, [offer_for(alert)],
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Could not persist grouped bot alert")

    def _webhook(self, alert: Alert) -> tuple[str | None, str]:
        # No override: keep existing product/admin delivery destinations.
        override = self._config.discord_type_webhooks.get(alert.alert_type)
        if override:
            return override, f"discord_{alert.alert_type.value.lower()}"
        if alert.is_admin:
            return self._config.discord_admin_webhook_url, "discord_admin"
        return self._config.discord_webhook_url, "discord"

    @staticmethod
    def deduplication_key(alert: Alert) -> str:
        product_key: int | str | None = alert.product_id
        if product_key is None and alert.product:
            product_key = f"{alert.product.retailer}:{alert.product.retailer_product_id}"
        state = alert.new_state or (alert.product.availability.value if alert.product else alert.message)
        release = alert.product.release if alert.product else None
        price = str(alert.product.price) if alert.product and alert.product.price is not None else None
        identity = json.dumps([alert.occurrence_id, product_key, alert.alert_type.value, state, price,
                               format_release(alert.previous_release), format_release(release)], separators=(",", ":"))
        return hashlib.sha256(identity.encode()).hexdigest()

    async def _was_delivered(self, alert: Alert) -> bool:
        connection = self._database.connection
        if connection is None:
            raise RuntimeError("database is not connected")
        return await (await connection.execute(
            "SELECT 1 FROM notification_deliveries WHERE deduplication_key = ?",
            (self.deduplication_key(alert),))).fetchone() is not None

    async def _record_delivery(self, alert: Alert, destination: str) -> None:
        assert self._database.connection is not None
        await self._database.connection.execute(
            "INSERT OR IGNORE INTO notification_deliveries (deduplication_key, alert_type, destination, sent_at) VALUES (?, ?, ?, ?)",
            (self.deduplication_key(alert), alert.alert_type.value, destination, datetime.now(UTC).isoformat()))
        await self._database.connection.commit()

    async def send(self, alert: Alert) -> bool:
        if self._settings:
            enabled = self._settings.enabled(alert.alert_type)
        elif alert.alert_type is AlertType.PRODUCT_REMOVED:
            enabled = self._config.product_removed_enabled
        elif alert.alert_type is AlertType.OUT_OF_STOCK:
            enabled = self._config.sold_out_enabled
        else:
            enabled = True
        if not enabled:
            LOGGER.info("Discord alert type is disabled", extra={
                "retailer": alert.product.retailer if alert.product else None,
                "product_id": alert.product.retailer_product_id if alert.product else alert.product_id,
                "event_type": alert.alert_type.value,
            })
            return True

        type_webhook = self._config.discord_type_webhooks.get(alert.alert_type)
        fallback_webhook = (
            self._config.discord_admin_webhook_url if alert.is_admin
            else self._config.discord_webhook_url
        )
        channel = self._auto_channels.get(alert.alert_type)
        context = {
            "retailer": alert.product.retailer if alert.product else None,
            "product_id": alert.product.retailer_product_id if alert.product else alert.product_id,
            "product_url": alert.product.url if alert.product else None,
            "event_type": alert.alert_type.value,
        }
        if not type_webhook and channel is None and not fallback_webhook:
            LOGGER.warning("Discord destination is not configured", extra=context)
            return False

        async with self._delivery_lock:
            if await self._was_delivered(alert):
                LOGGER.info("Suppressing duplicate Discord alert", extra=context)
                return True

            # Named per-category webhook -> bot message in the same channel ->
            # legacy JSON-configured fallback webhook. When no named webhook
            # exists, prefer the bot-managed channel over the fallback webhook.
            if type_webhook and await self._deliver_webhook(
                alert, type_webhook, context,
                f"discord_{alert.alert_type.value.lower()}",
            ):
                return True
            if channel is not None:
                try:
                    await self._send_to_channel(alert, channel)
                    await self._record_delivery(alert, f"discord_channel_{channel.id}")
                    return True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOGGER.error("Discord bot channel delivery failed: %s",
                                 type(exc).__name__, extra=context)
            if fallback_webhook and fallback_webhook != type_webhook:
                if await self._deliver_webhook(
                    alert, fallback_webhook, context,
                    "discord_admin" if alert.is_admin else "discord",
                ):
                    return True
            return False

    async def _deliver_webhook(
        self, alert: Alert, webhook: str, context: dict[str, Any],
        destination: str,
    ) -> bool:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
            self._owns_session = True
        destination_key = "webhook:" + hashlib.sha256(webhook.encode("utf-8")).hexdigest()
        payload = build_discord_payload(
            alert, self._config, category_role_id=self._auto_roles.get(alert.alert_type)
        )
        try:
            group = await self._groups.find(alert, destination_key)
            if group is not None and group.message_id.isdigit():
                offers = [*group.offers, offer_for(alert)]
                merged = combined_payload(group.base_payload, offers)
                if merged is not None:
                    try:
                        response = await self._session.patch(
                            _edit_webhook_url(webhook, group.message_id),
                            json={"embeds": merged["embeds"], "components": []},
                        )
                        try:
                            if 200 <= response.status < 300:
                                await self._record_delivery(alert, destination)
                                try:
                                    await self._groups.save(
                                        alert, destination_key, group.message_id,
                                        group.base_payload, offers,
                                        created_at=group.created_at,
                                    )
                                except Exception:
                                    LOGGER.exception("Could not persist grouped webhook edit")
                                return True
                            LOGGER.warning("Could not edit grouped webhook (status %s), posting separately",
                                           response.status, extra=context)
                        finally:
                            response.release()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        LOGGER.warning("Grouped webhook edit failed (%s), posting separately",
                                       type(exc).__name__, extra=context)

            request_url = _component_webhook_url(
                webhook, enabled=alert.product is not None,
                wait=group_key(alert) is not None,
            )
            response = await self._session.post(request_url, json=payload)
            try:
                if not 200 <= response.status < 300:
                    LOGGER.error("Discord webhook rejected notification", extra={
                        **context, "status": response.status,
                    })
                    return False
                # wait=true returns an ID. Old webhook mocks and proxies returning
                # 204 still deliver normally, but cannot support future edits.
                message_id = None
                if group_key(alert) is not None and response.status != 204 and hasattr(response, "json"):
                    try:
                        posted = await response.json()
                        if isinstance(posted, dict) and str(posted.get("id", "")).isdigit():
                            message_id = str(posted["id"])
                    except (ValueError, TypeError, aiohttp.ContentTypeError):
                        LOGGER.warning("Discord webhook message ID unavailable; grouping skipped")
                await self._record_delivery(alert, destination)
                if message_id is not None:
                    try:
                        await self._groups.save(
                            alert, destination_key, message_id, payload, [offer_for(alert)],
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        LOGGER.exception("Could not persist grouped webhook")
                return True
            finally:
                response.release()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.error("Discord webhook delivery failed: %s",
                         type(exc).__name__, extra=context)
            return False

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._session = None

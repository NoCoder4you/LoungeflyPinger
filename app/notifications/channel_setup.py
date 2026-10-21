"""Idempotent Discord guild channel provisioning for alert categories."""

from __future__ import annotations

import logging

import discord

from app.models import AlertType

LOGGER = logging.getLogger("monitor.discord_channels")

# All emitted alert types have a stable home; one channel may serve several types.
CHANNEL_ALERT_TYPES: dict[str, tuple[AlertType, ...]] = {
    "lf-new-products": (AlertType.NEW_PRODUCT,),
    "lf-restocks": (AlertType.RESTOCK, AlertType.AVAILABILITY),
    "lf-preorders": (AlertType.PREORDER_OPEN, AlertType.BACKORDER, AlertType.COMING_SOON),
    "lf-price-changes": (AlertType.PRICE_DROP, AlertType.PRICE_INCREASE),
    "lf-releases": (
        AlertType.RELEASE_DATE_FOUND, AlertType.RELEASE_DATE_CHANGED,
        AlertType.RELEASE_TIME_FOUND, AlertType.RELEASE_TIME_CHANGED,
        AlertType.RELEASE_DATETIME_CHANGED, AlertType.RELEASING_SOON,
        AlertType.RELEASED, AlertType.ETA_CHANGED,
    ),
    "lf-stock-updates": (AlertType.LOW_STOCK, AlertType.OUT_OF_STOCK),
    "lf-product-updates": (
        AlertType.STATUS_CHANGE, AlertType.PRODUCT_UPDATED, AlertType.PRODUCT_REMOVED,
    ),
    "lf-monitor-status": (AlertType.MONITOR_ERROR, AlertType.MONITOR_RECOVERED),
}
CATEGORY_NAME = "Loungefly Alerts"
CREATION_REASON = "Loungefly Monitor automatic alert channel setup"


async def ensure_alert_channels(
    guild: discord.Guild, *, existing_ids: dict[str, int | None] | None = None,
    category_name: str = CATEGORY_NAME, create_missing: bool = True,
) -> dict[AlertType, discord.TextChannel]:
    """Reuse manually configured IDs, unique matching names, and create if allowed.

    A manual ID takes precedence over names. Stale/outside-guild IDs cannot
    silently create duplicates or redirect messages to the wrong channel.
    """
    routes: dict[AlertType, discord.TextChannel] = {}
    configured = existing_ids or {}
    category = next((cat for cat in guild.categories if cat.name == category_name), None)
    for name, alert_types in CHANNEL_ALERT_TYPES.items():
        configured_id = configured.get(name)
        if configured_id is not None:
            channel = next((c for c in guild.text_channels if c.id == configured_id), None)
            if channel is None:
                LOGGER.warning("discord_alert_channel_id_missing", extra={
                    "guild_id": guild.id, "channel_name": name, "channel_id": configured_id,
                })
                continue
        else:
            matches = [c for c in guild.text_channels if c.name == name]
            if len(matches) > 1:
                LOGGER.warning("discord_alert_channel_ambiguous", extra={
                    "guild_id": guild.id, "channel_name": name, "matches": len(matches),
                })
                continue
            channel = matches[0] if matches else None
            if channel is None and create_missing:
                if category is None:
                    try:
                        category = await guild.create_category(category_name, reason=CREATION_REASON)
                        LOGGER.info("discord_alert_category_created", extra={"guild_id": guild.id})
                    except discord.HTTPException:
                        LOGGER.warning("discord_alert_category_create_failed", extra={"guild_id": guild.id})
                try:
                    channel = await guild.create_text_channel(name, category=category, reason=CREATION_REASON)
                    LOGGER.info("discord_alert_channel_created", extra={
                        "guild_id": guild.id, "channel_id": channel.id, "channel_name": name,
                    })
                except discord.HTTPException:
                    LOGGER.warning("discord_alert_channel_create_failed", extra={
                        "guild_id": guild.id, "channel_name": name,
                    })
                    continue
        if channel is not None:
            for alert_type in alert_types:
                routes[alert_type] = channel
    return routes

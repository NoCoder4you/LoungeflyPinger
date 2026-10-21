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


async def ensure_alert_channels(guild: discord.Guild) -> dict[AlertType, discord.TextChannel]:
    """Reuse matching channels and create missing ones, never moving existing channels.

    Ambiguous duplicate names are deliberately skipped rather than routing to an
    arbitrary channel. Permission failures are isolated to their respective
    channels, leaving the legacy webhook fallback available.
    """
    routes: dict[AlertType, discord.TextChannel] = {}
    category = next((cat for cat in guild.categories if cat.name == CATEGORY_NAME), None)
    for name, alert_types in CHANNEL_ALERT_TYPES.items():
        matches = [channel for channel in guild.text_channels if channel.name == name]
        if len(matches) > 1:
            LOGGER.warning("discord_alert_channel_ambiguous", extra={
                "guild_id": guild.id, "channel_name": name, "matches": len(matches),
            })
            continue
        channel = matches[0] if matches else None
        if channel is None:
            if category is None:
                try:
                    category = await guild.create_category(CATEGORY_NAME, reason=CREATION_REASON)
                    LOGGER.info("discord_alert_category_created", extra={"guild_id": guild.id})
                except discord.HTTPException:
                    LOGGER.warning("discord_alert_category_create_failed", extra={"guild_id": guild.id})
                    # A channel may still be creatable without a category.
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
        for alert_type in alert_types:
            routes[alert_type] = channel
    return routes

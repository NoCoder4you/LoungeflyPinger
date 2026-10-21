"""Provision notification roles and bot-owned channel webhooks without duplicates."""

from __future__ import annotations

import logging

import discord

from app.notifications.channel_setup import CHANNEL_ALERT_TYPES, CREATION_REASON

LOGGER = logging.getLogger("monitor.discord_resources")

ROLE_NAMES: dict[str, str] = {
    "lf-new-products": "New Products",
    "lf-restocks": "Restocks",
    "lf-preorders": "Preorders",
    "lf-price-changes": "Price Changes",
    "lf-releases": "Releases",
    "lf-stock-updates": "Stock Updates",
    "lf-product-updates": "Product Updates",
    "lf-monitor-status": "Monitor Status",
}
WEBHOOK_NAMES: dict[str, str] = dict(ROLE_NAMES)

# Reuse and rename resources made by earlier versions instead of duplicating.
LEGACY_ROLE_NAMES = {key: f"LF Alerts | {name}" for key, name in ROLE_NAMES.items()}
LEGACY_WEBHOOK_NAMES = {key: f"Loungefly {name}" for key, name in WEBHOOK_NAMES.items()}


async def ensure_alert_roles(
    guild: discord.Guild,
    *,
    configured_ids: dict[str, int | None] | None = None,
    create_missing: bool = True,
) -> dict[str, discord.Role]:
    """Reuse IDs or uniquely named roles, never altering server role assignments."""
    resolved: dict[str, discord.Role] = {}
    ids = configured_ids or {}
    for key, name in ROLE_NAMES.items():
        selected_id = ids.get(key)
        if selected_id is not None:
            role = next((role for role in guild.roles if role.id == selected_id), None)
            if role is None:
                LOGGER.warning("discord_alert_role_id_missing", extra={
                    "guild_id": guild.id, "role_name": name, "role_id": selected_id,
                })
                continue
        else:
            matches = [role for role in guild.roles if role.name == name]
            if not matches:
                matches = [
                    role for role in guild.roles
                    if role.name == LEGACY_ROLE_NAMES[key]
                ]
            if len(matches) > 1:
                LOGGER.warning("discord_alert_role_ambiguous", extra={
                    "guild_id": guild.id, "role_name": name,
                })
                continue
            role = matches[0] if matches else None
            if role is None and create_missing:
                try:
                    role = await guild.create_role(
                        name=name, mentionable=True, reason=CREATION_REASON,
                    )
                    LOGGER.info("discord_alert_role_created", extra={
                        "guild_id": guild.id, "role_id": role.id, "role_name": name,
                    })
                except discord.HTTPException:
                    LOGGER.warning("discord_alert_role_create_failed", extra={
                        "guild_id": guild.id, "role_name": name,
                    })
                    continue
        if role is not None:
            if role.name == LEGACY_ROLE_NAMES[key]:
                try:
                    role = await role.edit(name=name, reason=CREATION_REASON)
                    LOGGER.info("discord_alert_role_renamed", extra={
                        "guild_id": guild.id, "role_id": role.id, "role_name": name,
                    })
                except discord.HTTPException:
                    LOGGER.warning("discord_alert_role_rename_failed", extra={
                        "guild_id": guild.id, "role_id": role.id,
                    })
            resolved[key] = role
    return resolved


async def ensure_alert_webhooks(
    channels: dict[str, discord.TextChannel],
    *,
    bot_user_id: int,
    configured_urls: dict[str, str | None] | None = None,
    managed_channel_ids: dict[str, int | None] | None = None,
    create_missing: bool = True,
) -> dict[str, tuple[int, str]]:
    """Return bot-owned webhooks and create missing ones; never touch manual URLs.

    Webhook enumeration must succeed before creating anything. A hook owned by a
    different application/user is never reused simply because its name matches.
    """
    result: dict[str, tuple[int, str]] = {}
    urls = configured_urls or {}
    managed_ids = managed_channel_ids or {}
    for key, channel in channels.items():
        previous_url = urls.get(key)
        previous_managed_channel_id = managed_ids.get(key)
        if previous_url and previous_managed_channel_id is None:
            # Explicit operator-provided webhook URLs are private overrides.
            continue
        if not create_missing and not previous_url:
            continue
        try:
            webhooks = await channel.webhooks()
        except discord.HTTPException:
            LOGGER.warning("discord_alert_webhooks_list_failed", extra={
                "channel_id": channel.id, "channel_key": key,
            })
            continue
        ours = [
            hook for hook in webhooks
            if hook.name in {WEBHOOK_NAMES[key], LEGACY_WEBHOOK_NAMES[key]}
            and hook.user is not None and hook.user.id == bot_user_id
            and hook.url
        ]
        matching = next((hook for hook in ours if hook.url == previous_url), None)
        if matching is None:
            if len(ours) == 1:
                matching = ours[0]
            elif len(ours) > 1:
                LOGGER.warning("discord_alert_webhooks_ambiguous", extra={
                    "channel_id": channel.id, "channel_key": key,
                })
                continue
        if matching is None and create_missing:
            try:
                matching = await channel.create_webhook(
                    name=WEBHOOK_NAMES[key], reason=CREATION_REASON,
                )
                LOGGER.info("discord_alert_webhook_created", extra={
                    "channel_id": channel.id, "channel_key": key,
                })
            except discord.HTTPException:
                LOGGER.warning("discord_alert_webhook_create_failed", extra={
                    "channel_id": channel.id, "channel_key": key,
                })
                continue
        if matching is not None and matching.url:
            if matching.name == LEGACY_WEBHOOK_NAMES[key]:
                try:
                    matching = await matching.edit(
                        name=WEBHOOK_NAMES[key], reason=CREATION_REASON,
                    )
                    LOGGER.info("discord_alert_webhook_renamed", extra={
                        "channel_id": channel.id, "channel_key": key,
                    })
                except discord.HTTPException:
                    LOGGER.warning("discord_alert_webhook_rename_failed", extra={
                        "channel_id": channel.id, "channel_key": key,
                    })
            result[key] = channel.id, matching.url
    return result

"""Secure, optional Discord application-command control plane."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from typing import Any, Awaitable, Callable

import discord
from discord import app_commands

from app.config import AppConfig, DiscordBotConfig
from app.database import Database
from app.services.watchlist_manager import StoredWatchRule, WatchlistManager
from app.services.retailer_manager import RetailerManager, ScanAlreadyRunning
from app.scheduler import Scheduler

LOGGER = logging.getLogger("monitor.discord_control")


def _csv(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


class DeleteConfirmation(discord.ui.View):
    def __init__(self, service: "DiscordControlService", user_id: int, rule_id: int) -> None:
        super().__init__(timeout=60)
        self.service, self.user_id, self.rule_id = service, user_id, rule_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the user who requested deletion may confirm it.", ephemeral=True)
            return False
        if not self.service.authorized(interaction):
            await self.service.audit(interaction, "denied:delete_confirmation", self.rule_id,
                                     self.service.manager.get(self.rule_id), None, False)
            await interaction.response.send_message(
                "Your authorization has changed; deletion was denied.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm deletion", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        before = self.service.manager.get(self.rule_id)
        try:
            await self.service.manager.delete(self.rule_id)
            await self.service.audit(interaction, "delete", self.rule_id, before, None, True)
            await interaction.response.edit_message(content=f"Deleted watch #{self.rule_id}.", view=None)
        except Exception:
            await self.service.audit(interaction, "delete", self.rule_id, before, None, False)
            await interaction.response.edit_message(content="Deletion failed; see service logs.", view=None)
            LOGGER.exception("discord_watch_delete_failed", extra={"rule_id": self.rule_id})
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Deletion cancelled.", view=None)
        self.stop()


class RetailerConfirmation(discord.ui.View):
    """User-bound, expiring confirmation for disruptive retailer changes."""
    def __init__(self, service: "DiscordControlService", user_id: int, key: str,
                 operation: str) -> None:
        super().__init__(timeout=60)
        self.service, self.user_id, self.key, self.operation = service, user_id, key, operation

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the requesting user may confirm.", ephemeral=True)
            return False
        if not self.service.authorized(interaction):
            await self.service.audit_retailer(
                interaction, f"denied:{self.operation}_confirmation", self.key,
                None, None, False)
            await interaction.response.send_message(
                "Your authorization has changed; this operation was denied.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert self.service.retailers is not None
        before = (await self.service.retailers.get_state(self.key)).as_dict()
        try:
            after = (await (self.service.retailers.set_enabled(self.key, False, str(self.user_id))
                     if self.operation == "disable" else self.service.retailers.reset(self.key))).as_dict()
            await self.service.audit_retailer(interaction, self.operation, self.key, before, after, True)
            await interaction.response.edit_message(content=f"{self.key} {self.operation} completed.", view=None)
        except Exception:
            await self.service.audit_retailer(interaction, self.operation, self.key, before, None, False)
            await interaction.response.edit_message(content="Operation failed; see service logs.", view=None)
            LOGGER.exception("discord_retailer_confirmation_failed")
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Retailer change cancelled.", view=None)
        self.stop()


class DiscordControlService:
    """Own the Discord client without allowing its failures to stop monitoring."""

    def __init__(self, config: DiscordBotConfig, manager: WatchlistManager, database: Database,
                 app_config: AppConfig, retailers: RetailerManager | None = None,
                 scheduler: Scheduler | None = None) -> None:
        self.config, self.manager, self.database, self.app_config = config, manager, database, app_config
        self.started_at = time.monotonic()
        self.retailers, self.scheduler = retailers, scheduler
        self.client: discord.Client | None = None
        self.tree: app_commands.CommandTree | None = None
        self._task: asyncio.Task[None] | None = None
        if config.enabled:
            self.client = discord.Client(intents=discord.Intents.none())
            self.tree = app_commands.CommandTree(self.client)
            self._register_commands()
            self.client.event(self._on_ready)

    async def _on_ready(self) -> None:
        assert self.tree is not None
        if self.config.guild_id:
            await self.tree.sync(guild=discord.Object(id=self.config.guild_id))
        else:
            await self.tree.sync()
        LOGGER.info("discord_control_ready", extra={"guild_scoped": bool(self.config.guild_id)})

    async def start(self) -> None:
        if not self.config.enabled:
            return
        assert self.client is not None and self.config.token is not None
        self._task = asyncio.create_task(self.client.start(self.config.token), name="discord-control")
        self._task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            LOGGER.exception("discord_control_stopped_unexpectedly")

    async def close(self) -> None:
        if self.client is not None and not self.client.is_closed():
            await self.client.close()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def authorized(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self.config.allowed_user_ids:
            return True
        role_ids = {role.id for role in getattr(interaction.user, "roles", ())}
        return bool(role_ids & self.config.allowed_role_ids)

    async def require_authorized(self, interaction: discord.Interaction, action: str) -> bool:
        if self.authorized(interaction):
            return True
        if action.startswith("retailer:"):
            await self.audit_retailer(interaction, f"denied:{action}", None, None, None, False)
            message = "You are not authorized to perform this retailer operation."
        elif action.startswith("product:"):
            await self.audit(interaction, f"denied:{action}", None, None, None, False)
            message = "You are not authorized to read the product catalogue."
        else:
            await self.audit(interaction, f"denied:{action}", None, None, None, False)
            message = "You are not authorized to manage watches."
        await interaction.response.send_message(message, ephemeral=True)
        return False

    async def audit(self, interaction: discord.Interaction, action: str, resource_id: int | None,
                    before: StoredWatchRule | None, after: StoredWatchRule | None,
                    success: bool) -> None:
        connection = self.database.connection
        if connection is None:
            return
        from datetime import UTC, datetime
        async with self.database.write_lock:
            await connection.execute(
                """INSERT INTO discord_audit_log(timestamp,discord_user_id,guild_id,channel_id,
                   action,resource_type,resource_id,before_state,after_state,success)
                   VALUES(?,?,?,?,?,'watch',?,?,?,?)""",
                (datetime.now(UTC).isoformat(), str(interaction.user.id),
                 str(interaction.guild_id) if interaction.guild_id else None,
                 str(interaction.channel_id) if interaction.channel_id else None, action,
                 str(resource_id) if resource_id is not None else None,
                 json.dumps(before.as_dict(), default=str) if before else None,
                 json.dumps(after.as_dict(), default=str) if after else None, success),
            )
            await connection.commit()

    async def audit_retailer(self, interaction: discord.Interaction, action: str, key: str | None,
                             before: dict[str, Any] | None, after: dict[str, Any] | None,
                             success: bool) -> None:
        if self.database.connection is None: return
        from datetime import UTC, datetime
        async with self.database.write_lock:
            await self.database.connection.execute("""INSERT INTO discord_audit_log
                (timestamp,discord_user_id,guild_id,channel_id,action,resource_type,resource_id,before_state,after_state,success)
                VALUES(?,?,?,?,?,'retailer',?,?,?,?)""", (datetime.now(UTC).isoformat(), str(interaction.user.id),
                str(interaction.guild_id) if interaction.guild_id else None,
                str(interaction.channel_id) if interaction.channel_id else None, action, key,
                json.dumps(before, default=str) if before else None, json.dumps(after, default=str) if after else None, success))
            await self.database.connection.commit()

    def _register_commands(self) -> None:
        assert self.tree is not None
        group = app_commands.Group(name="watch", description="Manage product watch rules")

        @group.command(name="list", description="List configured watch rules")
        async def list_rules(interaction: discord.Interaction) -> None:
            lines = [self._summary(item) for item in self.manager.items]
            content = "\n".join(lines) or "No watch rules are configured."
            await interaction.response.send_message(content[:1900], ephemeral=True)

        @group.command(name="show", description="Show a complete watch rule")
        async def show(interaction: discord.Interaction, id: int) -> None:
            item = self.manager.get(id)
            content = json.dumps(item.as_dict(), indent=2, default=str) if item else "Watch rule not found."
            await interaction.response.send_message(f"```json\n{content[:1800]}\n```", ephemeral=True)

        @group.command(name="add", description="Create a watch rule")
        async def add(interaction: discord.Interaction, name: str, keywords: str = "",
                      excluded_keywords: str = "", franchise: str = "", character: str = "",
                      retailer: str = "", product_type: str = "", maximum_price: float | None = None,
                      exact_url: str = "", preorder: bool | None = None,
                      exclusive: bool | None = None, priority: str = "normal") -> None:
            if not await self.require_authorized(interaction, "add"): return
            value: dict[str, Any] = {"name": name, "priority": priority}
            for key, raw in (("keywords", keywords), ("excluded_keywords", excluded_keywords),
                             ("franchises", franchise), ("characters", character),
                             ("retailers", retailer), ("product_types", product_type)):
                if parsed := _csv(raw): value[key] = parsed
            if maximum_price is not None: value["max_price"] = maximum_price
            if exact_url: value["exact_url"] = exact_url
            if preorder is not None: value["preorder"] = preorder
            if exclusive is not None: value["exclusive"] = exclusive
            await self._mutate(interaction, "add", None,
                               lambda: self.manager.add(value, str(interaction.user.id)))

        @group.command(name="edit", description="Modify common fields on a watch rule")
        async def edit(interaction: discord.Interaction, id: int, name: str | None = None,
                       keywords: str | None = None, excluded_keywords: str | None = None,
                       franchise: str | None = None, character: str | None = None,
                       retailer: str | None = None, product_type: str | None = None,
                       exact_url: str | None = None, maximum_price: float | None = None,
                       preorder: bool | None = None, exclusive: bool | None = None,
                       priority: str | None = None) -> None:
            if not await self.require_authorized(interaction, "edit"): return
            changes: dict[str, Any] = {}
            if name is not None: changes["name"] = name
            if keywords is not None: changes["keywords"] = _csv(keywords)
            if excluded_keywords is not None: changes["excluded_keywords"] = _csv(excluded_keywords)
            for key, raw in (("franchises", franchise), ("characters", character),
                             ("retailers", retailer), ("product_types", product_type)):
                if raw is not None: changes[key] = _csv(raw)
            if exact_url is not None: changes["exact_url"] = exact_url
            if maximum_price is not None: changes["max_price"] = maximum_price
            if preorder is not None: changes["preorder"] = preorder
            if exclusive is not None: changes["exclusive"] = exclusive
            if priority is not None: changes["priority"] = priority
            await self._mutate(interaction, "edit", id,
                               lambda: self.manager.edit(id, changes, str(interaction.user.id)))

        @group.command(name="delete", description="Delete a watch after confirmation")
        async def delete(interaction: discord.Interaction, id: int) -> None:
            if not await self.require_authorized(interaction, "delete"): return
            if self.manager.get(id) is None:
                await interaction.response.send_message("Watch rule not found.", ephemeral=True); return
            await interaction.response.send_message(f"Delete watch #{id}?", ephemeral=True,
                                                    view=DeleteConfirmation(self, interaction.user.id, id))

        async def toggle(interaction: discord.Interaction, id: int, enabled: bool) -> None:
            action = "enable" if enabled else "disable"
            if not await self.require_authorized(interaction, action): return
            await self._mutate(interaction, action, id, lambda: self.manager.set_enabled(
                id, enabled, str(interaction.user.id)))

        @group.command(name="enable", description="Enable a watch rule")
        async def enable(interaction: discord.Interaction, id: int) -> None: await toggle(interaction, id, True)

        @group.command(name="disable", description="Disable a watch rule")
        async def disable(interaction: discord.Interaction, id: int) -> None: await toggle(interaction, id, False)

        @group.command(name="export", description="Export watch rules as YAML")
        async def export(interaction: discord.Interaction) -> None:
            data = io.BytesIO(self.manager.export_yaml().encode())
            await interaction.response.send_message(file=discord.File(data, filename="watchlist.yaml"), ephemeral=True)

        guild = discord.Object(id=self.config.guild_id) if self.config.guild_id else None
        self.tree.add_command(group, guild=guild)

        from app.product_commands import register_product_commands
        register_product_commands(self, guild)

        if self.retailers is not None:
            retailer_group = app_commands.Group(name="retailer", description="Manage retailer monitoring")

            async def known(interaction: discord.Interaction, key: str):
                if key not in self.retailers.registry:
                    await interaction.response.send_message("Unknown retailer key.", ephemeral=True); return None
                return await self.retailers.get_state(key)

            @retailer_group.command(name="list", description="List retailer state and health")
            async def retailer_list(interaction: discord.Interaction) -> None:
                states = await self.retailers.list_states()
                lines = [f"{'✅' if s.enabled else '⏸️'} `{s.key}` — {s.health}, {s.interval_minutes:g}m" for s in states]
                await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

            @retailer_group.command(name="show", description="Show retailer configuration and health")
            async def retailer_show(interaction: discord.Interaction, retailer: str) -> None:
                state = await known(interaction, retailer)
                if state: await interaction.response.send_message(f"```json\n{json.dumps(state.as_dict(), indent=2, default=str)[:1800]}\n```", ephemeral=True)

            @retailer_group.command(name="enable", description="Enable scheduled monitoring")
            async def retailer_enable(interaction: discord.Interaction, retailer: str) -> None:
                if not await self.require_authorized(interaction, "retailer:configure"): return
                state = await known(interaction, retailer)
                if not state: return
                after = await self.retailers.set_enabled(retailer, True, str(interaction.user.id))
                await self.audit_retailer(interaction, "enable", retailer, state.as_dict(), after.as_dict(), True)
                await interaction.response.send_message(f"Enabled `{retailer}` at {after.interval_minutes:g} minutes.", ephemeral=True)

            @retailer_group.command(name="disable", description="Disable scheduled monitoring")
            async def retailer_disable(interaction: discord.Interaction, retailer: str) -> None:
                if not await self.require_authorized(interaction, "retailer:configure"): return
                if not await known(interaction, retailer): return
                await interaction.response.send_message(f"Disable `{retailer}`? An active scan will finish safely.", ephemeral=True,
                    view=RetailerConfirmation(self, interaction.user.id, retailer, "disable"))

            @retailer_group.command(name="interval", description="Set the main scan interval")
            async def retailer_interval(interaction: discord.Interaction, retailer: str, minutes: float) -> None:
                if not await self.require_authorized(interaction, "retailer:configure"): return
                before = await known(interaction, retailer)
                if not before: return
                try: after = await self.retailers.set_interval(retailer, minutes, str(interaction.user.id))
                except ValueError as exc: await interaction.response.send_message(str(exc), ephemeral=True); return
                await self.audit_retailer(interaction, "interval", retailer, before.as_dict(), after.as_dict(), True)
                await interaction.response.send_message(f"`{retailer}`: {before.interval_minutes:g} → {after.interval_minutes:g}m (YAML {after.default_interval_minutes:g}m).", ephemeral=True)

            @retailer_group.command(name="reset", description="Restore startup YAML defaults")
            async def retailer_reset(interaction: discord.Interaction, retailer: str) -> None:
                if not await self.require_authorized(interaction, "retailer:configure"): return
                state = await known(interaction, retailer)
                if not state: return
                if state.enabled != state.default_enabled:
                    await interaction.response.send_message(f"Reset `{retailer}` and change enabled state?", ephemeral=True,
                        view=RetailerConfirmation(self, interaction.user.id, retailer, "reset")); return
                after = await self.retailers.reset(retailer)
                await self.audit_retailer(interaction, "reset", retailer, state.as_dict(), after.as_dict(), True)
                await interaction.response.send_message(f"Reset `{retailer}` to YAML defaults.", ephemeral=True)

            @retailer_group.command(name="scan", description="Run one isolated scan now")
            async def retailer_scan(interaction: discord.Interaction, retailer: str) -> None:
                if not await self.require_authorized(interaction, "retailer:scan"): return
                if retailer not in self.retailers.registry:
                    await interaction.response.send_message("Unknown retailer key.", ephemeral=True); return
                await interaction.response.defer(ephemeral=True)
                try:
                    result = await self.retailers.scan(retailer, str(interaction.user.id))
                    await self.audit_retailer(interaction, "manual_scan", retailer, None, result, result["success"])
                    await interaction.followup.send(f"`{retailer}` scan {'succeeded' if result['success'] else 'failed'} in {result['duration']:.1f}s; {result['alert_count']} alerts; health {result['health']}. Scheduled state remains {'enabled' if result['enabled'] else 'disabled'}.", ephemeral=True)
                except ScanAlreadyRunning as exc:
                    await self.audit_retailer(interaction, "manual_scan_rejected", retailer, None, {"reason": "already_running"}, False)
                    await interaction.followup.send(str(exc), ephemeral=True)
                except Exception:
                    await self.audit_retailer(interaction, "manual_scan", retailer, None, {"error": "internal failure"}, False)
                    LOGGER.exception("discord_manual_scan_failed", extra={"retailer": retailer})
                    await interaction.followup.send("Scan failed; see service logs.", ephemeral=True)

            @retailer_group.command(name="failures", description="Show recent failed scans")
            async def retailer_failures(interaction: discord.Interaction, retailer: str) -> None:
                if retailer not in self.retailers.registry:
                    await interaction.response.send_message("Unknown retailer key.", ephemeral=True); return
                rows = await self.retailers.failures(retailer)
                text = "\n".join(f"{r['completed_at']} [{r['trigger']}] HTTP {r['http_status'] or '-'}: {r['error'] or 'unknown error'}" for r in rows) or "No recorded failures."
                await interaction.response.send_message(text[:1900], ephemeral=True)

            self.tree.add_command(retailer_group, guild=guild)

        @self.tree.command(name="status", description="Show monitor status", guild=guild)
        async def status(interaction: discord.Interaction) -> None:
            states = await self.retailers.list_states() if self.retailers else []
            enabled = sum(s.enabled for s in states) if states else sum(bool(v.get("enabled")) for v in self.app_config.retailers.values() if isinstance(v, dict))
            latency = self.client.latency * 1000 if self.client else 0
            await interaction.response.send_message(
                f"Application: running\nActive watches: {len(self.manager.current.rules)}\n"
                f"Retailers: {enabled}/{len(states) or len(self.app_config.retailers)} enabled; running scans: {sum(s.running for s in states)}\nDatabase: connected\n"
                f"Bot latency: {latency:.0f} ms\nUptime: {int(time.monotonic()-self.started_at)}s", ephemeral=True)

        @self.tree.command(name="retailers", description="Show retailer health", guild=guild)
        async def retailers(interaction: discord.Interaction) -> None:
            rows = await (await self.database.connection.execute(
                "SELECT name,enabled,health,consecutive_failures,last_success FROM retailers ORDER BY name"
            )).fetchall()  # type: ignore[union-attr]
            lines = [f"{r[0]}: {r[2]} ({r[3]} failures)" for r in rows if r[1]]
            await interaction.response.send_message(("\n".join(lines) or "No enabled retailers.")[:1900], ephemeral=True)

    async def _mutate(self, interaction: discord.Interaction, action: str, rule_id: int | None,
                      operation: Callable[[], Awaitable[StoredWatchRule]]) -> None:
        before = self.manager.get(rule_id) if rule_id is not None else None
        try:
            after = await operation()
            await self.audit(interaction, action, after.id, before, after, True)
            await interaction.response.send_message(f"Watch #{after.id} {action} succeeded.", ephemeral=True)
        except (ValueError, KeyError) as exc:
            await self.audit(interaction, action, rule_id, before, None, False)
            await interaction.response.send_message(str(exc), ephemeral=True)
        except Exception:
            await self.audit(interaction, action, rule_id, before, None, False)
            LOGGER.exception("discord_watch_mutation_failed", extra={"action": action, "rule_id": rule_id})
            await interaction.response.send_message("Operation failed; see service logs.", ephemeral=True)

    @staticmethod
    def _summary(item: StoredWatchRule) -> str:
        filters = item.rule.keywords or item.rule.franchises or item.rule.characters or item.rule.retailers
        return f"#{item.id} {item.rule.name} [{'on' if item.enabled else 'off'}] {item.rule.priority.value}: {', '.join(filters) or 'exact/other filters'}"

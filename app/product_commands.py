"""Discord presentation adapter for the product-intelligence services."""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable

import discord
from discord import app_commands

from app.models import Availability
from app.services.product_query_service import Page, ProductQuery, ProductQueryService

LOGGER = logging.getLogger("monitor.discord_products")


def _price(item) -> str:
    return f"{item.currency} {item.price}" if item.price is not None else "Price unknown"


def page_embed(page: Page, title: str) -> discord.Embed:
    embed = discord.Embed(title=title, description=f"Page {page.page}/{page.pages} · {page.total} result(s)")
    for item in page.items:
        detail = f"**{item.retailer}** · {_price(item)} · {item.availability or 'UNKNOWN'}"
        classifications = " · ".join(filter(None, (item.franchise, item.product_type)))
        if classifications: detail += f"\n{classifications}"
        detail += f"\n[View product]({item.url})"
        embed.add_field(name=f"#{item.id} {item.name}"[:256], value=detail[:1024], inline=False)
    if not page.items:
        embed.description += "\nNo matching products."
    return embed


class ProductPagination(discord.ui.View):
    """Expiring navigation which reruns one bounded database page query."""
    def __init__(self, user_id: int, loader: Callable[[int], Awaitable[Page]], page: Page,
                 title: str, renderer: Callable[[Page, str], discord.Embed] = page_embed) -> None:
        super().__init__(timeout=120)
        self.user_id, self.loader, self.page, self.title, self.renderer = user_id, loader, page, title, renderer
        self._sync()

    def _sync(self) -> None:
        self.previous.disabled = self.page.page <= 1
        self.next.disabled = self.page.page >= self.page.pages

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.is_finished():
            await interaction.response.send_message("This result page has expired.", ephemeral=True)
            return False
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the requesting user can navigate these results.", ephemeral=True)
            return False
        return True

    async def _move(self, interaction: discord.Interaction, page: int) -> None:
        try:
            self.page = await self.loader(page); self._sync()
            await interaction.response.edit_message(embed=self.renderer(self.page, self.title), view=self)
        except Exception:
            LOGGER.exception("product_pagination_failed")
            await interaction.response.send_message("The catalogue query failed safely.", ephemeral=True)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._move(interaction, max(1, self.page.page - 1))

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._move(interaction, self.page.page + 1)


async def _respond(interaction, title: str, loader: Callable[[int], Awaitable[Page]]) -> None:
    try:
        page = await loader(1)
        view = ProductPagination(interaction.user.id, loader, page, title) if page.pages > 1 else None
        await interaction.response.send_message(embed=page_embed(page, title), view=view, ephemeral=True)
    except (ValueError, InvalidOperation):
        await interaction.response.send_message("Invalid catalogue filter.", ephemeral=True)
    except Exception:
        LOGGER.exception("product_query_failed")
        await interaction.response.send_message("The catalogue query failed safely.", ephemeral=True)


def register_product_commands(control, guild: discord.Object | None) -> None:
    """Register thin, authorized adapters on the existing command tree."""
    service = ProductQueryService(control.database)
    group = app_commands.Group(name="product", description="Search product intelligence")

    @group.command(name="search", description="Search persisted products")
    async def search(interaction: discord.Interaction, query: str = "", retailer: str = "",
                     franchise: str = "", character: str = "", product_type: str = "",
                     availability: str = "", min_price: float | None = None,
                     max_price: float | None = None, exclusive: bool | None = None,
                     preorder: bool | None = None) -> None:
        if not await control.require_authorized(interaction, "product:read"): return
        try: stock = Availability(availability.upper()) if availability else None
        except ValueError:
            await interaction.response.send_message("Unknown availability value.", ephemeral=True); return
        filters = ProductQuery(text=query or None, retailer=retailer or None,
            franchise=franchise or None, character=character or None, product_type=product_type or None,
            availability=stock, minimum_price=Decimal(str(min_price)) if min_price is not None else None,
            maximum_price=Decimal(str(max_price)) if max_price is not None else None,
            exclusive=exclusive, preorder=preorder)
        await _respond(interaction, "Product search", lambda page: service.search(filters, page=page))

    async def autocomplete(field: str, interaction: discord.Interaction, current: str):
        if not control.authorized(interaction):
            return []
        return [app_commands.Choice(name=value, value=value)
                for value in await service.distinct(field, current)]

    @search.autocomplete("retailer")
    async def search_retailer_autocomplete(interaction: discord.Interaction, current: str):
        return await autocomplete("retailer", interaction, current)

    @search.autocomplete("franchise")
    async def search_franchise_autocomplete(interaction: discord.Interaction, current: str):
        return await autocomplete("franchise", interaction, current)

    @search.autocomplete("character")
    async def search_character_autocomplete(interaction: discord.Interaction, current: str):
        return await autocomplete("character", interaction, current)

    @search.autocomplete("product_type")
    async def search_type_autocomplete(interaction: discord.Interaction, current: str):
        return await autocomplete("product_type", interaction, current)

    @group.command(name="available", description="List currently orderable products")
    async def available(interaction: discord.Interaction, retailer: str = "", franchise: str = "",
                        character: str = "", product_type: str = "", maximum_price: float | None = None,
                        exclusive: bool | None = None, include_preorders: bool = False) -> None:
        if not await control.require_authorized(interaction, "product:read"): return
        query = ProductQuery(retailer=retailer or None, franchise=franchise or None,
            character=character or None, product_type=product_type or None,
            maximum_price=Decimal(str(maximum_price)) if maximum_price is not None else None,
            exclusive=exclusive, include_preorders=include_preorders)
        await _respond(interaction, "Available products", lambda page: service.available(query, page=page))

    @group.command(name="recent", description="Show newly discovered products")
    async def recent(interaction: discord.Interaction, period: str = "24h") -> None:
        if not await control.require_authorized(interaction, "product:read"): return
        periods = {"1h": timedelta(hours=1), "6h": timedelta(hours=6), "24h": timedelta(hours=24),
                   "3d": timedelta(days=3), "7d": timedelta(days=7), "30d": timedelta(days=30)}
        if period not in periods:
            await interaction.response.send_message("Period must be 1h, 6h, 24h, 3d, 7d, or 30d.", ephemeral=True); return
        await _respond(interaction, f"Discovered in {period}", lambda page: service.recent(periods[period], page=page))

    @group.command(name="show", description="Show a complete product intelligence card")
    async def show(interaction: discord.Interaction, id: int) -> None:
        if not await control.require_authorized(interaction, "product:read"): return
        item = await service.get(id)
        if item is None:
            await interaction.response.send_message("Product not found.", ephemeral=True); return
        embed = discord.Embed(title=f"#{item.id} {item.name}", url=item.url,
            description="**REMOVED**" if item.removed_at else None)
        fields = [("Retailer", item.retailer), ("Availability", item.availability), ("Price", _price(item)),
                  ("Type", item.product_type), ("Franchise", item.franchise),
                  ("Characters", ", ".join(filter(None, (item.character, *item.characters)))),
                  ("SKU", item.sku), ("Barcode", item.barcode),
                  ("Loungefly code", item.loungefly_product_code), ("First seen", item.first_seen),
                  ("Last seen", item.last_seen), ("Release", item.metadata.get("release_datetime") or item.metadata.get("release_date") or item.metadata.get("release_text")),
                  ("ETA", item.metadata.get("estimated_arrival_text") or item.metadata.get("estimated_arrival_date"))]
        for name, value in fields:
            if value not in (None, ""): embed.add_field(name=name, value=str(value)[:1024], inline=True)
        if item.image_url: embed.set_thumbnail(url=item.image_url)
        view = discord.ui.View(); view.add_item(discord.ui.Button(label="View Product", url=item.url))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @group.command(name="history", description="Show meaningful product changes")
    async def history(interaction: discord.Interaction, id: int) -> None:
        if not await control.require_authorized(interaction, "product:read"): return
        async def loader(page: int) -> Page: return await service.history(id, page=page)
        def render(page: Page, title: str) -> discord.Embed:
            embed = discord.Embed(title=title, description=f"Page {page.page}/{page.pages} · {page.total} event(s)")
            for row in page.items:
                embed.add_field(name=f"{row['timestamp']} · {row['kind']}",
                    value=" · ".join(str(v) for v in (row['availability'], row['price'], row['currency'], row['detail']) if v) or "Change detected", inline=False)
            return embed
        page = await loader(1); embed = render(page, f"Product #{id} history")
        view = ProductPagination(interaction.user.id, loader, page, f"Product #{id} history", render) if page.pages > 1 else None
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @group.command(name="offers", description="Find likely equivalent retailer offers")
    async def offers(interaction: discord.Interaction, id: int) -> None:
        if not await control.require_authorized(interaction, "product:read"): return
        matches = await service.offers(id)
        embed = discord.Embed(title=f"Offers for product #{id}", description="No equivalent listings found." if len(matches) <= 1 else None)
        for offer in matches:
            item = offer.product
            embed.add_field(name=f"{offer.confidence} · {item.retailer}",
                value=f"#{item.id} {item.name}\n{_price(item)} · {item.availability}" +
                      (" · **cheapest in currency**" if offer.cheapest_same_currency else "") + f"\n[View]({item.url})", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def filtered(interaction, title, method, retailer="", franchise="", product_type=""):
        if not await control.require_authorized(interaction, "product:read"): return
        query = ProductQuery(retailer=retailer or None, franchise=franchise or None, product_type=product_type or None)
        await _respond(interaction, title, lambda page: method(query, page=page))

    @group.command(name="releases", description="Show release-related products")
    async def releases(interaction: discord.Interaction, retailer: str = "", franchise: str = "", product_type: str = "") -> None:
        await filtered(interaction, "Upcoming releases", service.releases, retailer, franchise, product_type)

    @group.command(name="preorders", description="Show open preorders")
    async def preorders(interaction: discord.Interaction, retailer: str = "", franchise: str = "", product_type: str = "") -> None:
        await filtered(interaction, "Open preorders", service.preorders, retailer, franchise, product_type)

    @group.command(name="sales", description="Show sale, clearance and last-chance products")
    async def sales(interaction: discord.Interaction, retailer: str = "", franchise: str = "", product_type: str = "") -> None:
        await filtered(interaction, "Sales", service.sales, retailer, franchise, product_type)

    @group.command(name="exclusives", description="Show exclusive products")
    async def exclusives(interaction: discord.Interaction, retailer: str = "",
                         exclusive_retailer: str = "", region: str = "",
                         disney_parks: bool | None = None,
                         event_exclusive: bool | None = None,
                         exclusive_type: str = "") -> None:
        if not await control.require_authorized(interaction, "product:read"): return
        query = ProductQuery(retailer=retailer or None,
            exclusive_retailer=exclusive_retailer or None, exclusive_region=region or None,
            disney_parks=disney_parks, event_exclusive=event_exclusive,
            exclusive_type=exclusive_type or None)
        await _respond(interaction, "Exclusives", lambda page: service.exclusives(query, page=page))

    control.tree.add_command(group, guild=guild)

    alerts = app_commands.Group(name="alerts", description="Product event intelligence")
    @alerts.command(name="recent", description="Show recently detected product events")
    async def alert_recent(interaction: discord.Interaction, alert_type: str = "", retailer: str = "", product: int | None = None) -> None:
        if not await control.require_authorized(interaction, "product:read"): return
        page = await service.events(event_type=alert_type or None, retailer=retailer or None, product_id=product)
        embed = discord.Embed(title="Recent product events", description=f"{page.total} event(s)")
        for row in page.items:
            embed.add_field(name=f"{row['detected_at']} · {row['event_type']}",
                value=f"#{row['product_id']} {row['name']} · {row['retailer']}\n{row['new_summary'] or row['availability'] or ''}"[:1024], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    control.tree.add_command(alerts, guild=guild)

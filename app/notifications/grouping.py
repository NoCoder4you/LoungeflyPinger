"""Combine retailer links for the same bag in recently posted Discord messages.

The alert episode/delivery keys remain per listing.  Grouping only changes how a
successful notification is displayed in its destination.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.models import Alert, AlertType
from app.retailers import retailer_brand

GROUP_WINDOW = timedelta(hours=6)
_MAX_EMBED_CHARS = 6000
_MAX_FIELD_CHARS = 1024
_RETAILER_FIELDS = {
    "Retailer", "Status", "Price", "Saving", "Previous Status",
    "Quick Links", "EMP Regional Stores", "Retailer ETA",
    "SKU", "Product ID", "Exclusive", "Exclusive Retailer", "Exclusive Region",
    "First Seen", "Restocked",
}


def group_key(alert: Alert) -> str | None:
    """Use conservative, complete product names; never merge variant titles.

    SKU and retailer product IDs are not reliable cross-store identifiers.  We
    remove only the manufacturer prefix and punctuation, keeping colours, sizes,
    character names and editions in the identity.
    """
    product = alert.product
    if product is None or alert.is_admin or alert.alert_type is AlertType.PRODUCT_REMOVED:
        return None
    name = re.sub(r"\bloungefly\b", " ", product.name, flags=re.I)
    words = re.findall(r"[a-z0-9]+", name.casefold())
    kind = re.findall(r"[a-z0-9]+", product.product_type.casefold())
    if not words:
        return None
    identity = json.dumps([sorted(words), sorted(kind)], separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def offer_for(alert: Alert) -> dict[str, str | None]:
    product = alert.product
    assert product is not None
    return {
        "retailer": product.retailer,
        "name": retailer_brand(product.retailer).display_name,
        "url": product.url,
        "cart_url": product.cart_url,
        "price": str(product.price) if product.price is not None else None,
        "currency": product.currency,
        "availability": product.availability.value,
        "barcode": re.sub(r"\D", "", product.barcode) if product.barcode else None,
        "loungefly_code": product.loungefly_product_code.casefold()
        if product.loungefly_product_code else None,
    }


@dataclass
class ExistingGroup:
    message_id: str
    created_at: str
    base_payload: dict[str, Any]
    offers: list[dict[str, str | None]]


def can_join(group: ExistingGroup, candidate: dict[str, str | None]) -> bool:
    if datetime.now(UTC) - datetime.fromisoformat(group.created_at) > GROUP_WINDOW:
        return False
    # A repeat alert from the same retailer is a new event, not another shop.
    if any(item["retailer"].casefold() == candidate["retailer"].casefold()
           for item in group.offers if item.get("retailer")):
        return False
    # A contradictory manufacturer code is positive evidence of a different bag.
    for item in group.offers:
        for identifier in ("barcode", "loungefly_code"):
            if item.get(identifier) and candidate.get(identifier) and (
                item[identifier] != candidate[identifier]
            ):
                return False
    return True


def _link_line(offer: dict[str, str | None]) -> str:
    name = (offer["name"] or offer["retailer"] or "Retailer").replace("[", "").replace("]", "")
    url = offer["url"]
    line = f"[{name}]({url})"
    if offer.get("price"):
        currency = offer.get("currency") or ""
        price = (f"£{offer['price']}" if currency == "GBP"
                 else f"${offer['price']}" if currency == "USD"
                 else f"{currency} {offer['price']}")
        line += f" — {price}"
    if offer.get("availability"):
        line += f" — {offer['availability'].replace('_', ' ').title()}"
    if offer.get("cart_url") and offer["cart_url"] != url:
        line += f" · [Cart]({offer['cart_url']})"
    return line


def combined_payload(base: dict[str, Any], offers: list[dict[str, str | None]]) -> dict[str, Any] | None:
    """Build a shared embed, or decline when Discord's limits would hide a link."""
    if len(offers) < 2:
        return deepcopy(base)
    payload = deepcopy(base)
    embed = payload["embeds"][0]
    embed.pop("author", None)
    embed.pop("url", None)
    # A single retailer's price/status and cart button are misleading after merging.
    fields = [field for field in embed.get("fields", []) if field["name"] not in _RETAILER_FIELDS]
    used = len(embed.get("title", "")) + len(embed.get("description", ""))
    used += sum(len(field["name"]) + len(field["value"]) for field in fields)
    chunk: list[str] = []
    chunks: list[str] = []
    for offer in offers:
        line = _link_line(offer)
        if len(line) > _MAX_FIELD_CHARS:
            return None
        if chunk and len("\n".join([*chunk, line])) > _MAX_FIELD_CHARS:
            chunks.append("\n".join(chunk))
            chunk = []
        chunk.append(line)
    if chunk:
        chunks.append("\n".join(chunk))
    for index, value in enumerate(chunks):
        name = "Retailer Links" if index == 0 else f"Retailer Links ({index + 1})"
        if len(fields) >= 25 or used + len(name) + len(value) > _MAX_EMBED_CHARS:
            return None
        fields.append({"name": name, "value": value, "inline": False})
        used += len(name) + len(value)
    embed["fields"] = fields
    payload["components"] = []
    payload["allowed_mentions"] = {"parse": []}
    # Never edit the existing notification's mention/content; no second ping.
    payload.pop("content", None)
    return payload


class GroupedAlertStore:
    """SQLite-backed message pointers survive restarts and route changes."""

    def __init__(self, database: Any) -> None:
        self.database = database

    async def find(self, alert: Alert, destination_key: str) -> ExistingGroup | None:
        key = group_key(alert)
        if key is None:
            return None
        db = self.database.connection
        if db is None:
            return None
        row = await (await db.execute(
            """SELECT message_id, created_at, base_payload, offers
               FROM discord_grouped_alerts
               WHERE destination_key=? AND alert_type=? AND bag_key=?""",
            (destination_key, alert.alert_type.value, key),
        )).fetchone()
        if row is None:
            return None
        try:
            group = ExistingGroup(row[0], row[1], json.loads(row[2]), json.loads(row[3]))
            return group if can_join(group, offer_for(alert)) else None
        except (ValueError, TypeError, KeyError):
            return None

    async def save(
        self, alert: Alert, destination_key: str, message_id: str,
        base_payload: dict[str, Any], offers: list[dict[str, str | None]],
        *, created_at: str | None = None,
    ) -> None:
        key = group_key(alert)
        db = self.database.connection
        if key is None or db is None:
            return
        now = datetime.now(UTC)
        await db.execute(
            """INSERT INTO discord_grouped_alerts
               (destination_key, alert_type, bag_key, message_id, created_at, base_payload, offers)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(destination_key, alert_type, bag_key) DO UPDATE SET
                 message_id=excluded.message_id, created_at=excluded.created_at,
                 base_payload=excluded.base_payload, offers=excluded.offers""",
            (destination_key, alert.alert_type.value, key, message_id,
             created_at or now.isoformat(), json.dumps(base_payload),
             json.dumps(offers)),
        )
        # Bounded disk growth even on a long-running Raspberry Pi monitor.
        await db.execute(
            "DELETE FROM discord_grouped_alerts WHERE created_at < ?",
            ((now - timedelta(days=7)).isoformat(),),
        )
        await db.commit()

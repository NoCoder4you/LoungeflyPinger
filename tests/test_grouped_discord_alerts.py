"""Cross-retailer Discord grouping with real message IDs and restart safety."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import NotificationConfig
from app.database import Database
from app.models import Alert, AlertType, Availability, Product
from app.notifications.discord import DiscordNotifier
from app.notifications.grouping import combined_payload, group_key, offer_for


class Response:
    status = 200

    def __init__(self, message_id="1000", status=200):
        self.message_id = message_id
        self.status = status
        self.released = False

    async def json(self):
        return {"id": self.message_id}

    def release(self):
        self.released = True


class Session:
    closed = False

    def __init__(self):
        self.posts = []
        self.patches = []
        self.next_id = 1000
        self.patch_status = 200

    async def post(self, url, *, json):
        self.posts.append((url, json))
        mid = str(self.next_id)
        self.next_id += 1
        return Response(mid)

    async def patch(self, url, *, json):
        self.patches.append((url, json))
        return Response(status=self.patch_status)


class Message:
    def __init__(self, mid):
        self.id = mid
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class Channel:
    id = 1200

    def __init__(self):
        self.sent = []
        self.messages = {}

    async def send(self, **kwargs):
        mid = 6000 + len(self.sent)
        self.sent.append(kwargs)
        msg = Message(mid)
        self.messages[mid] = msg
        return msg

    async def fetch_message(self, mid):
        return self.messages[mid]


def bag(retailer, *, name="Disney Stitch Floral Mini Backpack", barcode=None, code=None):
    return Product(
        retailer=retailer, retailer_product_id=retailer.lower(),
        name=name, url=f"https://{retailer.lower()}.example/stitch",
        availability=Availability.IN_STOCK,
        price=Decimal("69.99") if retailer == "GeekCore" else Decimal("74.99"),
        currency="GBP", barcode=barcode, loungefly_product_code=code,
    )


@pytest.mark.asyncio
async def test_two_retailers_edit_one_webhook_embed_without_second_ping(tmp_path: Path):
    async with Database(tmp_path / "combined.db") as db:
        session = Session()
        notifier = DiscordNotifier(
            NotificationConfig(discord_type_webhooks={
                AlertType.NEW_PRODUCT: "https://discord.example/api/webhooks/12/token",
            }),
            db, session=session,
        )
        notifier.set_auto_roles({AlertType.NEW_PRODUCT: 987})
        a = bag("GeekCore")
        b = bag("BoxLunch")
        assert await notifier.send(Alert(AlertType.NEW_PRODUCT, a))
        assert await notifier.send(Alert(AlertType.NEW_PRODUCT, b))
        assert len(session.posts) == 1
        assert len(session.patches) == 1
        assert session.posts[0][0].endswith("?with_components=true&wait=true")
        assert session.posts[0][1]["content"] == "<@&987>"
        assert session.patches[0][0].endswith("/messages/1000")
        patch = session.patches[0][1]
        assert "content" not in patch
        assert patch["components"] == []
        embed = patch["embeds"][0]
        assert embed["title"] == a.name
        assert "url" not in embed
        fields = {field["name"]: field["value"] for field in embed["fields"]}
        links = fields["Retailer Links"]
        assert "https://geekcore.example/stitch" in links
        assert "https://boxlunch.example/stitch" in links
        assert "£69.99" in links and "£74.99" in links
        assert not any(field in fields for field in ("Retailer", "Price", "Quick Links"))
        delivered = await (await db.connection.execute(
            "SELECT COUNT(*) FROM notification_deliveries"
        )).fetchone()
        assert delivered[0] == 2


@pytest.mark.asyncio
async def test_group_reused_after_restart(tmp_path: Path):
    path = tmp_path / "restart.db"
    url = "https://discord.example/api/webhooks/13/token"
    config = NotificationConfig(discord_webhook_url=url)
    async with Database(path) as db:
        session = Session()
        notifier = DiscordNotifier(config, db, session=session)
        assert await notifier.send(Alert(AlertType.RESTOCK, bag("GeekCore")))
    async with Database(path) as db:
        session = Session()
        notifier = DiscordNotifier(config, db, session=session)
        assert await notifier.send(Alert(AlertType.RESTOCK, bag("BoxLunch")))
        assert len(session.patches) == 1
        assert session.patches[0][0].endswith("/messages/1000")
        assert not session.posts


@pytest.mark.asyncio
async def test_channel_alerts_are_edited_instead_of_posted(tmp_path: Path):
    async with Database(tmp_path / "bot.db") as db:
        channel = Channel()
        notifier = DiscordNotifier(NotificationConfig(), db)
        notifier.set_auto_channels({AlertType.RESTOCK: channel})
        assert await notifier.send(Alert(AlertType.RESTOCK, bag("GeekCore")))
        assert await notifier.send(Alert(AlertType.RESTOCK, bag("BoxLunch")))
        assert len(channel.sent) == 1
        edits = channel.messages[6000].edits
        assert len(edits) == 1
        assert edits[0]["view"] is None
        assert edits[0]["allowed_mentions"].everyone is False
        fields = {f.name: f.value for f in edits[0]["embed"].fields}
        assert "geekcore.example" in fields["Retailer Links"]
        assert "boxlunch.example" in fields["Retailer Links"]


@pytest.mark.asyncio
async def test_different_variants_and_alert_types_not_grouped(tmp_path: Path):
    async with Database(tmp_path / "variants.db") as db:
        session = Session()
        notifier = DiscordNotifier(NotificationConfig(discord_webhook_url="https://example.com/hook"), db, session=session)
        assert await notifier.send(Alert(AlertType.NEW_PRODUCT, bag("GeekCore")))
        assert await notifier.send(Alert(AlertType.NEW_PRODUCT, bag("BoxLunch", name="Disney Stitch Blue Mini Backpack")))
        assert await notifier.send(Alert(AlertType.RESTOCK, bag("BoxLunch")))
        assert not session.patches
        assert len(session.posts) == 3


@pytest.mark.asyncio
async def test_same_retailer_new_episode_does_not_modify_old_alert(tmp_path: Path):
    async with Database(tmp_path / "episodes.db") as db:
        session = Session()
        notifier = DiscordNotifier(NotificationConfig(discord_webhook_url="https://example.com/hook"), db, session=session)
        assert await notifier.send(Alert(AlertType.RESTOCK, bag("GeekCore"), occurrence_id="first"))
        assert await notifier.send(Alert(AlertType.RESTOCK, bag("GeekCore"), occurrence_id="second"))
        assert not session.patches
        assert len(session.posts) == 2


@pytest.mark.asyncio
async def test_conflicting_barcodes_keep_offers_separate(tmp_path: Path):
    async with Database(tmp_path / "barcodes.db") as db:
        session = Session()
        notifier = DiscordNotifier(NotificationConfig(discord_webhook_url="https://example.com/hook"), db, session=session)
        assert await notifier.send(Alert(AlertType.RESTOCK, bag("GeekCore", barcode="123456")))
        assert await notifier.send(Alert(AlertType.RESTOCK, bag("BoxLunch", barcode="654321")))
        assert not session.patches
        assert len(session.posts) == 2


@pytest.mark.asyncio
async def test_failed_edit_sends_new_alert_instead_of_losing_link(tmp_path: Path):
    async with Database(tmp_path / "edit-fail.db") as db:
        session = Session()
        notifier = DiscordNotifier(NotificationConfig(discord_webhook_url="https://example.com/hook"), db, session=session)
        assert await notifier.send(Alert(AlertType.RESTOCK, bag("GeekCore")))
        session.patch_status = 404
        assert await notifier.send(Alert(AlertType.RESTOCK, bag("BoxLunch")))
        assert len(session.patches) == 1
        assert len(session.posts) == 2


@pytest.mark.asyncio
async def test_expired_group_posts_fresh_message(tmp_path: Path):
    async with Database(tmp_path / "expired.db") as db:
        session = Session()
        notifier = DiscordNotifier(NotificationConfig(discord_webhook_url="https://example.com/hook"), db, session=session)
        assert await notifier.send(Alert(AlertType.RESTOCK, bag("GeekCore")))
        await db.connection.execute(
            "UPDATE discord_grouped_alerts SET created_at=?",
            ((datetime.now(UTC) - timedelta(hours=7)).isoformat(),),
        )
        await db.connection.commit()
        assert await notifier.send(Alert(AlertType.RESTOCK, bag("BoxLunch")))
        assert not session.patches
        assert len(session.posts) == 2


def test_combined_payload_respects_field_limits_and_falls_back_if_needed():
    source = Alert(AlertType.NEW_PRODUCT, bag("GeekCore"))
    from app.notifications.discord import build_discord_payload
    original = build_discord_payload(source)
    offers = [offer_for(source)] + [
        offer_for(Alert(AlertType.NEW_PRODUCT, replace(
            bag(f"Retailer{i}"), url=f"https://example.com/{i}/stitch",
        )))
        for i in range(200)
    ]
    assert combined_payload(original, offers) is None
    assert "Quick Links" in [field["name"] for field in original["embeds"][0]["fields"]]


def test_product_title_normalisation_is_conservative():
    first = Alert(AlertType.RESTOCK, bag("GeekCore"))
    equivalent = Alert(AlertType.RESTOCK, bag("BoxLunch", name="Loungefly Disney Stitch Floral Mini Backpack"))
    different_colour = Alert(AlertType.RESTOCK, bag("BoxLunch", name="Disney Stitch Blue Mini Backpack"))
    assert group_key(first) == group_key(equivalent)
    assert group_key(first) != group_key(different_colour)

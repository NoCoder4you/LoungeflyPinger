from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import ReleaseAlertConfig
from app.database import Database
from app.models import Alert, AlertType, Availability, Product, ReleaseInfo, ReleasePrecision
from app.notifications.discord import DiscordNotifier, build_discord_payload
from app.release import format_release, parse_release_text
from app.services.monitor_service import MonitorService


class Monitor:
    def __init__(self, products): self.products = products
    async def discover_products(self): return self.products


class Notifier:
    def __init__(self): self.alerts = []
    async def send(self, alert): self.alerts.append(alert); return True


def bag(release=None, availability=Availability.COMING_SOON):
    return Product("GeekCore", "release-1", "Release Bag", "https://example.com/release-1",
                   availability, price=Decimal("80"), currency="GBP", release=release)


@pytest.mark.parametrize(("text", "precision"), [
    ("Releases 18 September 2026", ReleasePrecision.DATE_ONLY),
    ("Available 18/09/2026 at 09:00", ReleasePrecision.EXACT_DATETIME),
    ("Launching September 2026", ReleasePrecision.MONTH_ONLY),
    ("Coming Soon", ReleasePrecision.COMING_SOON),
])
def test_release_precision_parsing(text, precision):
    parsed = parse_release_text(text, source="fixture", local_timezone="Europe/London")
    assert parsed is not None and parsed.precision == precision


def test_timezone_dst_and_no_fake_midnight():
    summer = parse_release_text("Available 18/09/2026 at 09:00", source="x",
                                local_timezone="Europe/London")
    winter = parse_release_text("Available 18/12/2026 at 09:00", source="x",
                                local_timezone="Europe/London")
    date_only = parse_release_text("Releases 18 September 2026", source="x",
                                   local_timezone="Europe/London")
    assert summer.release_datetime.utcoffset() == timedelta(hours=1)
    assert winter.release_datetime.utcoffset() == timedelta(0)
    assert summer.timezone_inferred is True
    assert date_only.release_datetime is None and date_only.release_time is None
    explicit = parse_release_text("Available 18/09/2026 at 09:00 UTC", source="x")
    assert explicit.timezone == "UTC" and explicit.timezone_inferred is False


@pytest.mark.parametrize("text", ["Release eventually", "Available 32/15/2026 at 99:00", "", None])
def test_malformed_release_text_is_not_evidence(text):
    assert parse_release_text(text, source="fixture", local_timezone="Europe/London") is None


@pytest.mark.asyncio
async def test_initial_enrichment_silent_then_time_added_and_changes_alert(tmp_path: Path):
    date_only = parse_release_text("Releases 18 September 2026", source="fixture")
    exact = parse_release_text("Available 18/09/2026 at 09:00", source="fixture",
                               local_timezone="Europe/London")
    changed = parse_release_text("Available 20/09/2026 at 10:00", source="fixture",
                                 local_timezone="Europe/London")
    async with Database(tmp_path / "release.db") as db:
        monitor = Monitor([bag()])
        notifier = Notifier()
        service = MonitorService(monitor, db, notifier, retailer_name="GeekCore")
        await service.synchronize()
        monitor.products = [bag(date_only)]
        alerts = await service.synchronize()
        assert [a.alert_type for a in alerts] == [AlertType.RELEASE_DATE_FOUND]
        monitor.products = [bag(exact)]
        assert [a.alert_type for a in await service.synchronize()] == [AlertType.RELEASE_TIME_FOUND]
        monitor.products = [bag(changed)]
        assert [a.alert_type for a in await service.synchronize()] == [AlertType.RELEASE_DATETIME_CHANGED]


@pytest.mark.asyncio
async def test_existing_upgrade_enrichment_is_silent_and_missing_is_preserved(tmp_path: Path):
    path = tmp_path / "upgrade.db"
    release = parse_release_text("Releases 18 September 2026", source="fixture")
    async with Database(path) as db:
        service = MonitorService(Monitor([bag()]), db, Notifier(), retailer_name="GeekCore")
        await service.synchronize()
        await db.connection.execute("UPDATE retailers SET release_sync_completed=0")
        await db.connection.commit()
    async with Database(path) as db:
        monitor = Monitor([bag(release)])
        service = MonitorService(monitor, db, Notifier(), retailer_name="GeekCore")
        assert await service.synchronize() == []
        monitor.products = [bag(None)]
        assert await service.synchronize() == []
        product_id = (await (await db.connection.execute("SELECT id FROM products")).fetchone())[0]
        assert (await service.releases.current(product_id)).release_date == release.release_date


@pytest.mark.asyncio
async def test_reminders_persist_across_restart_and_date_only_has_none(tmp_path: Path):
    path = tmp_path / "reminders.db"
    instant = datetime.now(UTC) + timedelta(minutes=30)
    exact = ReleaseInfo(ReleasePrecision.EXACT_DATETIME, instant.date(), instant.timetz().replace(tzinfo=None),
                        "UTC", instant, "Available soon", "fixture")
    config = ReleaseAlertConfig(True, (3600,), False)
    async with Database(path) as db:
        notifier = Notifier()
        service = MonitorService(Monitor([bag(exact)]), db, notifier, retailer_name="GeekCore",
                                 release_alerts=config)
        await service.synchronize()
        assert notifier.alerts == []
        await service.synchronize()
        assert [a.alert_type for a in notifier.alerts] == [AlertType.RELEASING_SOON]
    async with Database(path) as db:
        notifier = Notifier()
        service = MonitorService(Monitor([bag(exact)]), db, notifier, retailer_name="GeekCore",
                                 release_alerts=config)
        assert await service.synchronize() == []


def test_discord_release_change_payload_and_dedup_identity():
    old = parse_release_text("Releases 18 September 2026", source="fixture")
    new = parse_release_text("Available 20/09/2026 at 10:00", source="fixture",
                             local_timezone="Europe/London")
    alert = Alert(AlertType.RELEASE_DATETIME_CHANGED, bag(new), 1, previous_release=old,
                  occurrence_id="stable-release-change")
    fields = build_discord_payload(alert)["embeds"][0]["fields"]
    assert {field["name"] for field in fields} >= {"Previous Release", "New Release"}
    assert DiscordNotifier.deduplication_key(alert) == DiscordNotifier.deduplication_key(alert)


@pytest.mark.asyncio
async def test_released_requires_retailer_stock_evidence_and_known_release(tmp_path: Path):
    release = parse_release_text("Coming Soon", source="fixture")
    async with Database(tmp_path / "released.db") as db:
        monitor = Monitor([bag(release)])
        service = MonitorService(monitor, db, Notifier(), retailer_name="GeekCore")
        await service.synchronize()
        # Merely passing a published date is never evaluated as released; actual stock is.
        monitor.products = [bag(release, Availability.IN_STOCK)]
        assert [a.alert_type for a in await service.synchronize()] == [AlertType.RELEASED]

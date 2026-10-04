"""Orchestration shared by retailer adapters."""

import logging
import asyncio
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.config import PriceAlertConfig, ReleaseAlertConfig
from app.database import Database
from app.http import HttpClientError
from app.models import Alert, AlertType, Availability, Product, RetailerHealth
from app.monitors.base import RetailerMonitor
from app.monitors.base import IncompleteScanError
from app.notifications.base import NotificationProvider
from app.services.product_service import ProductService
from app.services.release_service import ReleaseService
from app.services.stock_service import StockService
from app.services.product_event_service import ProductEventService
from app.watchlist import Watchlist, classify_product
from typing import Protocol

LOGGER = logging.getLogger("monitor.retailers")


class WatchlistProvider(Protocol):
    @property
    def current(self) -> Watchlist: ...


class MonitorService:
    """Persist one discovery pass and emit only meaningful post-baseline changes."""

    def __init__(
        self,
        monitor: RetailerMonitor,
        database: Database,
        notifier: NotificationProvider,
        *,
        retailer_name: str,
        watchlist: Watchlist | WatchlistProvider | None = None,
        price_alerts: PriceAlertConfig | None = None,
        missing_scan_threshold: int = 3,
        release_alerts: ReleaseAlertConfig | None = None,
        failure_alert_threshold: int = 5,
        circuit_failure_threshold: int = 5,
        circuit_cooldown_seconds: float = 900,
    ) -> None:
        self.monitor = monitor
        self.database = database
        self.notifier = notifier
        self.retailer_name = retailer_name
        self.products = ProductService(database)
        self.stock = StockService(database)
        self.events = ProductEventService(database)
        # None keeps backwards compatibility for programmatic users; an explicitly
        # empty configured watchlist intentionally sends no product alerts.
        self.watchlist = watchlist
        self.price_alerts = price_alerts or PriceAlertConfig()
        self.release_alerts = release_alerts or ReleaseAlertConfig()
        self.releases = ReleaseService(database)
        if missing_scan_threshold < 1:
            raise ValueError("missing_scan_threshold must be at least one")
        self.missing_scan_threshold = missing_scan_threshold
        if failure_alert_threshold < 1:
            raise ValueError("failure_alert_threshold must be at least one")
        self.failure_alert_threshold = failure_alert_threshold
        if circuit_failure_threshold < 1 or circuit_cooldown_seconds <= 0:
            raise ValueError("circuit settings must be positive")
        self.circuit_failure_threshold = circuit_failure_threshold
        self.circuit_cooldown_seconds = circuit_cooldown_seconds

    @staticmethod
    def _stock_alert(previous: Availability, product: Product) -> AlertType | None:
        current = product.availability
        if previous == current:
            return None
        if previous in {Availability.OUT_OF_STOCK, Availability.BACKORDER} and current == Availability.IN_STOCK:
            return AlertType.RESTOCK
        if current == Availability.LOW_STOCK and previous != Availability.LOW_STOCK:
            return AlertType.LOW_STOCK
        if previous == Availability.COMING_SOON and current == Availability.IN_STOCK:
            return AlertType.AVAILABILITY
        if previous == Availability.PREORDER and current == Availability.IN_STOCK:
            return AlertType.AVAILABILITY
        # ``preorder`` describes the product/listing, even when ordering is
        # currently unavailable.  Only the normalized PREORDER availability is
        # evidence that customers can actually place an order now.
        if (previous in {Availability.COMING_SOON, Availability.OUT_OF_STOCK}
                and current == Availability.PREORDER):
            return AlertType.PREORDER_OPEN
        if current in {Availability.OUT_OF_STOCK, Availability.UNAVAILABLE}:
            return AlertType.OUT_OF_STOCK
        if current == Availability.BACKORDER:
            return AlertType.BACKORDER
        if current == Availability.COMING_SOON:
            return AlertType.COMING_SOON
        if current == Availability.PREORDER:
            return AlertType.PREORDER_OPEN
        return AlertType.STATUS_CHANGE

    def _is_price_drop(self, previous: Decimal | None, product: Product, currency: str | None) -> bool:
        if not self.price_alerts.enabled or previous is None or product.price is None:
            return False
        if currency != product.currency or previous <= product.price or previous == 0:
            return False
        drop = previous - product.price
        percent = drop * Decimal("100") / previous
        return (drop >= Decimal(str(self.price_alerts.minimum_drop_value)) and
                percent >= Decimal(str(self.price_alerts.minimum_drop_percent)))

    def _is_price_increase(self, previous: Decimal | None, product: Product,
                           currency: str | None) -> bool:
        return bool(self.price_alerts.enabled and previous is not None and product.price is not None
                    and currency == product.currency and product.price > previous)

    def _matches(self, product: Product):
        watchlist = self.watchlist.current if hasattr(self.watchlist, "current") else self.watchlist
        return watchlist.match(product) if watchlist is not None else ()

    @staticmethod
    def _release_key(info):
        if info is None:
            return None
        return (info.precision, info.release_date, info.release_time, info.timezone,
                info.release_datetime, info.timezone_inferred, info.release_month, info.release_year)

    @classmethod
    def _release_alert(cls, old, new) -> AlertType | None:
        if new is None or cls._release_key(old) == cls._release_key(new):
            return None
        if old is None:
            return AlertType.RELEASE_DATE_FOUND if new.release_date else None
        date_changed = old.release_date != new.release_date or (
            old.release_date is None and new.release_date is None and old.precision != new.precision
        )
        time_changed = old.release_time != new.release_time or old.timezone != new.timezone
        if date_changed and time_changed:
            return AlertType.RELEASE_DATETIME_CHANGED
        if date_changed:
            return AlertType.RELEASE_DATE_CHANGED
        if old.release_time is None and new.release_time is not None:
            return AlertType.RELEASE_TIME_FOUND
        if time_changed:
            return AlertType.RELEASE_TIME_CHANGED
        return None

    async def _send(self, alert: Alert, alerts: list[Alert]) -> bool:
        """Attempt an eligible alert and report whether its destination accepted it."""
        if self.watchlist is not None and not alert.watch_matches:
            return False
        alerts.append(alert)
        return await self.notifier.send(alert)

    async def synchronize(self) -> list[Alert]:
        """Run one isolated scan, atomically persist it, and maintain durable health."""
        if not await self._claim_circuit_probe():
            LOGGER.info("scan_skipped_circuit_open", extra={"retailer": self.retailer_name})
            return []
        started = time.monotonic()
        reset_metrics = getattr(getattr(self.monitor, "http", None), "reset_metrics", None)
        if reset_metrics:
            reset_metrics()
        LOGGER.info("scan_started", extra={"retailer": self.retailer_name})
        try:
            # Slow retailer I/O happens before the short SQLite critical section,
            # allowing every scheduled retailer to make progress independently.
            discovered = await self.monitor.discover_products()
            if (not discovered and getattr(self.monitor, "reject_unexpected_empty", False)
                    and await self._has_existing_products()):
                raise IncompleteScanError(
                    "Unexpected empty catalogue; preserving previously observed products"
                )
            async with self.database.write_lock:
                connection = self.database.connection
                if connection is None:
                    raise RuntimeError("database is not connected")
                await connection.execute("BEGIN IMMEDIATE")
                try:
                    alerts = await self._synchronize_success(started, discovered)
                except BaseException:
                    await connection.rollback()
                    raise
            return alerts
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self._record_failure(exc, time.monotonic() - started)

    async def _has_existing_products(self) -> bool:
        connection = self.database.connection
        if connection is None:
            return False
        row = await (await connection.execute(
            "SELECT 1 FROM products WHERE retailer=? LIMIT 1", (self.retailer_name,)
        )).fetchone()
        return row is not None

    async def _claim_circuit_probe(self) -> bool:
        """Skip an open circuit, atomically claiming its single half-open probe."""
        connection = self.database.connection
        if connection is None:
            return True
        async with self.database.write_lock:
            row = await (await connection.execute(
                "SELECT circuit_state, circuit_open_until FROM retailers WHERE name=?",
                (self.retailer_name,),
            )).fetchone()
            if not row or row[0] == "CLOSED":
                return True
            now = datetime.now(UTC)
            eligible = datetime.fromisoformat(row[1]) if row[1] else now
            if eligible > now or row[0] == "HALF_OPEN":
                return False
            result = await connection.execute(
                "UPDATE retailers SET circuit_state='HALF_OPEN' WHERE name=? AND circuit_state='OPEN'",
                (self.retailer_name,),
            )
            await connection.commit()
            return result.rowcount == 1

    async def _record_failure(self, exc: Exception, duration: float) -> list[Alert]:
        connection = self.database.connection
        if connection is None:
            LOGGER.exception("scan_failed_without_database", extra={"retailer": self.retailer_name})
            return []
        now = datetime.now(UTC).isoformat()
        status = exc.status if isinstance(exc, HttpClientError) else getattr(
            getattr(self.monitor, "http", None), "response_status", None
        )
        http_error = exc if isinstance(exc, HttpClientError) else getattr(
            getattr(self.monitor, "http", None), "last_error", None
        )
        error = (http_error.diagnostic() if isinstance(http_error, HttpClientError)
                 else str(exc).strip() or type(exc).__name__)
        async with self.database.write_lock:
            await connection.execute(
                "INSERT INTO retailers(name) VALUES (?) ON CONFLICT(name) DO NOTHING",
                (self.retailer_name,),
            )
            row = await (await connection.execute(
                "SELECT consecutive_failures, failure_alert_sent, incident_started_at, "
                "circuit_open_count, circuit_state FROM retailers WHERE name=?",
                (self.retailer_name,),
            )).fetchone()
            failures = int(row[0]) + 1
            alert_sent = bool(row[1])
            health = (RetailerHealth.FAILED if failures >= self.failure_alert_threshold
                      else RetailerHealth.DEGRADED)
            blocked = isinstance(http_error, HttpClientError) and http_error.kind.value == "forbidden"
            if blocked:
                health = RetailerHealth.BLOCKED_BY_RETAILER
            should_open = blocked or failures >= self.circuit_failure_threshold or row[4] == "HALF_OPEN"
            open_count = int(row[3]) + (1 if should_open else 0)
            cooldown = min(self.circuit_cooldown_seconds * 2 ** max(0, open_count - 1), 86400)
            open_until = ((datetime.now(UTC) + timedelta(seconds=cooldown)).isoformat()
                          if should_open else None)
            await connection.execute(
                """UPDATE retailers SET last_failure=?, consecutive_failures=?, last_error=?,
                          response_status=?, request_duration=?, health=?, error_category=?,
                          error_hostname=?, error_method=?, retry_attempts=?, retry_after_supplied=?,
                          incident_started_at=COALESCE(incident_started_at, ?), circuit_state=?,
                          circuit_open_until=?, circuit_open_count=? WHERE name=?""",
                (now, failures, error[:2000], status, duration, health.value,
                 http_error.kind.value if isinstance(http_error, HttpClientError) else "adapter_failure",
                 http_error.hostname if isinstance(http_error, HttpClientError) else None,
                 http_error.method if isinstance(http_error, HttpClientError) else None,
                 http_error.attempts if isinstance(http_error, HttpClientError) else None,
                 int(http_error.retry_after_supplied) if isinstance(http_error, HttpClientError) else 0,
                 now, "OPEN" if should_open else "CLOSED", open_until, open_count,
                 self.retailer_name),
            )
            await connection.commit()
        LOGGER.error("scan_failed", extra={"retailer": self.retailer_name, "failures": failures,
                                           "status": status, "error": error})
        if health not in {RetailerHealth.FAILED, RetailerHealth.BLOCKED_BY_RETAILER} or alert_sent:
            return []
        last_success = await (await connection.execute(
            "SELECT last_success FROM retailers WHERE name=?", (self.retailer_name,)
        )).fetchone()
        display_success = "Never" if not last_success or not last_success[0] else last_success[0]
        alert = Alert(
            AlertType.MONITOR_ERROR,
            message=(f"Retailer: {self.retailer_name}\nFailures: {failures}\n"
                     f"Last Successful Scan: {display_success}\nError: {error}"),
            new_state=health.value,
            occurrence_id=f"retailer-failure:{self.retailer_name}:{now}",
        )
        # This flag represents issuance, rather than delivery. A Discord outage must
        # not turn every scan into an alert storm; the persistent health row remains
        # available to operators until a recovery occurs.
        await self.notifier.send(alert)
        await connection.execute(
            "UPDATE retailers SET failure_alert_sent=1 WHERE name=?", (self.retailer_name,)
        )
        await connection.commit()
        return [alert]

    async def record_failure(self, exc: Exception, duration: float) -> list[Alert]:
        """Record a coordinator-level failure through the canonical health path.

        Scan coordinators use this when a deadline cancels discovery before
        ``synchronize`` can handle an ordinary adapter exception itself.
        """
        return await self._record_failure(exc, duration)

    async def _synchronize_success(self, started: float, discovered: list[Product]) -> list[Alert]:
        connection = self.database.connection
        if connection is None:
            raise RuntimeError("database is not connected")
        retailer = discovered[0].retailer if discovered else self.retailer_name
        prior_sync = await (
            await connection.execute(
                "SELECT last_success, release_sync_completed FROM retailers WHERE name = ?", (retailer,)
            )
        ).fetchone()
        release_baselined = bool(prior_sync and prior_sync[1])
        alerts: list[Alert] = []
        seen_ids: set[int] = set()
        for product in discovered:
            product = classify_product(product)
            known_product = await (await connection.execute(
                "SELECT id, removed_at, first_seen FROM products WHERE retailer=? AND retailer_product_id=?",
                (product.retailer, product.retailer_product_id),
            )).fetchone()
            was_removed = known_product is not None and known_product[1] is not None
            product_id = await self.products.upsert(product)
            if product.availability == Availability.ERROR and was_removed:
                # Upsert refreshes metadata, but a failed check is not evidence of reappearance.
                await connection.execute(
                    "UPDATE products SET removed_at=? WHERE id=?", (known_product[1], product_id)
                )
            seen_ids.add(product_id)
            prior_state = await self.stock.current(product_id)
            prior_release = await self.releases.current(product_id)
            previous = prior_state.availability if prior_state else None
            alert_types: list[AlertType] = []
            if product.availability != Availability.ERROR:
                if prior_sync is not None and prior_sync[0] is not None and known_product is None:
                    alert_types.append(AlertType.NEW_PRODUCT)
                elif previous is not None:
                    if (self.release_alerts.enabled and prior_release is not None and
                            previous in {Availability.COMING_SOON, Availability.PREORDER} and
                            product.availability in {
                                Availability.IN_STOCK, Availability.LOW_STOCK,
                            }):
                        alert_types.append(AlertType.RELEASED)
                    elif was_removed:
                        alert_types.append(AlertType.AVAILABILITY)
                    else:
                        stock_alert = self._stock_alert(previous, product)
                        if stock_alert is not None:
                            alert_types.append(stock_alert)
                    if self._is_price_drop(prior_state.price, product, prior_state.currency):
                        alert_types.append(AlertType.PRICE_DROP)
                    elif self._is_price_increase(prior_state.price, product, prior_state.currency):
                        alert_types.append(AlertType.PRICE_INCREASE)
                    old_eta = prior_state.estimated_arrival_text or (
                        prior_state.estimated_arrival_date.isoformat()
                        if prior_state.estimated_arrival_date else None
                    )
                    new_eta = product.estimated_arrival_text or (
                        product.estimated_arrival_date.isoformat()
                        if product.estimated_arrival_date else None
                    )
                    if old_eta is not None and new_eta is not None and old_eta != new_eta:
                        alert_types.append(AlertType.ETA_CHANGED)
            # A failed parse/check provides no inventory evidence. Preserve the
            # last known stock state until a successful observation replaces it.
            if product.availability != Availability.ERROR:
                await self.stock.record(product_id, product)
                release_alert = self._release_alert(prior_release, product.release)
                if product.release is not None:
                    await self.releases.record(product_id, product.release)
                # New products carry release details in NEW_PRODUCT. Existing rows with no
                # release state are silent by default on the first release-aware scan.
                is_new = known_product is None
                may_notify_found = (release_baselined or self.release_alerts.notify_existing_on_upgrade)
                if (self.release_alerts.enabled and release_alert is not None and
                        not is_new and (prior_release is not None or may_notify_found)):
                    alert_types.append(release_alert)
                if known_product is None:
                    await self.events.record(
                        product_id, AlertType.NEW_PRODUCT, new_summary=product.availability.value,
                        price=product.price, currency=product.currency,
                        availability=product.availability,
                    )
            for alert_type in alert_types:
                if not (alert_type == AlertType.NEW_PRODUCT and known_product is None):
                    await self.events.record(
                        product_id, alert_type,
                        previous_summary=previous.value if previous else None,
                        new_summary=product.availability.value,
                        price=product.price, currency=product.currency,
                        availability=product.availability,
                    )
                alert = Alert(
                    alert_type, product, product_id,
                    previous_price=(prior_state.price if alert_type in {
                        AlertType.PRICE_DROP, AlertType.PRICE_INCREASE
                    } else None),
                    previous_availability=previous, watch_matches=self._matches(product),
                    previous_release=prior_release,
                    first_seen=(datetime.fromisoformat(known_product[2]) if known_product else datetime.now(UTC)),
                )
                await self._send(alert, alerts)
            if (release_baselined and product.availability != Availability.ERROR and
                    self.release_alerts.enabled and
                    product.release is not None and product.release.release_datetime is not None):
                instant = product.release.release_datetime
                remaining = (instant - datetime.now(UTC)).total_seconds()
                for seconds in self.release_alerts.reminders_seconds:
                    if 0 < remaining <= seconds and not await self.releases.reminder_sent(
                        product_id, instant, seconds
                    ):
                        reminder = Alert(
                            AlertType.RELEASING_SOON, product, product_id,
                            occurrence_id=f"release-reminder:{product_id}:{instant.isoformat()}:{seconds}",
                            watch_matches=self._matches(product), reminder_seconds=seconds,
                        )
                        if await self._send(reminder, alerts):
                            await self.releases.mark_reminder(product_id, instant, seconds)
            # Keep a tombstone through an unsuccessful check so the next usable
            # observation still produces the reappearance notification.
            if product.availability != Availability.ERROR:
                await self.products.mark_seen(product_id)
        # Absence is only evidence after repeated successful, complete listing scans.
        missing_rows = await (await connection.execute(
            """SELECT p.id, p.retailer, p.retailer_product_id, p.name, p.url, p.image_url, p.sku,
                      p.product_type, p.franchise, p.character, p.exclusive, p.exclusive_retailer,
                      p.new_release, p.missing_scans,
                      s.availability, s.price, s.currency, s.preorder
                 FROM products p LEFT JOIN product_states s ON s.product_id=p.id
                WHERE p.retailer=? AND p.removed_at IS NULL""", (retailer,)
        )).fetchall()
        now = datetime.now(UTC).isoformat()
        for row in missing_rows:
            if row[0] in seen_ids:
                continue
            count = row[13] + 1
            removed = count >= self.missing_scan_threshold
            await connection.execute(
                "UPDATE products SET missing_scans=?, removed_at=? WHERE id=?",
                (count, now if removed else None, row[0]),
            )
            if removed and row[14] is not None:
                missing_product = Product(
                    retailer=row[1], retailer_product_id=row[2], name=row[3], url=row[4],
                    availability=Availability(row[14]), image_url=row[5],
                    price=Decimal(row[15]) if row[15] is not None else None,
                    currency=row[16], product_type=row[7], franchise=row[8], character=row[9],
                    exclusive=bool(row[10]), exclusive_retailer=row[11],
                    new_release=bool(row[12]), preorder=bool(row[17]), sku=row[6],
                )
                await self.events.record(
                    row[0], AlertType.PRODUCT_REMOVED,
                    previous_summary=row[14], new_summary="REMOVED",
                    price=missing_product.price, currency=missing_product.currency,
                    availability=missing_product.availability,
                )
                await self._send(Alert(
                    AlertType.PRODUCT_REMOVED, missing_product, row[0],
                    previous_availability=Availability(row[14]),
                    watch_matches=self._matches(missing_product),
                ), alerts)
        previous_health = await (await connection.execute(
            "SELECT health FROM retailers WHERE name=?", (retailer,)
        )).fetchone()
        duration = time.monotonic() - started
        status = getattr(getattr(self.monitor, "http", None), "response_status", None)
        await connection.execute(
            """INSERT INTO retailers(name, last_success, consecutive_failures, release_sync_completed)
               VALUES (?, ?, 0, 1)
               ON CONFLICT(name) DO UPDATE SET last_success=excluded.last_success,
                 consecutive_failures=0, release_sync_completed=1, last_error=NULL,
                 response_status=?, request_duration=?, health='HEALTHY', failure_alert_sent=0,
                 error_category=NULL, error_hostname=NULL, error_method=NULL, retry_attempts=NULL,
                 retry_after_supplied=0, incident_started_at=NULL, circuit_state='CLOSED',
                 circuit_open_until=NULL, circuit_open_count=0""",
            (retailer, now, status, duration),
        )
        await connection.commit()
        if previous_health and previous_health[0] in {
            RetailerHealth.FAILED.value, RetailerHealth.BLOCKED_BY_RETAILER.value,
        }:
            recovery = Alert(
                AlertType.MONITOR_RECOVERED,
                message=f"Retailer: {retailer}\nThe retailer monitor is responding normally again.",
                new_state=RetailerHealth.HEALTHY.value,
                occurrence_id=f"retailer-recovered:{retailer}:{now}",
            )
            alerts.append(recovery)
            await self.notifier.send(recovery)
        LOGGER.info("scan_completed", extra={"retailer": retailer, "products": len(discovered),
                                               "changes": len(alerts), "duration": duration})
        return alerts

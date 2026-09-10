"""Normalized domain models shared by monitors and services."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from urllib.parse import urlparse
from uuid import uuid4


class Availability(StrEnum):
    UNKNOWN = "UNKNOWN"
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    PREORDER = "PREORDER"
    COMING_SOON = "COMING_SOON"
    BACKORDER = "BACKORDER"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


class AlertType(StrEnum):
    NEW_PRODUCT = "NEW_PRODUCT"
    RESTOCK = "RESTOCK"
    PREORDER_OPEN = "PREORDER_OPEN"
    PRICE_DROP = "PRICE_DROP"
    LOW_STOCK = "LOW_STOCK"
    PRODUCT_REMOVED = "PRODUCT_REMOVED"
    AVAILABILITY = "AVAILABILITY"
    MONITOR_ERROR = "MONITOR_ERROR"
    MONITOR_RECOVERED = "MONITOR_RECOVERED"


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class WatchMatch:
    """Small, provider-independent description of a matched watch rule."""

    name: str
    priority: Priority = Priority.NORMAL

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("watch match name must be a non-empty string")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "priority", Priority(self.priority))


@dataclass(frozen=True, slots=True)
class Product:
    retailer: str
    retailer_product_id: str
    name: str
    url: str
    availability: Availability = Availability.UNKNOWN
    image_url: str | None = None
    price: Decimal | None = None
    currency: str = "USD"
    product_type: str = "Mini Backpack"
    franchise: str | None = None
    character: str | None = None
    exclusive: bool = False
    preorder: bool = False
    sku: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("retailer", "retailer_product_id", "name"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
            object.__setattr__(self, field_name, value.strip())
        self._validate_url("url", self.url, required=True)
        self._validate_url("image_url", self.image_url, required=False)
        try:
            availability = Availability(self.availability)
        except ValueError as exc:
            raise ValueError("availability is not recognized") from exc
        object.__setattr__(self, "availability", availability)
        if self.price is not None:
            try:
                price = Decimal(str(self.price))
            except InvalidOperation as exc:
                raise ValueError("price must be a decimal number") from exc
            if not price.is_finite() or price < 0:
                raise ValueError("price must be a finite, non-negative value")
            object.__setattr__(self, "price", price)
        currency = self.currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("currency must be a three-letter ISO-style code")
        object.__setattr__(self, "currency", currency)
        if self.sku is not None:
            if not isinstance(self.sku, str) or not self.sku.strip():
                raise ValueError("sku must be a non-empty string when provided")
            object.__setattr__(self, "sku", self.sku.strip())

    @staticmethod
    def _validate_url(field: str, value: str | None, *, required: bool) -> None:
        if value is None and not required:
            return
        if not isinstance(value, str) or urlparse(value).scheme not in {"http", "https"} or not urlparse(value).netloc:
            raise ValueError(f"{field} must be a valid HTTP(S) URL")


@dataclass(frozen=True, slots=True)
class Alert:
    """A provider-independent notification event."""

    alert_type: AlertType
    product: Product | None = None
    product_id: int | None = None
    previous_price: Decimal | None = None
    previous_availability: Availability | None = None
    new_state: str | None = None
    message: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    occurrence_id: str = field(default_factory=lambda: uuid4().hex)
    watch_matches: tuple[WatchMatch, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "alert_type", AlertType(self.alert_type))
        if self.previous_price is not None:
            price = Decimal(str(self.previous_price))
            if not price.is_finite() or price < 0:
                raise ValueError("previous_price must be a finite, non-negative value")
            object.__setattr__(self, "previous_price", price)
        if self.previous_availability is not None:
            object.__setattr__(self, "previous_availability", Availability(self.previous_availability))
        if not isinstance(self.occurrence_id, str) or not self.occurrence_id.strip():
            raise ValueError("occurrence_id must be a non-empty string")
        object.__setattr__(self, "occurrence_id", self.occurrence_id.strip())
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        object.__setattr__(self, "watch_matches", tuple(self.watch_matches))

    @property
    def priority(self) -> Priority:
        """Highest matched priority, suitable for future channel routing (for example SMS)."""
        rank = {Priority.LOW: 0, Priority.NORMAL: 1, Priority.HIGH: 2}
        return max((match.priority for match in self.watch_matches), key=rank.get, default=Priority.NORMAL)

    @property
    def is_admin(self) -> bool:
        return self.alert_type in {AlertType.MONITOR_ERROR, AlertType.MONITOR_RECOVERED}

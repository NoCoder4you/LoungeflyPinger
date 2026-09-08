"""Normalized domain models shared by monitors and services."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from urllib.parse import urlparse


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
    MONITOR_ERROR = "MONITOR_ERROR"
    MONITOR_RECOVERED = "MONITOR_RECOVERED"


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

    @staticmethod
    def _validate_url(field: str, value: str | None, *, required: bool) -> None:
        if value is None and not required:
            return
        if not isinstance(value, str) or urlparse(value).scheme not in {"http", "https"} or not urlparse(value).netloc:
            raise ValueError(f"{field} must be a valid HTTP(S) URL")

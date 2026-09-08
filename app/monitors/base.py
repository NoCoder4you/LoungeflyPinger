"""Contract implemented by every future retailer adapter."""

from abc import ABC, abstractmethod

from app.models import Product


class RetailerMonitor(ABC):
    @abstractmethod
    async def discover_products(self) -> list[Product]:
        """Return normalized products found at the retailer."""

    @abstractmethod
    async def check_product(self, product: Product) -> Product:
        """Return the product with its current normalized state."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Return whether the retailer endpoint and adapter are healthy."""

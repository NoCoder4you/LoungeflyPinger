from abc import ABC, abstractmethod

from app.models import AlertType, Product


class NotificationDestination(ABC):
    @abstractmethod
    async def send(self, alert_type: AlertType, product: Product, message: str) -> bool:
        """Deliver an alert and return whether delivery succeeded."""

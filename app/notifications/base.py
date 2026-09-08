from abc import ABC, abstractmethod

from app.models import Alert


class NotificationProvider(ABC):
    @abstractmethod
    async def send(self, alert: Alert) -> bool:
        """Deliver an alert and return whether delivery succeeded."""


# Backwards-compatible name for code built on the initial contract.
NotificationDestination = NotificationProvider

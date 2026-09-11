"""Retailer monitor adapters."""

from .base import RetailerMonitor
from .boxlunch import BoxLunchMonitor, HotTopicUSMonitor
from .books_a_million import BooksAMillionMonitor
from .disney_store_uk import DisneyStoreUKMonitor, DisneyStoreUSMonitor
from .geekcore import GeekCoreMonitor
from .loungefly_uk import LoungeflyCanadaMonitor, LoungeflyUKMonitor, LoungeflyUSMonitor
from .truffleshuffle import TruffleShuffleMonitor

__all__ = [
    "BooksAMillionMonitor", "BoxLunchMonitor", "DisneyStoreUKMonitor", "DisneyStoreUSMonitor", "GeekCoreMonitor",
    "HotTopicUSMonitor", "LoungeflyCanadaMonitor", "LoungeflyUKMonitor",
    "LoungeflyUSMonitor", "RetailerMonitor", "TruffleShuffleMonitor",
]

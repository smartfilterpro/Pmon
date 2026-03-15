"""Base notifier interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pmon.models import StockResult, CheckoutResult


class BaseNotifier(ABC):
    @abstractmethod
    async def notify_in_stock(self, result: StockResult):
        ...

    @abstractmethod
    async def notify_checkout(self, result: CheckoutResult):
        ...

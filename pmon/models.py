"""Shared data models for stock status tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class StockStatus(Enum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"
    ERROR = "error"


class CheckoutStatus(Enum):
    IDLE = "idle"
    ATTEMPTING = "attempting"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class StockResult:
    url: str
    retailer: str
    product_name: str
    status: StockStatus
    price: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    error_message: str = ""


@dataclass
class CheckoutResult:
    url: str
    retailer: str
    product_name: str
    status: CheckoutStatus
    order_number: str = ""
    error_message: str = ""
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class MonitorState:
    """Tracks the state of all monitored products."""
    products: dict[str, StockResult] = field(default_factory=dict)
    checkout_attempts: list[CheckoutResult] = field(default_factory=list)
    is_running: bool = False
    started_at: datetime | None = None

    def update_stock(self, result: StockResult):
        self.products[result.url] = result

    def add_checkout(self, result: CheckoutResult):
        self.checkout_attempts.append(result)
        # Keep last 100 attempts
        if len(self.checkout_attempts) > 100:
            self.checkout_attempts = self.checkout_attempts[-100:]

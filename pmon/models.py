"""Shared data models for stock status tracking.

AUDIT FINDINGS (2026-03-17):
=============================================================================
Models are clean and well-structured. Gaps for the rewrite:

1. CheckoutResult NEEDS screenshot_path FIELD: For debugging failed checkout
   attempts, each result should optionally reference a screenshot file.

2. CheckoutStatus NEEDS MORE STATES: Currently only IDLE/ATTEMPTING/SUCCESS/
   FAILED. The rewrite may need PRICE_EXCEEDED, CAPTCHA_BLOCKED, or
   SESSION_EXPIRED states for better error reporting.

3. NO PopupEvent MODEL: The new PopupHandler will need a model to log
   detected popups (type, action taken, screenshot, timestamp).
=============================================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _utcnow() -> datetime:
    """Return timezone-aware UTC now.

    Using timezone-aware datetimes ensures .isoformat() includes '+00:00',
    which lets the browser correctly convert to the user's local timezone.
    """
    return datetime.now(timezone.utc)


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
    image_url: str = ""
    timestamp: datetime = field(default_factory=_utcnow)
    error_message: str = ""
    stock_quantity: int | None = None


@dataclass
class CheckoutResult:
    url: str
    retailer: str
    product_name: str
    status: CheckoutStatus
    order_number: str = ""
    error_message: str = ""
    timestamp: datetime = field(default_factory=_utcnow)


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

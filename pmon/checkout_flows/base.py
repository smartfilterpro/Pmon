"""Base checkout handler with step-based architecture.

CREATED [Mission 3] — Abstract base for per-retailer checkout flows.
Each step returns a StepResult so the runner can log, retry, and detect queues.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger(__name__)


class CheckoutStatus(Enum):
    """Extended checkout statuses for accurate terminal state reporting."""
    PLACED = "placed"
    UNKNOWN = "unknown"
    FAILED = "failed"
    CANCELLED = "cancelled"
    QUEUE_TIMEOUT = "queue_timeout"
    OUT_OF_STOCK = "out_of_stock"
    PRICE_EXCEEDED = "price_exceeded"
    AUTH_FAILED = "auth_failed"


@dataclass
class StepResult:
    """Outcome of a single checkout step."""
    success: bool
    step_name: str
    message: str = ""
    screenshot_b64: str | None = None


@dataclass
class CheckoutFlowResult:
    """Full checkout attempt result with step-level detail."""
    url: str
    retailer: str
    product_name: str
    status: CheckoutStatus
    order_number: str = ""
    error_message: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    steps_completed: list[str] = field(default_factory=list)
    total_price: float = 0.0


class BaseCheckoutHandler:
    """Base class for per-retailer checkout flows.

    Subclasses MUST override all step methods and set ``retailer_name``.
    """
    retailer_name: str = "unknown"

    def __init__(self, vision_helper=None, max_price: float = 0):
        self._vision = vision_helper
        self._max_price = max_price

    async def navigate_to_cart(self, page) -> StepResult:
        """Add item to cart or use Buy Now, then navigate to cart page."""
        return StepResult(success=False, step_name="navigate_to_cart", message="not implemented")

    async def verify_cart_contents(self, page) -> StepResult:
        """Verify the cart is not empty and contains the expected item."""
        return StepResult(success=False, step_name="verify_cart_contents", message="not implemented")

    async def proceed_to_checkout(self, page) -> StepResult:
        """Select delivery method, wait for checkout button, click it."""
        return StepResult(success=False, step_name="proceed_to_checkout", message="not implemented")

    async def fill_shipping(self, page) -> StepResult:
        """Fill shipping address if required (some retailers pre-fill)."""
        return StepResult(success=True, step_name="fill_shipping", message="skipped")

    async def fill_payment(self, page, creds) -> StepResult:
        """Fill payment info (e.g. CVV for saved cards, full card for PKC)."""
        return StepResult(success=False, step_name="fill_payment", message="not implemented")

    async def review_order(self, page) -> StepResult:
        """Review order and check price guard before proceeding.

        If ``self._max_price > 0`` and the displayed total exceeds it,
        returns a failed StepResult with message ``"price_exceeded"``.
        """
        total = await self._extract_order_total(page)
        if self._max_price > 0 and total > self._max_price:
            logger.warning(
                "%s: price guard triggered — total $%.2f exceeds max $%.2f",
                self.retailer_name, total, self._max_price,
            )
            return StepResult(
                success=False,
                step_name="review_order",
                message="price_exceeded",
                screenshot_b64=await self._screenshot(page),
            )
        return StepResult(success=True, step_name="review_order", message=f"total=${total:.2f}")

    async def place_order(self, page) -> StepResult:
        """Click the final 'Place order' button."""
        return StepResult(success=False, step_name="place_order", message="not implemented")

    async def confirm_order_placed(self, page) -> StepResult:
        """CRITICAL: Wait for order confirmation signals.

        After clicking 'Place order', wait for ONE of:
        1. URL contains confirmation path
        2. Page contains order number text
        3. Network response with order confirmation

        Wait up to 30 seconds.
        If ANY signal: return StepResult(success=True, message=order_number)
        If NO signal: return StepResult(success=True, step_name="confirm", message="unknown")
        NEVER return success=False from this method -- UNKNOWN is not FAILED.
        """
        return StepResult(success=True, step_name="confirm_order_placed", message="unknown")

    # -- Shared helpers -------------------------------------------------------

    async def _extract_order_total(self, page) -> float:
        """Extract the displayed order total from the page.

        Looks for common total patterns: ``$123.45``, ``Total: $99.00``, etc.
        Returns 0.0 if no total can be found.
        """
        try:
            body_text = await page.locator("body").inner_text(timeout=3000)
            # Look for "Total" followed by a dollar amount
            match = re.search(r"(?:order\s*)?total[:\s]*\$?([\d,]+\.?\d*)", body_text, re.IGNORECASE)
            if match:
                return float(match.group(1).replace(",", ""))
            # Fallback: any dollar amount near "total"
            match = re.search(r"\$([\d,]+\.\d{2})", body_text)
            if match:
                return float(match.group(1).replace(",", ""))
        except Exception:
            pass
        return 0.0

    async def _screenshot(self, page) -> str | None:
        """Take a base64 screenshot."""
        try:
            raw = await page.screenshot(type="png")
            return base64.b64encode(raw).decode()
        except Exception:
            return None

    async def _wait_for_confirmation_signals(
        self, page, url_patterns: list[str], text_patterns: list[str],
        timeout: int = 30000,
    ) -> tuple[bool, str]:
        """Wait for order confirmation URL or text.

        Returns (confirmed, order_number_or_empty_string).
        """
        import asyncio

        poll_interval = 2000  # ms
        elapsed = 0

        while elapsed < timeout:
            # Check URL patterns
            current_url = page.url.lower()
            for pattern in url_patterns:
                if pattern in current_url:
                    order_num = await self._extract_order_number(page)
                    return True, order_num

            # Check text patterns
            try:
                body_text = await page.locator("body").inner_text(timeout=2000)
                lower_text = body_text.lower()
                for pattern in text_patterns:
                    if pattern.lower() in lower_text:
                        order_num = await self._extract_order_number(page)
                        return True, order_num
            except Exception:
                pass

            await asyncio.sleep(poll_interval / 1000)
            elapsed += poll_interval

        return False, ""

    async def _extract_order_number(self, page) -> str:
        """Extract order number from the current page."""
        try:
            body_text = await page.locator("body").inner_text(timeout=3000)
            match = re.search(r"[Oo]rder\s*#?\s*([A-Z0-9\-]{5,})", body_text)
            if match:
                return match.group(1)
        except Exception:
            pass
        return ""

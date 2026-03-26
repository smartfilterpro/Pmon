"""Walmart.com checkout flow handler.

CREATED [Mission 3] — Extracted from _checkout_walmart() in checkout/engine.py.
Uses selectors from pmon/selectors/walmart.py and human behavior helpers.
"""

from __future__ import annotations

import logging

from pmon.checkout_flows.base import BaseCheckoutHandler, StepResult
from pmon.checkout.human_behavior import (
    human_click_element,
    idle_scroll,
    random_delay,
    random_mouse_jitter,
    sweep_popups,
    wait_for_page_ready,
)
from pmon.selectors.walmart import WALMART_SELECTORS

logger = logging.getLogger(__name__)

_PDP = WALMART_SELECTORS["pdp"]
_CART = WALMART_SELECTORS["cart"]
_CKO = WALMART_SELECTORS["checkout"]

# Confirmation signals for Walmart
_CONFIRM_URL_PATTERNS = ["/checkout/thankyou", "/thank-you"]
_CONFIRM_TEXT_PATTERNS = [
    "order placed",
    "order number",
    "thanks for your order",
]


class WalmartCheckoutHandler(BaseCheckoutHandler):
    """Walmart.com checkout flow with step-based architecture."""

    retailer_name = "walmart"

    def __init__(self, engine=None, vision_helper=None, max_price: float = 0):
        super().__init__(vision_helper=vision_helper, max_price=max_price)
        self._engine = engine

    # -- Helpers delegating to engine -----------------------------------------

    async def _smart_click(self, page, desc: str, sel: str, timeout: int = 5000) -> bool:
        if self._engine:
            return await self._engine._smart_click(page, desc, sel, timeout=timeout)
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=timeout):
                await human_click_element(page, loc)
                return True
        except Exception:
            pass
        return False

    async def _smart_read_error(self, page) -> str | None:
        if self._engine:
            return await self._engine._smart_read_error(page)
        return None

    # -- Step methods ---------------------------------------------------------

    async def navigate_to_cart(self, page) -> StepResult:
        """Add item to cart and navigate to checkout."""
        await sweep_popups(page)
        await random_mouse_jitter(page)
        await idle_scroll(page)
        await random_delay(page, 500, 1500)
        await sweep_popups(page)

        # Add to cart
        if not await self._smart_click(page, "Add to cart", _PDP["add_to_cart"]):
            # Retry after popup sweep
            if await sweep_popups(page):
                await self._smart_click(page, "Add to cart", _PDP["add_to_cart"])
            else:
                error = await self._smart_read_error(page)
                return StepResult(
                    success=False, step_name="navigate_to_cart",
                    message=error or "Add to cart button not found",
                )

        await random_delay(page, 1500, 2500)
        await sweep_popups(page)

        # Go to checkout
        if not await self._smart_click(page, "Check out", _CART["checkout"], timeout=3000):
            await page.goto("https://www.walmart.com/checkout", wait_until="domcontentloaded")
            await wait_for_page_ready(page, timeout=10000)

        await sweep_popups(page)
        return StepResult(success=True, step_name="navigate_to_cart")

    async def verify_cart_contents(self, page) -> StepResult:
        """Verify checkout page loaded (Walmart goes directly to checkout)."""
        current_url = page.url.lower()
        if "checkout" in current_url or "cart" in current_url:
            return StepResult(success=True, step_name="verify_cart_contents")
        return StepResult(
            success=False, step_name="verify_cart_contents",
            message="Not on checkout or cart page",
        )

    async def proceed_to_checkout(self, page) -> StepResult:
        """Handle sign-in or guest checkout if needed.

        Walmart may show sign-in at checkout. This step handles the
        guest checkout fallback. Full sign-in is delegated to the engine.
        """
        # Guest checkout fallback
        await self._smart_click(
            page, "Continue as guest", _CKO["guest_checkout"], timeout=2000,
        )
        await sweep_popups(page)
        return StepResult(success=True, step_name="proceed_to_checkout")

    async def fill_payment(self, page, creds) -> StepResult:
        """Walmart uses saved payment -- no action needed in most cases."""
        # Payment is pre-filled for signed-in users with saved cards.
        # Guest checkout would need full payment form, but that is not
        # currently supported (too complex for automated checkout).
        await sweep_popups(page)
        return StepResult(success=True, step_name="fill_payment", message="saved_payment")

    async def review_order(self, page) -> StepResult:
        """Check price guard on review page."""
        return await super().review_order(page)

    async def place_order(self, page) -> StepResult:
        """Click 'Place order'."""
        if await self._smart_click(page, "Place order", _CKO["place_order"], timeout=15000):
            await wait_for_page_ready(page, timeout=10000)
            return StepResult(success=True, step_name="place_order")

        error = await self._smart_read_error(page)
        return StepResult(
            success=False, step_name="place_order",
            message=error or "Place order button not found",
            screenshot_b64=await self._screenshot(page),
        )

    async def confirm_order_placed(self, page) -> StepResult:
        """Wait for Walmart order confirmation.

        NEVER returns success=False -- UNKNOWN is not FAILED.
        """
        confirmed, order_num = await self._wait_for_confirmation_signals(
            page, _CONFIRM_URL_PATTERNS, _CONFIRM_TEXT_PATTERNS, timeout=30000,
        )
        if confirmed:
            logger.info("Walmart: order confirmed, order number: %s", order_num or "not found")
            return StepResult(
                success=True, step_name="confirm_order_placed",
                message=order_num or "confirmed",
            )
        logger.warning("Walmart: no confirmation signal after 30s -- status UNKNOWN")
        return StepResult(success=True, step_name="confirm_order_placed", message="unknown")

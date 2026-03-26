"""Target.com checkout flow handler.

CREATED [Mission 3] — Extracted from _checkout_target() in checkout/engine.py.
Uses selectors from pmon/selectors/target.py and human behavior helpers.
"""

from __future__ import annotations

import logging

from pmon.checkout_flows.base import BaseCheckoutHandler, StepResult
from pmon.checkout.human_behavior import (
    human_click_element,
    human_type,
    idle_scroll,
    random_delay,
    random_mouse_jitter,
    sweep_popups,
    wait_for_button_enabled,
    wait_for_page_ready,
)
from pmon.selectors.target import TARGET_SELECTORS

logger = logging.getLogger(__name__)

# Shorthand selector access
_PDP = TARGET_SELECTORS["pdp"]
_CART = TARGET_SELECTORS["cart"]
_CKO = TARGET_SELECTORS["checkout"]

# Confirmation signals for Target
_CONFIRM_URL_PATTERNS = ["/co-thankyou", "/checkout/confirmation"]
_CONFIRM_TEXT_PATTERNS = [
    "order #",
    "order confirmation",
    "thanks for your order",
]


class TargetCheckoutHandler(BaseCheckoutHandler):
    """Target.com checkout flow with step-based architecture."""

    retailer_name = "target"

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

    async def _nuke_floating_ui_portals(self, page) -> int:
        if self._engine:
            return await self._engine._nuke_floating_ui_portals(page)
        return 0

    async def _dismiss_health_consent(self, page) -> bool:
        if self._engine:
            return await self._engine._dismiss_health_consent_modal(page)
        return False

    async def _sweep(self, page) -> None:
        """Full popup sweep: floating-ui portals + generic popups + health consent."""
        await self._nuke_floating_ui_portals(page)
        await sweep_popups(page)
        await self._dismiss_health_consent(page)

    # -- Step methods ---------------------------------------------------------

    async def navigate_to_cart(self, page) -> StepResult:
        """Buy Now path OR add-to-cart -> view cart path."""
        await self._sweep(page)
        await idle_scroll(page)
        await random_delay(page, 500, 1500)
        await self._sweep(page)

        # --- Try Buy Now first ---
        buy_now_clicked = await self._smart_click(page, "Buy now", _PDP["buy_now"], timeout=3000)
        if buy_now_clicked:
            logger.info("Target: clicked 'Buy now' -- skipping cart")
            await wait_for_page_ready(page, timeout=15000)
            await self._sweep(page)
            return StepResult(success=True, step_name="navigate_to_cart", message="buy_now")

        # --- Add to cart flow ---
        logger.info("Target: 'Buy now' not available -- using add-to-cart flow")
        added = False
        for attempt in range(3):
            if attempt > 0:
                logger.info("Target: add-to-cart attempt %d/3", attempt + 1)
                await self._sweep(page)
                await random_delay(page, 500, 1000)

            if await self._smart_click(page, "Ship it / Add to cart", _PDP["ship_it"]):
                await random_delay(page, 1500, 2500)
                if await self._verify_add_to_cart(page):
                    logger.info("Target: item confirmed added to cart")
                    added = True
                    break
                logger.warning("Target: add-to-cart click succeeded but not confirmed")
            else:
                await self._sweep(page)

        if not added:
            error = await self._smart_read_error(page)
            msg = error or "Add to cart failed after 3 attempts"
            return StepResult(success=False, step_name="navigate_to_cart", message=msg)

        # Dismiss coverage offer
        await self._sweep(page)
        await self._smart_click(page, "No thanks / Decline coverage", _PDP["decline_coverage"], timeout=2000)
        await random_delay(page, 300, 700)

        # Navigate to cart
        if not await self._smart_click(page, "View cart & check out", _PDP["view_cart"], timeout=3000):
            await page.goto("https://www.target.com/cart", wait_until="domcontentloaded")
            await wait_for_page_ready(page, timeout=15000)

        await self._sweep(page)
        await random_mouse_jitter(page)
        return StepResult(success=True, step_name="navigate_to_cart", message="add_to_cart")

    async def _verify_add_to_cart(self, page) -> bool:
        """Check for add-to-cart confirmation modal or cart count badge."""
        for sel in _PDP["add_to_cart_confirm"]:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=1500):
                    return True
            except Exception:
                continue
        try:
            cart_count = page.locator(_CART["cart_count"])
            text = await cart_count.first.inner_text(timeout=2000)
            if text.strip() and int(text.strip()) > 0:
                return True
        except Exception:
            pass
        return False

    async def verify_cart_contents(self, page) -> StepResult:
        """Verify cart is not empty."""
        for sel in _CART["empty_cart"]:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=2000):
                    return StepResult(success=False, step_name="verify_cart_contents", message="Cart is empty")
            except Exception:
                continue
        return StepResult(success=True, step_name="verify_cart_contents")

    async def proceed_to_checkout(self, page) -> StepResult:
        """Select delivery method, wait for checkout button, click it."""
        # Select delivery method
        await self._select_delivery(page)
        await self._sweep(page)

        # Wait for checkout button
        checkout_sel = _CART["checkout_button"]
        button_enabled = await wait_for_button_enabled(page, checkout_sel, timeout=15000)

        if not button_enabled:
            # Check shipping minimum -- try switching to pickup
            logger.info("Target: checkout button disabled -- checking shipping minimum")
            switched = await self._switch_to_pickup_if_minimum(page)
            if switched:
                button_enabled = await wait_for_button_enabled(page, checkout_sel, timeout=10000)

        if not button_enabled:
            await self._sweep(page)
            await self._select_delivery(page)
            button_enabled = await wait_for_button_enabled(page, checkout_sel, timeout=10000)

        await random_delay(page, 200, 500)

        if not await self._smart_click(page, "Check out", _CART["checkout_all"]):
            return StepResult(
                success=False, step_name="proceed_to_checkout",
                message="Checkout button not found",
                screenshot_b64=await self._screenshot(page),
            )

        await wait_for_page_ready(page, timeout=15000)
        return StepResult(success=True, step_name="proceed_to_checkout")

    async def _select_delivery(self, page) -> None:
        """Select shipping/delivery method on cart page."""
        needs = False
        for ind in _CART["delivery_needed_indicators"]:
            try:
                if await page.locator(ind).first.is_visible(timeout=2000):
                    needs = True
                    break
            except Exception:
                continue
        if not needs:
            return
        for sel in _CART["shipping_options"]:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=1000):
                    await human_click_element(page, loc)
                    await wait_for_page_ready(page, timeout=5000)
                    for s_sel in _CART["save_delivery"]:
                        try:
                            s = page.locator(s_sel).first
                            if await s.is_visible(timeout=1000):
                                await human_click_element(page, s)
                                await random_delay(page, 800, 1500)
                                break
                        except Exception:
                            continue
                    return
            except Exception:
                continue

    async def _switch_to_pickup_if_minimum(self, page) -> bool:
        """Switch to pickup if cart is below shipping minimum."""
        try:
            has_minimum = False
            for sel in _CART["shipping_minimum_indicators"]:
                try:
                    if await page.locator(sel).first.is_visible(timeout=1000):
                        has_minimum = True
                        break
                except Exception:
                    continue
            if not has_minimum:
                return False

            logger.info("Target: below shipping minimum -- switching to pickup")
            for sel in _CART["pickup_options"]:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=1500):
                        await human_click_element(page, loc)
                        await wait_for_page_ready(page, timeout=8000)
                        return True
                except Exception:
                    continue
            return False
        except Exception:
            return False

    async def fill_payment(self, page, creds) -> StepResult:
        """Enter CVV for saved Target card."""
        # Navigate through checkout steps, filling CVV when found
        continue_sel = _CKO["continue_button"]
        continue_css = _CKO["continue_css"]

        for step in range(5):
            await sweep_popups(page)
            await random_mouse_jitter(page)
            await random_delay(page, 200, 600)

            # Check if Place order is visible -- we are done
            try:
                place_btn = page.locator(_CKO["place_order"])
                if await place_btn.first.is_visible(timeout=2000):
                    logger.info("Target checkout: reached order review (step %d)", step)
                    return StepResult(success=True, step_name="fill_payment")
            except Exception:
                pass

            # Look for CVV field
            cvv_filled = False
            for cvv_sel in _CKO["cvv_inputs"]:
                try:
                    cvv_input = page.locator(cvv_sel).first
                    if await cvv_input.is_visible(timeout=500):
                        if creds and creds.card_cvv:
                            await human_click_element(page, cvv_input)
                            await random_delay(page, 150, 300)
                            await human_type(page, creds.card_cvv)
                            logger.info("Target checkout: entered CVV via %s", cvv_sel)
                            cvv_filled = True
                            await random_delay(page, 300, 600)
                        else:
                            logger.warning("Target checkout: CVV field found but no CVV configured")
                        break
                except Exception:
                    continue

            if cvv_filled:
                await random_delay(page, 300, 600)

            # Click continue
            await wait_for_button_enabled(page, continue_css, timeout=10000)
            await random_delay(page, 100, 300)
            try:
                cont_btn = page.locator(continue_sel).first
                if await cont_btn.is_visible(timeout=3000):
                    await human_click_element(page, cont_btn)
                    logger.info("Target checkout: clicked continue (step %d)", step + 1)
                    await wait_for_page_ready(page, timeout=10000)
                else:
                    break
            except Exception:
                break

        return StepResult(success=True, step_name="fill_payment")

    async def review_order(self, page) -> StepResult:
        """Check price guard on the order review page."""
        return await super().review_order(page)

    async def place_order(self, page) -> StepResult:
        """Click 'Place your order'."""
        if await self._smart_click(page, "Place your order", _CKO["place_order"], timeout=10000):
            await wait_for_page_ready(page, timeout=10000)
            return StepResult(success=True, step_name="place_order")

        error = await self._smart_read_error(page)
        return StepResult(
            success=False, step_name="place_order",
            message=error or "Place order button not found",
            screenshot_b64=await self._screenshot(page),
        )

    async def confirm_order_placed(self, page) -> StepResult:
        """Wait for Target order confirmation.

        NEVER returns success=False -- UNKNOWN is not FAILED.
        """
        confirmed, order_num = await self._wait_for_confirmation_signals(
            page, _CONFIRM_URL_PATTERNS, _CONFIRM_TEXT_PATTERNS, timeout=30000,
        )
        if confirmed:
            logger.info("Target: order confirmed, order number: %s", order_num or "not found")
            return StepResult(success=True, step_name="confirm_order_placed", message=order_num or "confirmed")
        logger.warning("Target: no confirmation signal after 30s -- status UNKNOWN")
        return StepResult(success=True, step_name="confirm_order_placed", message="unknown")

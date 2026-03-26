"""PokemonCenter.com checkout flow handler.

CREATED [Mission 3] — Extracted from checkout/engine.py. PKC requires full
payment entry on every checkout (no saved cards).
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
    wait_for_page_ready,
)
from pmon.selectors.pokemoncenter import POKEMONCENTER_SELECTORS

logger = logging.getLogger(__name__)

_PDP = POKEMONCENTER_SELECTORS["pdp"]
_CART = POKEMONCENTER_SELECTORS["cart"]
_CKO = POKEMONCENTER_SELECTORS["checkout"]

# Confirmation signals for Pokemon Center
_CONFIRM_URL_PATTERNS = ["/order/confirmation", "/checkout/success"]
_CONFIRM_TEXT_PATTERNS = [
    "order #",
    "thank you for your order",
    "order confirmation",
]

_SHIPPING_FIELDS = [
    ('#firstName, input[name="firstName"], input[autocomplete="given-name"]', "first_name"),
    ('#lastName, input[name="lastName"], input[autocomplete="family-name"]', "last_name"),
    ('#address1, input[name="address1"], input[autocomplete="address-line1"]', "address_line1"),
    ('#address2, input[name="address2"], input[autocomplete="address-line2"]', "address_line2"),
    ('#city, input[name="city"], input[autocomplete="address-level2"]', "city"),
    ('#zipCode, input[name="zipCode"], input[name="zip"], input[autocomplete="postal-code"]', "zip_code"),
    ('#phone, input[name="phone"], input[type="tel"]', "phone"),
]
_STATE_SEL = '#state, select[name="state"], select[name="region"]'
_CARD_NUMBER_SEL = ('#cardNumber, input[name="cardNumber"], input[name="card_number"], '
                    'input[autocomplete="cc-number"], input[placeholder*="Card number" i]')
_IFRAME_SELS = ['iframe[name*="card" i]', 'iframe[name*="payment" i]',
                'iframe[src*="braintree" i]', 'iframe[src*="stripe" i]',
                'iframe[title*="card" i]', 'iframe[id*="card" i]']
_IFRAME_CARD_SEL = ('input[name="cardnumber"], input[name="card-number"], '
                    'input[autocomplete="cc-number"], input[name="number"], input[placeholder*="Card" i]')
_IFRAME_EXP_SEL = ('input[name="exp-date"], input[name="expiryDate"], '
                   'input[autocomplete="cc-exp"], input[placeholder*="MM" i]')
_IFRAME_CVV_SEL = ('input[name="cvc"], input[name="cvv"], '
                   'input[autocomplete="cc-csc"], input[placeholder*="CVV" i]')
_CONTINUE_SEL = ('button:has-text("Continue"), button:has-text("Next"), '
                 'button:has-text("Continue to Payment"), button:has-text("Save & Continue")')


class PokemonCenterCheckoutHandler(BaseCheckoutHandler):
    """PokemonCenter.com checkout flow with step-based architecture."""

    retailer_name = "pokemoncenter"

    def __init__(self, engine=None, vision_helper=None, max_price: float = 0):
        super().__init__(vision_helper=vision_helper, max_price=max_price)
        self._engine = engine
        self._profile = None  # Set by runner before calling steps

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
        """Add item to cart and navigate to cart/checkout."""
        await sweep_popups(page)
        await random_mouse_jitter(page)
        await idle_scroll(page)
        await random_delay(page, 500, 1500)

        # Add to cart
        if not await self._smart_click(page, "Add to Cart", _PDP["add_to_cart"], timeout=10000):
            error = await self._smart_read_error(page)
            return StepResult(
                success=False, step_name="navigate_to_cart",
                message=error or "Add to cart button not found",
            )
        await random_delay(page, 1500, 2500)

        # Go to cart
        if not await self._smart_click(page, "Go to Cart", _CART["go_to_cart"]):
            await page.goto("https://www.pokemoncenter.com/cart", wait_until="domcontentloaded")
        await wait_for_page_ready(page, timeout=10000)

        return StepResult(success=True, step_name="navigate_to_cart")

    async def verify_cart_contents(self, page) -> StepResult:
        """Verify we are on the cart page."""
        current_url = page.url.lower()
        if "cart" in current_url or "checkout" in current_url:
            return StepResult(success=True, step_name="verify_cart_contents")
        return StepResult(
            success=False, step_name="verify_cart_contents",
            message="Not on cart or checkout page",
        )

    async def proceed_to_checkout(self, page) -> StepResult:
        """Click Checkout button on cart page."""
        if not await self._smart_click(page, "Checkout", _CART["checkout"]):
            return StepResult(
                success=False, step_name="proceed_to_checkout",
                message="Checkout button not found",
                screenshot_b64=await self._screenshot(page),
            )
        await wait_for_page_ready(page, timeout=15000)
        return StepResult(success=True, step_name="proceed_to_checkout")

    async def fill_shipping(self, page) -> StepResult:
        """Fill shipping address fields if empty."""
        if not self._profile:
            return StepResult(success=True, step_name="fill_shipping", message="no_profile")

        await sweep_popups(page)
        await random_mouse_jitter(page)

        for sel, attr_name in _SHIPPING_FIELDS:
            value = getattr(self._profile, attr_name, None)
            if not value:
                continue
            try:
                field_elem = page.locator(sel).first
                if await field_elem.is_visible(timeout=1000):
                    current_val = await field_elem.input_value(timeout=500)
                    if not current_val:
                        await human_click_element(page, field_elem)
                        await random_delay(page, 100, 250)
                        await human_type(page, value)
                        await random_delay(page, 150, 400)
            except Exception:
                continue

        # State dropdown
        state = getattr(self._profile, "state", None)
        if state:
            try:
                se = page.locator(_STATE_SEL).first
                if await se.is_visible(timeout=1000) and not await se.input_value(timeout=500):
                    await se.select_option(value=state)
            except Exception:
                pass
        # Email field
        email = getattr(self._profile, "email", None)
        if email:
            try:
                ee = page.locator('#email, input[name="email"][autocomplete="email"]').first
                if await ee.is_visible(timeout=1000) and not await ee.input_value(timeout=500):
                    await human_click_element(page, ee)
                    await human_type(page, email)
            except Exception:
                pass
        # Click Continue to proceed to payment
        try:
            cont_btn = page.locator(_CONTINUE_SEL)
            if await cont_btn.first.is_visible(timeout=3000):
                await human_click_element(page, cont_btn)
                await wait_for_page_ready(page, timeout=10000)
                await random_delay(page, 500, 1000)
        except Exception:
            pass

        return StepResult(success=True, step_name="fill_shipping")

    async def fill_payment(self, page, creds) -> StepResult:
        """Fill payment info -- PKC requires full card entry every time."""
        await sweep_popups(page)

        if not creds or not creds.card_number:
            logger.warning("PKC checkout: no card number in credentials")
            return StepResult(success=False, step_name="fill_payment", message="No card number")

        card_filled = await self._fill_card_direct(page, creds)
        if not card_filled:
            card_filled = await self._fill_card_iframe(page, creds)

        if not card_filled:
            return StepResult(
                success=False, step_name="fill_payment",
                message="Could not fill card number",
                screenshot_b64=await self._screenshot(page),
            )
        return StepResult(success=True, step_name="fill_payment")

    async def _fill_card_direct(self, page, creds) -> bool:
        """Try filling card number directly on the page."""
        try:
            card_input = page.locator(_CARD_NUMBER_SEL).first
            if await card_input.is_visible(timeout=3000):
                await human_click_element(page, card_input)
                await random_delay(page, 100, 250)
                await human_type(page, creds.card_number)
                await random_delay(page, 200, 500)
                return True
        except Exception:
            pass
        return False

    async def _fill_card_iframe(self, page, creds) -> bool:
        """Try filling card number inside a payment iframe."""
        for iframe_sel in _IFRAME_SELS:
            try:
                if not await page.locator(iframe_sel).first.is_visible(timeout=2000):
                    continue
                frame = page.frame_locator(iframe_sel)
                card_in_frame = frame.locator(_IFRAME_CARD_SEL).first
                await card_in_frame.wait_for(state="visible", timeout=3000)
                await card_in_frame.click()
                await card_in_frame.type(creds.card_number, delay=50)
                # Expiry
                try:
                    exp_input = frame.locator(_IFRAME_EXP_SEL).first
                    if await exp_input.is_visible(timeout=1000):
                        exp_val = f"{creds.card_exp_month}/{creds.card_exp_year[-2:]}" if creds.card_exp_year else creds.card_exp_month
                        await exp_input.click()
                        await exp_input.type(exp_val, delay=50)
                except Exception:
                    pass
                try:
                    cvv_input = frame.locator(_IFRAME_CVV_SEL).first
                    if await cvv_input.is_visible(timeout=1000):
                        await cvv_input.click()
                        await cvv_input.type(creds.card_cvv, delay=50)
                except Exception:
                    pass

                return True
            except Exception:
                continue
        return False

    async def review_order(self, page) -> StepResult:
        """Check price guard on review page."""
        return await super().review_order(page)

    async def place_order(self, page) -> StepResult:
        """Click 'Place Order'."""
        place_sel = (
            f'{_CKO["place_order"]}, '
            'button:has-text("Pay Now")'
        )
        if await self._smart_click(page, "Place Order", place_sel, timeout=15000):
            await wait_for_page_ready(page, timeout=10000)
            return StepResult(success=True, step_name="place_order")

        error = await self._smart_read_error(page)
        return StepResult(
            success=False, step_name="place_order",
            message=error or "Place order button not found",
            screenshot_b64=await self._screenshot(page),
        )

    async def confirm_order_placed(self, page) -> StepResult:
        """Wait for PKC order confirmation.

        NEVER returns success=False -- UNKNOWN is not FAILED.
        """
        confirmed, order_num = await self._wait_for_confirmation_signals(
            page, _CONFIRM_URL_PATTERNS, _CONFIRM_TEXT_PATTERNS, timeout=30000,
        )
        if confirmed:
            logger.info("PKC: order confirmed, order number: %s", order_num or "not found")
            return StepResult(
                success=True, step_name="confirm_order_placed",
                message=order_num or "confirmed",
            )
        logger.warning("PKC: no confirmation signal after 30s -- status UNKNOWN")
        return StepResult(success=True, step_name="confirm_order_placed", message="unknown")

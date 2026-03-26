"""Best Buy login handler (stub).

Best Buy uses a dedicated sign-in page at bestbuy.com/identity/signin.
Known challenges: reCAPTCHA is used heavily on the login page.

TODO [Mission 1]: Implement full login flow once Best Buy checkout is built.
"""

from __future__ import annotations

import logging
import time

from pmon.checkout.human_behavior import (
    human_click_element,
    human_type,
    random_delay,
    random_mouse_jitter,
    sweep_popups,
    wait_for_button_enabled,
    wait_for_page_ready,
)
from pmon.login.base import BaseLoginHandler, LoginResult, LoginStatus

logger = logging.getLogger(__name__)

# Selectors — will move to pmon/selectors/bestbuy.py when full flow is built
_EMAIL_SEL = 'input[id="fld-e"], input[type="email"], input[name="email"]'
_PASS_SEL = 'input[id="fld-p1"], input[type="password"], input[name="password"]'
_SUBMIT_SEL = 'button[type="submit"], button:has-text("Sign In")'
_ACCOUNT_SEL = 'span:has-text("Hi, "), [data-testid="account-button"]'


class BestBuyLoginHandler(BaseLoginHandler):
    """Handle Best Buy authentication (stub)."""

    retailer = "bestbuy"

    def __init__(self, *, vision_helper=None):
        self._vision = vision_helper

    async def login(self, page, credentials, **kwargs) -> LoginResult:
        """Navigate to Best Buy sign-in and fill credentials."""
        start = time.monotonic()
        user_id = getattr(credentials, "user_id", None)

        await page.goto("https://www.bestbuy.com/identity/signin", wait_until="domcontentloaded")
        await wait_for_page_ready(page, timeout=15000)
        await sweep_popups(page)
        await random_mouse_jitter(page)

        # CAPTCHA gate — Best Buy shows reCAPTCHA before the form
        if await self._detect_captcha(page):
            ss = await self._screenshot(page)
            return self._make_result(
                LoginStatus.CAPTCHA, user_id=user_id, start_time=start,
                failure_reason="reCAPTCHA on login page", screenshot_b64=ss,
            )

        # Fill email
        try:
            email_field = page.locator(_EMAIL_SEL).first
            await email_field.wait_for(state="visible", timeout=10000)
            await human_click_element(page, email_field)
            await random_delay(page, 200, 400)
            await human_type(page, credentials.email)
            await random_delay(page, 300, 600)
        except Exception:
            ss = await self._screenshot(page)
            return self._make_result(
                LoginStatus.FAILED, user_id=user_id, start_time=start,
                failure_reason="Email field not found", screenshot_b64=ss,
            )

        # Fill password
        try:
            pass_field = page.locator(_PASS_SEL).first
            await pass_field.wait_for(state="visible", timeout=5000)
            await human_click_element(page, pass_field)
            await random_delay(page, 150, 300)
            await human_type(page, credentials.password)
            await random_delay(page, 200, 500)
        except Exception:
            ss = await self._screenshot(page)
            return self._make_result(
                LoginStatus.FAILED, user_id=user_id, start_time=start,
                failure_reason="Password field not found", screenshot_b64=ss,
            )

        # Submit
        await wait_for_button_enabled(page, _SUBMIT_SEL, timeout=10000)
        try:
            await page.locator(_SUBMIT_SEL).first.click(force=True)
        except Exception:
            pass
        await wait_for_page_ready(page, timeout=15000)

        # Verify — "Hi, {name}" in header
        if await self.verify_authenticated(page):
            return self._make_result(LoginStatus.SUCCESS, user_id=user_id, start_time=start)

        if await self._detect_captcha(page):
            ss = await self._screenshot(page)
            return self._make_result(
                LoginStatus.CAPTCHA, user_id=user_id, start_time=start,
                failure_reason="reCAPTCHA after submit", screenshot_b64=ss,
            )

        ss = await self._screenshot(page)
        return self._make_result(
            LoginStatus.FAILED, user_id=user_id, start_time=start,
            failure_reason="Login did not complete", screenshot_b64=ss,
        )

    async def verify_authenticated(self, page) -> bool:
        """Check for 'Hi, {name}' greeting in header."""
        try:
            acct = page.locator(_ACCOUNT_SEL)
            if await acct.first.is_visible(timeout=3000):
                return True
        except Exception:
            pass
        return False

"""Walmart.com login handler.

Extracted from the inline sign-in block in ``_checkout_walmart()``
(checkout/engine.py lines 2017-2128) as part of Mission 1.

Walmart sign-in happens on the checkout page itself — there is no
separate navigation step.  The handler detects the sign-in prompt,
fills credentials, and waits for the ``wmt_auth`` network pattern.
"""

from __future__ import annotations

import logging
import time

from pmon.checkout.human_behavior import (
    human_click_element,
    random_delay,
    random_mouse_jitter,
    sweep_popups,
    wait_for_button_enabled,
    wait_for_page_ready,
    wait_for_url_change,
)
from pmon.checkout.network_monitor import NetworkMonitor
from pmon.login.base import BaseLoginHandler, LoginResult, LoginStatus
from pmon.selectors.walmart import WALMART_SELECTORS

logger = logging.getLogger(__name__)

_LOGIN = WALMART_SELECTORS["login"]


class WalmartLoginHandler(BaseLoginHandler):
    """Handle Walmart.com authentication on the checkout page."""

    retailer = "walmart"

    def __init__(self, *, vision_helper=None):
        self._vision = vision_helper

    async def login(self, page, credentials, **kwargs) -> LoginResult:
        """Execute Walmart sign-in on the current page."""
        start = time.monotonic()
        user_id = getattr(credentials, "user_id", None)

        # Detect sign-in prompt
        sign_in_visible = False
        try:
            sign_in_visible = await page.locator(_LOGIN["sign_in"]).first.is_visible(timeout=2000)
        except Exception:
            pass

        if not sign_in_visible:
            # No sign-in prompt — may already be authenticated
            if await self.verify_authenticated(page):
                return self._make_result(LoginStatus.SESSION_REUSED, user_id=user_id, start_time=start)
            return self._make_result(
                LoginStatus.FAILED, user_id=user_id, start_time=start,
                failure_reason="Sign-in prompt not visible on page",
            )

        # Start network monitor
        net_monitor = NetworkMonitor(page)
        net_monitor.add_pattern("wmt_auth", "/account/electrode/api/signin")
        net_monitor.add_pattern("wmt_token", "/orchestra/snb/graphql")
        await net_monitor.start()

        await random_mouse_jitter(page)

        email_sel = _LOGIN["email_input"]
        pass_sel = _LOGIN["password_input"]
        submit_sel = _LOGIN["submit_button"]

        # Check for pre-filled auth method picker
        auth_picker_visible = await self._check_auth_picker(page)

        if not auth_picker_visible:
            # Standard flow: fill email and submit
            filled = await self._fill_email(page, credentials, email_sel, submit_sel)
            if not filled:
                await net_monitor.stop()
                ss = await self._screenshot(page)
                return self._make_result(
                    LoginStatus.FAILED, user_id=user_id, start_time=start,
                    failure_reason="Could not fill email", screenshot_b64=ss,
                )

            # Check for auth picker after email submit
            await self._check_auth_picker(page)

        # Fill password
        pass_filled = await self._fill_password(page, credentials, pass_sel)
        if pass_filled:
            await wait_for_button_enabled(page, submit_sel, timeout=10000)
            pre_url = page.url

            # Click sign-in submit
            for text in _LOGIN["submit_texts"]:
                try:
                    btn = page.get_by_role("button", name=text, exact=False)
                    if await btn.first.is_visible(timeout=500):
                        await human_click_element(page, btn)
                        break
                except Exception:
                    continue
            else:
                try:
                    await page.locator(submit_sel).first.click(force=True)
                except Exception:
                    pass

            # Wait for login network pattern
            login_done = await net_monitor.wait_for("wmt_auth", timeout=15000)
            if not login_done:
                await wait_for_url_change(page, pre_url, timeout=10000)
        elif self._vision:
            # Vision-assisted sign-in
            await self._vision._smart_sign_in(page, credentials, "walmart")
        else:
            await net_monitor.stop()
            ss = await self._screenshot(page)
            return self._make_result(
                LoginStatus.FAILED, user_id=user_id, start_time=start,
                failure_reason="Password field not found", screenshot_b64=ss,
            )

        # Check if blocked
        if net_monitor.was_blocked():
            blocked = net_monitor.get_blocked_details()
            logger.warning("Walmart sign-in: blocked %d request(s)", len(blocked))
            await net_monitor.stop()
            return self._make_result(
                LoginStatus.BLOCKED, user_id=user_id, start_time=start,
                failure_reason=f"Blocked {len(blocked)} request(s)",
            )

        await net_monitor.stop()
        await sweep_popups(page)

        if await self.handle_obstacles(page):
            if await self._detect_2fa(page):
                return self._make_result(LoginStatus.REQUIRES_2FA, user_id=user_id, start_time=start)
            return self._make_result(LoginStatus.BLOCKED, user_id=user_id, start_time=start)

        if await self.verify_authenticated(page):
            return self._make_result(LoginStatus.SUCCESS, user_id=user_id, start_time=start)

        return self._make_result(
            LoginStatus.FAILED, user_id=user_id, start_time=start,
            failure_reason="Sign-in did not complete",
        )

    async def verify_authenticated(self, page) -> bool:
        """Check for account menu or absence of sign-in prompt."""
        # Sign-in prompt gone?
        try:
            still_visible = await page.locator(_LOGIN["sign_in"]).first.is_visible(timeout=1500)
            if not still_visible:
                return True
        except Exception:
            return True  # element not found → likely signed in

        # Account menu visible?
        try:
            acct = page.locator('button:has-text("Account"), [data-testid="account-menu"]')
            if await acct.first.is_visible(timeout=1500):
                return True
        except Exception:
            pass

        return False

    async def _check_auth_picker(self, page) -> bool:
        """Check for and click Password radio in auth method picker."""
        try:
            pw_radio = page.get_by_role("radio", name=_LOGIN["auth_method_radio"], exact=False)
            if await pw_radio.first.is_visible(timeout=1000):
                await human_click_element(page, pw_radio)
                await random_delay(page, 800, 1500)
                return True
        except Exception:
            pass
        return False

    async def _fill_email(self, page, credentials, email_sel: str, submit_sel: str) -> bool:
        """Fill email and submit the first step."""
        if self._vision:
            filled = await self._vision._smart_fill(page, "email/phone", email_sel, credentials.email)
        else:
            try:
                field = page.locator(email_sel).first
                await field.click()
                await field.fill(credentials.email)
                filled = True
            except Exception:
                filled = False

        if not filled:
            return False

        # Verify
        try:
            actual = await page.locator(email_sel).first.input_value(timeout=1000)
            if actual != credentials.email:
                await page.locator(email_sel).first.fill(credentials.email)
                await random_delay(page, 200, 400)
        except Exception:
            pass

        await wait_for_button_enabled(page, submit_sel, timeout=10000)
        try:
            await page.locator(submit_sel).first.click(force=True)
        except Exception:
            pass
        await wait_for_page_ready(page, timeout=10000)
        await sweep_popups(page)
        return True

    async def _fill_password(self, page, credentials, pass_sel: str) -> bool:
        """Fill the password field. Returns True if successful."""
        if self._vision:
            filled = await self._vision._smart_fill(page, "password", pass_sel, credentials.password)
        else:
            try:
                field = page.locator(pass_sel).first
                await field.wait_for(state="visible", timeout=5000)
                await field.click()
                await field.fill(credentials.password)
                filled = True
            except Exception:
                filled = False

        if not filled:
            return False

        # Verify
        try:
            pw_val = await page.locator(pass_sel).first.input_value(timeout=1000)
            if not pw_val:
                await page.locator(pass_sel).first.fill(credentials.password)
                await random_delay(page, 200, 400)
        except Exception:
            pass

        return True

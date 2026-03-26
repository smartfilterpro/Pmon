"""PokemonCenter.com login handler.

Extracted from checkout/engine.py ``_sign_in_pokemoncenter()`` (Mission 1).

Pokemon Center login is a homepage modal — there is no dedicated /login
page.  The flow navigates to pokemoncenter.com for cookie warmup, clicks
the header sign-in icon, fills the modal form, and waits for the
``pkc_auth_login`` network pattern.
"""

from __future__ import annotations

import logging
import time

from pmon.checkout.human_behavior import (
    human_click_element,
    human_type,
    idle_scroll,
    random_delay,
    random_mouse_jitter,
    sweep_popups,
    wait_for_button_enabled,
    wait_for_page_ready,
    wait_for_url_change,
)
from pmon.checkout.network_monitor import NetworkMonitor
from pmon.login.base import BaseLoginHandler, LoginResult, LoginStatus
from pmon.selectors.pokemoncenter import POKEMONCENTER_SELECTORS

logger = logging.getLogger(__name__)

_LOGIN = POKEMONCENTER_SELECTORS["login"]


class PokemonCenterLoginHandler(BaseLoginHandler):
    """Handle PokemonCenter.com authentication via homepage modal."""

    retailer = "pokemoncenter"

    def __init__(self, *, vision_helper=None):
        self._vision = vision_helper

    # ------------------------------------------------------------------
    # Main login flow
    # ------------------------------------------------------------------

    async def login(self, page, credentials, **kwargs) -> LoginResult:
        """Execute the full Pokemon Center login flow."""
        start = time.monotonic()
        user_id = getattr(credentials, "user_id", None)

        email_sel = _LOGIN["email_input"]
        pass_sel = _LOGIN["password_input"]
        submit_sel = _LOGIN["submit_button"]

        # Start network monitor
        net_monitor = NetworkMonitor(page)
        net_monitor.add_pattern("pkc_auth_login", "tpci-ecommweb-api/auth/login")
        net_monitor.add_pattern("pkc_profile", "tpci-ecommweb-api/profile/data")
        net_monitor.add_pattern("pkc_cart", "tpci-ecommweb-api/cart/data")
        net_monitor.add_pattern("pkc_account_event", "SSAccountSignInCustomEvent")
        net_monitor.add_pattern("pkc_resource_api", "site/resourceapi/account")
        await net_monitor.start()

        # Step 0 — navigate to homepage for cookie warmup
        email_input = None
        for attempt in range(3):
            if attempt == 0:
                logger.info("PKC login: navigating to homepage for cookie warm-up")
                await page.goto("https://www.pokemoncenter.com", wait_until="domcontentloaded")
            else:
                logger.warning("PKC login: form not rendered (attempt %d/3) — reloading", attempt)
                await page.reload(wait_until="domcontentloaded")

            await wait_for_page_ready(page, timeout=15000)
            await sweep_popups(page)
            await random_mouse_jitter(page)
            await idle_scroll(page)
            await random_delay(page, 800, 2000)

            # Click header sign-in
            await self._click_header_sign_in(page)
            await random_delay(page, 1000, 2500)
            await sweep_popups(page)

            # Wait for email field
            try:
                email_input = page.locator(email_sel).first
                await email_input.wait_for(state="visible", timeout=10000)
                break
            except Exception:
                email_input = None

        if email_input is None:
            await net_monitor.stop()
            ss = await self._screenshot(page)
            return self._make_result(
                LoginStatus.FAILED, user_id=user_id, start_time=start,
                failure_reason="Email field not found after 3 attempts",
                screenshot_b64=ss,
            )

        # Step 1 — fill email
        await random_mouse_jitter(page)
        await human_click_element(page, page.locator(email_sel))
        await random_delay(page, 200, 500)
        await email_input.press("Control+a")
        await human_type(page, credentials.email)
        await random_delay(page, 300, 700)

        # Verify
        try:
            actual = await email_input.input_value(timeout=1000)
            if actual != credentials.email:
                logger.warning("PKC sign-in: email mismatch — using fill()")
                await email_input.fill(credentials.email)
                await random_delay(page, 200, 400)
        except Exception:
            await email_input.fill(credentials.email)
            await random_delay(page, 200, 400)

        # Step 2 — fill password
        pass_found = False
        try:
            await page.locator(pass_sel).first.wait_for(state="visible", timeout=5000)
            pass_found = True
        except Exception:
            pass

        if pass_found:
            await human_click_element(page, page.locator(pass_sel))
            await random_delay(page, 150, 400)
            await human_type(page, credentials.password)
            await random_delay(page, 200, 600)

            # Verify
            try:
                pw_val = await page.locator(pass_sel).first.input_value(timeout=1000)
                if not pw_val:
                    await page.locator(pass_sel).first.fill(credentials.password)
                    await random_delay(page, 200, 400)
            except Exception:
                pass
        elif self._vision:
            logger.warning("PKC sign-in: password field not found — trying vision")
            filled = await self._vision._smart_fill(page, "password input field", pass_sel, credentials.password)
            if not filled:
                await net_monitor.stop()
                ss = await self._screenshot(page)
                return self._make_result(
                    LoginStatus.FAILED, user_id=user_id, start_time=start,
                    failure_reason="Password field not found", screenshot_b64=ss,
                )
        else:
            await net_monitor.stop()
            ss = await self._screenshot(page)
            return self._make_result(
                LoginStatus.FAILED, user_id=user_id, start_time=start,
                failure_reason="Password field not found and no vision helper",
                screenshot_b64=ss,
            )

        # Step 3 — submit
        await wait_for_button_enabled(page, submit_sel, timeout=10000)
        await random_delay(page, 200, 500)

        pre_url = page.url
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

        # Step 4 — wait for auth network pattern
        auth_ok = await net_monitor.wait_for("pkc_auth_login", expected_count=1, timeout=15000)
        if auth_ok:
            logger.info("PKC login: auth API responded")
            try:
                await net_monitor.wait_for("pkc_profile", expected_count=1, timeout=5000)
            except Exception:
                pass
        else:
            logger.warning("PKC login: auth API not observed — checking URL change")
            await wait_for_url_change(page, pre_url, timeout=10000)

        await wait_for_page_ready(page, timeout=10000)

        # Post-login checks
        if net_monitor.was_blocked():
            blocked = net_monitor.get_blocked_details()
            logger.warning("PKC login: blocked %d request(s)", len(blocked))
        await net_monitor.stop()
        await sweep_popups(page)

        # Check obstacles
        if await self.handle_obstacles(page):
            if await self._detect_captcha(page):
                return self._make_result(LoginStatus.CAPTCHA, user_id=user_id, start_time=start)
            return self._make_result(LoginStatus.BLOCKED, user_id=user_id, start_time=start)

        if auth_ok or await self.verify_authenticated(page):
            return self._make_result(LoginStatus.SUCCESS, user_id=user_id, start_time=start)

        ss = await self._screenshot(page)
        return self._make_result(
            LoginStatus.FAILED, user_id=user_id, start_time=start,
            failure_reason="Login did not complete", screenshot_b64=ss,
        )

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    async def verify_authenticated(self, page) -> bool:
        """Check for account indicators and absence of sign-in link."""
        # Account indicators
        try:
            acct = page.locator(_LOGIN["account_indicators"])
            if await acct.first.is_visible(timeout=3000):
                return True
        except Exception:
            pass

        # URL check
        url = page.url
        if "/account" in url and "/login" not in url:
            return True

        # Sign-in link still visible → not authenticated
        try:
            still = page.locator(_LOGIN["still_sign_in"])
            if await still.first.is_visible(timeout=1500):
                return False
        except Exception:
            pass

        return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _click_header_sign_in(self, page) -> None:
        """Click the header sign-in icon/link using multiple strategies."""
        header_sel = _LOGIN["header_sign_in"]

        # Strategy 1: CSS selectors
        try:
            link = page.locator(header_sel)
            if await link.first.is_visible(timeout=8000):
                await human_click_element(page, link)
                logger.info("PKC login: clicked header sign-in via CSS")
                return
        except Exception:
            pass

        # Strategy 2: get_by_role
        for role_type in ["link", "button"]:
            for text in ["Sign In", "Log In", "Sign in", "Log in"]:
                try:
                    elem = page.get_by_role(role_type, name=text, exact=False)
                    if await elem.first.is_visible(timeout=1000):
                        await human_click_element(page, elem)
                        logger.info("PKC login: clicked sign-in via get_by_role('%s', '%s')", role_type, text)
                        return
                except Exception:
                    continue

        # Strategy 3: Vision fallback
        if self._vision:
            clicked = await self._vision._smart_click(
                page, "Sign In link or account icon in the page header",
                header_sel, timeout=5000,
            )
            if clicked:
                return

        # Strategy 4: JS class match
        logger.warning("PKC login: header sign-in not found — trying JS click on sign-in class")
        try:
            clicked_js = await page.evaluate("""() => {
                const els = document.querySelectorAll('span, a, button, div');
                for (const el of els) {
                    const cls = (el.className || '').toString().toLowerCase();
                    if ((cls.includes('sign-in') || cls.includes('signin') || cls.includes('header-sign-in'))
                        && el.offsetParent !== null) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }""")
            if clicked_js:
                logger.info("PKC login: clicked sign-in via JS class match")
        except Exception:
            pass

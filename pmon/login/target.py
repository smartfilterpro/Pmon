"""Target.com login handler.

Extracted from checkout/engine.py ``_sign_in_target()`` (Mission 1).

The Target login flow has no dedicated /login page — sign-in is triggered
from the homepage account icon which opens a side panel.  After entering
email, Target may show an auth-method picker (password, OTP, etc.) that
requires multi-strategy clicking (7 strategies from CSS to vision).
"""

from __future__ import annotations

import logging
import time

from pmon.checkout.human_behavior import (
    human_click_element, human_type, random_delay,
    random_mouse_jitter, sweep_popups,
    wait_for_button_enabled, wait_for_page_ready, wait_for_url_change,
)
from pmon.checkout.network_monitor import NetworkMonitor
from pmon.login.base import BaseLoginHandler, LoginResult, LoginStatus
from pmon.selectors.target import TARGET_SELECTORS

logger = logging.getLogger(__name__)
_LOGIN = TARGET_SELECTORS["login"]

# JS snippet for Strategy 6 — click any password-related element
_JS_CLICK_PASSWORD = """() => {
    const els = document.querySelectorAll(
        'a, button, [role="button"], [role="link"], li, div[tabindex], span[tabindex]');
    for (const el of els) {
        const t = (el.textContent || '').toLowerCase().trim();
        if (t.includes('password') && !t.includes('forgot') && el.offsetParent !== null) {
            el.click(); return true;
        }
    }
    return false;
}"""


class TargetLoginHandler(BaseLoginHandler):
    """Handle Target.com authentication."""

    retailer = "target"

    def __init__(self, *, vision_helper=None):
        self._vision = vision_helper

    async def login(self, page, credentials, **kwargs) -> LoginResult:
        """Execute the full Target login flow."""
        start = time.monotonic()
        uid = getattr(credentials, "user_id", None)
        email_sel, pass_sel = _LOGIN["email_input"], _LOGIN["password_input"]
        submit_sel = _LOGIN["submit_button"]

        net_monitor = NetworkMonitor(page)
        await net_monitor.start()

        # Step 0 — open sign-in panel and locate email field
        email_input = await self._open_login_form(page, email_sel)
        if email_input is None:
            await net_monitor.stop()
            return self._make_result(LoginStatus.FAILED, user_id=uid, start_time=start,
                                     failure_reason="Email field not found after 3 attempts",
                                     screenshot_b64=await self._screenshot(page))

        # Step 1 — enter email with human-like typing
        await self._fill_and_verify(page, email_sel, email_input, credentials.email)

        # Step 2 — single-step or multi-step
        pass_visible = False
        try:
            pass_visible = await page.locator(pass_sel).first.is_visible(timeout=1000)
        except Exception:
            pass

        if pass_visible:
            await self._fill_password_and_submit(page, credentials, pass_sel, submit_sel, net_monitor)
        else:
            r = await self._multi_step_login(page, credentials, pass_sel, submit_sel, net_monitor, start, uid)
            if r is not None:
                return r

        # Post-login
        if net_monitor.was_blocked():
            logger.warning("Target sign-in: blocked %d request(s)", len(net_monitor.get_blocked_details()))
        await net_monitor.stop()
        await sweep_popups(page)

        if await self.handle_obstacles(page):
            if await self._detect_captcha(page):
                return self._make_result(LoginStatus.CAPTCHA, user_id=uid, start_time=start)
            if await self._detect_2fa(page):
                return self._make_result(LoginStatus.REQUIRES_2FA, user_id=uid, start_time=start)
            return self._make_result(LoginStatus.BLOCKED, user_id=uid, start_time=start,
                                     failure_reason="Account locked or blocked")

        if await self.verify_authenticated(page):
            return self._make_result(LoginStatus.SUCCESS, user_id=uid, start_time=start)

        final_url = page.url
        if any(ind in final_url.lower() for ind in _LOGIN["login_indicators"]):
            return self._make_result(LoginStatus.FAILED, user_id=uid, start_time=start,
                                     failure_reason=f"Still on login page: {final_url}",
                                     screenshot_b64=await self._screenshot(page))
        return self._make_result(LoginStatus.SUCCESS, user_id=uid, start_time=start)

    async def verify_authenticated(self, page) -> bool:
        """Check account nav text and auth cookies."""
        try:
            nav = page.locator(_LOGIN["account_nav"])
            if await nav.first.is_visible(timeout=3000):
                text = await nav.first.inner_text(timeout=1000)
                if text and "sign in" not in text.lower():
                    return True
        except Exception:
            pass
        try:
            names = {c["name"] for c in await page.context.cookies()}
            if "accessToken" in names or "refreshToken" in names:
                return True
        except Exception:
            pass
        return False

    # -- Navigation helpers ------------------------------------------------

    async def _open_login_form(self, page, email_sel: str):
        """Navigate to homepage, open side panel, return email input or None."""
        for attempt in range(3):
            if attempt == 0:
                await page.goto("https://www.target.com", wait_until="domcontentloaded")
            else:
                logger.warning("Target login: form not rendered (attempt %d/3)", attempt)
                await page.reload(wait_until="domcontentloaded")
            await wait_for_page_ready(page, timeout=15000)
            await sweep_popups(page)
            await random_mouse_jitter(page)
            await random_delay(page, 500, 1500)

            # Click account icon
            try:
                link = page.locator(_LOGIN["account_link"])
                if await link.first.is_visible(timeout=8000):
                    await human_click_element(page, link)
                elif self._vision:
                    await self._vision._smart_click(page, "Account or Sign in icon",
                                                    _LOGIN["account_link"], timeout=5000)
            except Exception:
                if self._vision:
                    await self._vision._smart_click(page, "Account or Sign in icon",
                                                    _LOGIN["account_link"], timeout=5000)
            await random_delay(page, 1000, 2000)

            # Click "Sign in or create account"
            try:
                btn = page.locator(_LOGIN["sign_in_panel"])
                if await btn.first.is_visible(timeout=5000):
                    await human_click_element(page, btn)
                    await wait_for_page_ready(page, timeout=15000)
                    await random_delay(page, 500, 1500)
            except Exception:
                pass
            await sweep_popups(page)
            await random_mouse_jitter(page)

            try:
                el = page.locator(email_sel).first
                await el.wait_for(state="visible", timeout=15000)
                return el
            except Exception:
                continue
        return None

    # -- Credential helpers ------------------------------------------------

    async def _fill_and_verify(self, page, sel: str, element, value: str) -> None:
        """Click, type with human_type, verify, fallback to fill()."""
        await human_click_element(page, page.locator(sel))
        await random_delay(page, 200, 400)
        await element.press("Control+a")
        await human_type(page, value)
        await random_delay(page, 300, 600)
        try:
            actual = await element.input_value(timeout=1000)
            if actual != value:
                await element.fill(value)
                await random_delay(page, 200, 400)
        except Exception:
            await element.fill(value)

    async def _fill_password_and_submit(self, page, creds, pass_sel, submit_sel, net_monitor) -> None:
        """Fill password and click submit."""
        await human_click_element(page, page.locator(pass_sel))
        await random_delay(page, 150, 300)
        await human_type(page, creds.password)
        await random_delay(page, 200, 500)
        try:
            if not await page.locator(pass_sel).first.input_value(timeout=1000):
                await page.locator(pass_sel).first.fill(creds.password)
        except Exception:
            pass
        await wait_for_button_enabled(page, submit_sel, timeout=15000)
        await random_delay(page, 100, 300)
        pre_url = page.url
        for sel in [submit_sel, _LOGIN["submit_fallback"]]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1000):
                    await btn.click(force=True)
                    break
            except Exception:
                continue
        if not await net_monitor.wait_for_login_complete(timeout=20000):
            await wait_for_url_change(page, pre_url, timeout=10000)

    async def _multi_step_login(self, page, creds, pass_sel, submit_sel, net_monitor, start, uid):
        """Submit email, pick auth method, enter password. Returns LoginResult on failure, else None."""
        await wait_for_button_enabled(page, submit_sel, timeout=5000)
        await random_delay(page, 100, 300)
        for sel in [submit_sel, 'button:has-text("Continue")', 'button:has-text("Sign in")']:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1000):
                    await btn.click(force=True)
                    break
            except Exception:
                continue
        await wait_for_page_ready(page, timeout=10000)
        await sweep_popups(page)

        for check_label in ("email submit", "auth picker"):
            url = page.url
            if not any(ind in url.lower() for ind in _LOGIN["login_indicators"]):
                logger.info("Target sign-in: already signed in after %s", check_label)
                await net_monitor.stop()
                return None
            if check_label == "email submit":
                await self._select_password_option(page, pass_sel)

        # Wait for password field
        pass_found = False
        try:
            await page.locator(pass_sel).first.wait_for(state="visible", timeout=10000)
            pass_found = True
        except Exception:
            pass

        if pass_found:
            await self._fill_password_and_submit(page, creds, pass_sel, submit_sel, net_monitor)
            return None
        if self._vision:
            if await self._vision._smart_fill(page, "password input field", pass_sel, creds.password):
                return None
        await net_monitor.stop()
        return self._make_result(LoginStatus.FAILED, user_id=uid, start_time=start,
                                 failure_reason="Password field not found",
                                 screenshot_b64=await self._screenshot(page))

    async def _select_password_option(self, page, pass_sel: str) -> None:
        """Try 7 strategies to select the password auth method."""
        await sweep_popups(page)
        try:
            if await page.locator(pass_sel).first.is_visible(timeout=2000):
                return
        except Exception:
            pass

        pw_texts = _LOGIN["auth_method_texts"]
        clicked = False

        # Strategies 1-3: get_by_role (button, link, radio)
        for role in ("button", "link"):
            if clicked:
                break
            for text in pw_texts:
                try:
                    opt = page.get_by_role(role, name=text, exact=False)
                    if await opt.first.is_visible(timeout=500):
                        await human_click_element(page, opt)
                        clicked = True
                        break
                except Exception:
                    continue
        if not clicked:
            try:
                opt = page.get_by_role("radio", name="Password", exact=False)
                if await opt.first.is_visible(timeout=500):
                    await human_click_element(page, opt)
                    clicked = True
            except Exception:
                pass

        # Strategy 4: get_by_text
        if not clicked:
            for text in pw_texts:
                try:
                    opt = page.get_by_text(text, exact=False)
                    if await opt.first.is_visible(timeout=500):
                        await human_click_element(page, opt)
                        clicked = True
                        break
                except Exception:
                    continue

        # Strategy 5: CSS
        if not clicked:
            try:
                opt = page.locator(_LOGIN["auth_method_css"])
                if await opt.first.is_visible(timeout=1000):
                    await human_click_element(page, opt)
                    clicked = True
            except Exception:
                pass

        # Strategy 6: JS click
        if not clicked:
            try:
                if await page.evaluate(_JS_CLICK_PASSWORD):
                    clicked = True
            except Exception:
                pass

        # Strategy 7: Vision
        if not clicked and self._vision:
            clicked = await self._vision._smart_click(
                page, "Password option (radio or link for password sign-in)", "", timeout=2000)

        if clicked:
            await wait_for_page_ready(page, timeout=8000)
        else:
            logger.warning("Target sign-in: could not find password auth method option")

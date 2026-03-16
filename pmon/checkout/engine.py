"""Checkout engine: API-first with optional Playwright browser fallback."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path

from pmon.config import Config, AccountCredentials, Profile
from pmon.models import CheckoutResult, CheckoutStatus
from pmon.checkout.api_checkout import ApiCheckout

logger = logging.getLogger(__name__)

# Directory to store browser session data (cookies, etc.)
SESSION_DIR = Path(__file__).parent.parent.parent / ".sessions"

# Stealth JS to inject into every page to reduce bot detection
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = {runtime: {}};
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : originalQuery(parameters);
"""


class CheckoutEngine:
    """API-first checkout with browser fallback and Claude vision assist."""

    def __init__(self, config: Config):
        self.config = config
        self._api = ApiCheckout()
        self._browser = None
        self._playwright = None
        self._browser_available = False
        self._anthropic = None
        self._vision_available = False
        SESSION_DIR.mkdir(exist_ok=True)
        self._init_vision()

    def _init_vision(self):
        """Initialize Claude API client for vision-based fallback."""
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            logger.info("ANTHROPIC_API_KEY not set — vision fallback disabled")
            return
        try:
            import anthropic
            self._anthropic = anthropic.Anthropic(api_key=api_key)
            self._vision_available = True
            logger.info("Claude vision fallback enabled")
        except ImportError:
            logger.info("anthropic package not installed — vision fallback disabled")

    async def _screenshot_b64(self, page) -> str:
        """Take a screenshot and return as base64."""
        raw = await page.screenshot(type="png")
        return base64.b64encode(raw).decode()

    def _ask_vision(self, screenshot_b64: str, prompt: str) -> str | None:
        """Send a screenshot to Claude and get a response. Returns None on failure."""
        if not self._vision_available:
            return None
        try:
            resp = self._anthropic.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": screenshot_b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            return resp.content[0].text
        except Exception as e:
            logger.warning(f"Vision API call failed: {e}")
            return None

    async def _smart_click(self, page, description: str, selectors: str, timeout: int = 5000) -> bool:
        """Try CSS selectors first; fall back to Claude vision to find and click an element.

        Returns True if click succeeded, False otherwise.
        """
        # Fast path: selectors
        try:
            elem = page.locator(selectors)
            await elem.first.click(timeout=timeout)
            return True
        except Exception:
            pass

        # Slow path: vision
        screenshot = await self._screenshot_b64(page)
        answer = self._ask_vision(
            screenshot,
            f'I need to click the "{description}" button/link on this page. '
            f"Return ONLY a JSON object with the x,y pixel coordinates to click: "
            f'{{"x": N, "y": N}}. If the element is not visible, return {{"x": null, "y": null}}.',
        )
        if not answer:
            return False
        try:
            coords = json.loads(answer.strip())
            if coords.get("x") is not None and coords.get("y") is not None:
                await page.mouse.click(int(coords["x"]), int(coords["y"]))
                return True
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning(f"Vision click parse error for '{description}': {e}")
        return False

    async def _smart_fill(self, page, description: str, selectors: str, value: str, timeout: int = 5000) -> bool:
        """Try CSS selectors first; fall back to Claude vision to find and fill an input.

        Returns True if fill succeeded, False otherwise.
        """
        # Fast path: selectors
        try:
            elem = page.locator(selectors)
            await elem.first.wait_for(state="visible", timeout=timeout)
            await elem.first.fill(value)
            return True
        except Exception:
            pass

        # Slow path: vision — click on field first, then type
        screenshot = await self._screenshot_b64(page)
        answer = self._ask_vision(
            screenshot,
            f'I need to click the "{description}" input field on this page to type into it. '
            f"Return ONLY a JSON object with the x,y pixel coordinates of the input: "
            f'{{"x": N, "y": N}}. If the field is not visible, return {{"x": null, "y": null}}.',
        )
        if not answer:
            return False
        try:
            coords = json.loads(answer.strip())
            if coords.get("x") is not None and coords.get("y") is not None:
                await page.mouse.click(int(coords["x"]), int(coords["y"]))
                await page.keyboard.type(value, delay=50)
                return True
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning(f"Vision fill parse error for '{description}': {e}")
        return False

    async def _smart_sign_in(self, page, creds: AccountCredentials, retailer: str) -> bool:
        """Vision-assisted sign-in: screenshot the page, ask Claude what to do.

        Used as a last-resort when selector-based sign-in fails.
        Returns True if sign-in appears to have succeeded.
        """
        if not self._vision_available:
            return False

        screenshot = await self._screenshot_b64(page)
        answer = self._ask_vision(
            screenshot,
            "This is a retailer checkout or sign-in page. I need to sign in. "
            "Analyze the page and return a JSON object describing what actions to take, in order. "
            "Each action is either a click or a fill. Format:\n"
            '{"actions": [\n'
            '  {"type": "fill", "description": "email field", "x": N, "y": N, "value": "EMAIL"},\n'
            '  {"type": "fill", "description": "password field", "x": N, "y": N, "value": "PASSWORD"},\n'
            '  {"type": "click", "description": "sign in button", "x": N, "y": N}\n'
            "]}\n"
            "Replace EMAIL and PASSWORD with the literal strings EMAIL and PASSWORD (I will substitute them). "
            "If no sign-in form is visible, return {\"actions\": []}.",
        )
        if not answer:
            return False

        try:
            plan = json.loads(answer.strip())
            actions = plan.get("actions", [])
            if not actions:
                return False

            for action in actions:
                x, y = int(action["x"]), int(action["y"])
                if action["type"] == "fill":
                    val = action.get("value", "")
                    val = val.replace("EMAIL", creds.email).replace("PASSWORD", creds.password)
                    await page.mouse.click(x, y)
                    await page.keyboard.type(val, delay=50)
                elif action["type"] == "click":
                    await page.mouse.click(x, y)
                await page.wait_for_timeout(1000)

            await page.wait_for_timeout(3000)
            return True
        except Exception as e:
            logger.warning(f"Vision sign-in failed for {retailer}: {e}")
            return False

    async def _smart_read_error(self, page) -> str | None:
        """Screenshot the page and ask Claude if there's an error message visible."""
        if not self._vision_available:
            return None
        screenshot = await self._screenshot_b64(page)
        answer = self._ask_vision(
            screenshot,
            "Is there an error message, alert, or blocking issue visible on this page? "
            "If yes, return a JSON object: {\"error\": \"description of the error\"}. "
            "If no error is visible, return {\"error\": null}.",
        )
        if not answer:
            return None
        try:
            result = json.loads(answer.strip())
            return result.get("error")
        except (json.JSONDecodeError, TypeError):
            return None

    async def _multi_strategy_click(self, page, description: str, button_texts: list[str], css_fallback: str, timeout: int = 3000) -> bool:
        """Multi-strategy button click: get_by_role → get_by_text → CSS → vision.

        Matches the robust approach used in test login.
        """
        # Strategy 1: CSS selectors (fast path)
        if css_fallback:
            try:
                elem = page.locator(css_fallback)
                if await elem.first.is_visible(timeout=min(timeout, 1000)):
                    await elem.first.click()
                    return True
            except Exception:
                pass

        # Strategy 2: get_by_role with each text variation
        for btn_text in button_texts:
            try:
                btn = page.get_by_role("button", name=btn_text, exact=False)
                if await btn.first.is_visible(timeout=500):
                    await btn.first.click()
                    return True
            except Exception:
                continue

        # Strategy 3: get_by_text (catches links/divs acting as buttons)
        for btn_text in button_texts:
            try:
                link = page.get_by_text(btn_text, exact=False)
                if await link.first.is_visible(timeout=500):
                    await link.first.click()
                    return True
            except Exception:
                continue

        # Strategy 4: Vision fallback
        return await self._smart_click(page, description, "", timeout=1000)

    async def start(self):
        """Try to launch the browser for fallback. Not required."""
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-web-security",
                    "--disable-features=VizDisplayCompositor",
                ],
            )
            self._browser_available = True
            logger.info("Browser fallback available (headless)")
        except Exception as e:
            logger.info(f"Browser fallback unavailable: {e}")
            logger.info("API-only checkout mode — this is fine for most retailers")

    async def stop(self):
        """Close resources."""
        await self._api.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def attempt_checkout(
        self,
        url: str,
        retailer: str,
        product_name: str,
        profile_name: str = "default",
        dry_run: bool = False,
    ) -> CheckoutResult:
        """Attempt checkout: API first, browser fallback.

        If dry_run=True, runs the full checkout flow but stops right before
        clicking "Place order". Useful for testing the entire flow without
        actually purchasing.
        """
        profile = self.config.profiles.get(profile_name) or Profile()
        creds = self.config.accounts.get(retailer)

        if not creds:
            return CheckoutResult(
                url=url,
                retailer=retailer,
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=f"No credentials for {retailer}. Add them in config.",
            )

        if dry_run:
            logger.info(f"DRY RUN: testing checkout flow for {product_name} (will NOT place order)")

        # Try API checkout first (fast, headless, works on cloud) — skip in dry-run
        if not dry_run:
            logger.info(f"Trying API checkout for {product_name}...")
            result = await self._api.attempt(url, retailer, product_name, profile, creds)

            if result.status == CheckoutStatus.SUCCESS:
                return result

        # If API failed (or dry-run) and browser is available, try browser fallback
        if self._browser_available:
            if not dry_run:
                logger.info(f"API failed, trying browser fallback for {product_name}...")
            try:
                handler = getattr(self, f"_checkout_{retailer}", None)
                if handler:
                    return await handler(url, product_name, profile, creds, dry_run=dry_run)
            except Exception as e:
                logger.error(f"Browser checkout also failed: {e}")
                return CheckoutResult(
                    url=url,
                    retailer=retailer,
                    product_name=product_name,
                    status=CheckoutStatus.FAILED,
                    error_message=f"Both API and browser checkout failed: {e}",
                )

        # Return failure — API failed and no browser fallback
        return CheckoutResult(
            url=url,
            retailer=retailer,
            product_name=product_name,
            status=CheckoutStatus.FAILED,
            error_message=f"Checkout failed for {retailer} — no browser fallback available",
        )

    async def _get_context(self, retailer: str):
        """Get or create a browser context with persistent cookies and stealth."""
        storage_path = SESSION_DIR / f"{retailer}.json"
        ctx_kwargs = dict(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="America/New_York",
        )
        if storage_path.exists():
            ctx_kwargs["storage_state"] = str(storage_path)
        context = await self._browser.new_context(**ctx_kwargs)
        await context.add_init_script(STEALTH_JS)
        return context

    async def _save_context(self, context, retailer: str):
        """Save browser cookies/state for reuse."""
        storage_path = SESSION_DIR / f"{retailer}.json"
        await context.storage_state(path=str(storage_path))

    async def _checkout_target(
        self, url: str, product_name: str, profile: Profile, creds: AccountCredentials, dry_run: bool = False
    ) -> CheckoutResult:
        """Target checkout flow."""
        context = await self._get_context("target")
        page = await context.new_page()

        try:
            # Navigate to product page
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            # Check if we need to sign in
            if not await self._is_signed_in_target(page):
                await self._sign_in_target(page, creds)
                # Vision fallback if selector-based sign-in didn't work
                if not await self._is_signed_in_target(page):
                    await page.goto(url, wait_until="domcontentloaded")
                    await page.wait_for_timeout(2000)

            # Try to add to cart — decline coverage if offered
            if not await self._smart_click(
                page, "Add to cart / Ship it",
                'button[data-test="shipItButton"], button:has-text("Add to cart"), button:has-text("Ship it")',
            ):
                error = await self._smart_read_error(page)
                if error:
                    raise Exception(f"Cannot add to cart: {error}")
                raise Exception("Add to cart button not found")
            await page.wait_for_timeout(1500)

            # Decline optional coverage/warranty if modal appears
            await self._smart_click(
                page, "No thanks / Decline coverage",
                'button[data-test="espModalContent-declineCoverageButton"], button:has-text("No thanks")',
                timeout=2000,
            )
            await page.wait_for_timeout(500)

            # Go to cart via modal button or direct navigation
            if not await self._smart_click(
                page, "View cart & check out",
                'button[data-test="addToCartModalViewCartCheckout"], a[href*="/cart"]',
                timeout=3000,
            ):
                await page.goto("https://www.target.com/cart", wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)

            # Click checkout
            if not await self._smart_click(
                page, "Check out",
                'button[data-test="checkout-button"], button:has-text("Check out"), a:has-text("Check out")',
            ):
                raise Exception("Checkout button not found")
            await page.wait_for_timeout(3000)

            # Place order — assumes saved payment on Target account
            if dry_run:
                # Verify the "Place your order" button is visible, but don't click it
                try:
                    btn = page.locator('button:has-text("Place your order")')
                    if await btn.first.is_visible(timeout=10000):
                        logger.info("DRY RUN: 'Place your order' button found — checkout flow verified")
                        await self._save_context(context, "target")
                        return CheckoutResult(
                            url=url,
                            retailer="target",
                            product_name=product_name,
                            status=CheckoutStatus.SUCCESS,
                            error_message="DRY RUN: stopped before placing order — full flow verified",
                        )
                except Exception:
                    pass
                error = await self._smart_read_error(page)
                await self._save_context(context, "target")
                return CheckoutResult(
                    url=url,
                    retailer="target",
                    product_name=product_name,
                    status=CheckoutStatus.FAILED,
                    error_message=error or "DRY RUN: 'Place your order' button not found",
                )

            if await self._smart_click(
                page, "Place your order",
                'button:has-text("Place your order")',
                timeout=10000,
            ):
                await page.wait_for_timeout(5000)
                await self._save_context(context, "target")
                return CheckoutResult(
                    url=url,
                    retailer="target",
                    product_name=product_name,
                    status=CheckoutStatus.SUCCESS,
                )

            error = await self._smart_read_error(page)
            await self._save_context(context, "target")
            return CheckoutResult(
                url=url,
                retailer="target",
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=error or "Could not find place order button - manual intervention needed",
            )

        except Exception as e:
            return CheckoutResult(
                url=url,
                retailer="target",
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=str(e),
            )
        finally:
            await page.close()
            await context.close()

    async def _is_signed_in_target(self, page) -> bool:
        try:
            account = page.locator('#account, [data-test="accountNav"]')
            text = await account.inner_text(timeout=3000)
            return "sign in" not in text.lower()
        except Exception:
            return False

    async def _sign_in_target(self, page, creds: AccountCredentials):
        await page.goto(
            "https://www.target.com/login?client_id=ecom-web-1.0.0&ui_namespace=ui-default&back_button_action=browser&keep_me_signed_in=true&kmsi_default=true&actions=create_session_request_username",
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(2000)

        email_sel = '#username, input[name="username"], input[type="email"], input[type="tel"], input[id*="username" i], input[name*="email" i], input[autocomplete="username"], input[autocomplete="email tel"]'
        pass_sel = '#password, input[name="password"], input[type="password"], input[id*="password" i]'

        # Step 1: Enter email/phone using keyboard.type() for human-like input
        await page.locator(email_sel).first.wait_for(state="visible", timeout=10000)
        await page.locator(email_sel).first.click()
        await page.locator(email_sel).first.press("Control+a")
        await page.keyboard.type(creds.email, delay=40)

        # Check if password is already visible (single-step) or multi-step
        pass_visible = False
        try:
            pass_visible = await page.locator(pass_sel).first.is_visible(timeout=1000)
        except Exception:
            pass

        if pass_visible:
            # Single-step: fill password and submit
            await page.locator(pass_sel).first.click()
            await page.keyboard.type(creds.password, delay=40)
            await self._multi_strategy_click(page, "Sign in", [
                "Sign in", "Continue", "Log in",
            ], 'button[type="submit"], button:has-text("Sign in")')
        else:
            # Step 2: Submit email — multi-strategy click for "Continue with email"
            await self._multi_strategy_click(page, "Continue with email", [
                "Continue with email", "Continue", "Sign in", "Next",
            ], 'button[type="submit"], button:has-text("Continue")')
            await page.wait_for_timeout(3000)

            # Step 3: Auth method picker — Target shows "Enter your password"
            pw_option_clicked = False

            # Strategy 1: get_by_role("button") for button-style pickers
            for option_text in ["Enter your password", "Enter password", "Password", "Use password"]:
                try:
                    opt = page.get_by_role("button", name=option_text, exact=False)
                    if await opt.first.is_visible(timeout=500):
                        await opt.first.click()
                        pw_option_clicked = True
                        break
                except Exception:
                    continue

            # Strategy 2: get_by_role("radio") for radio-button pickers (Walmart)
            if not pw_option_clicked:
                try:
                    opt = page.get_by_role("radio", name="Password", exact=False)
                    if await opt.first.is_visible(timeout=500):
                        await opt.first.click()
                        pw_option_clicked = True
                except Exception:
                    pass

            # Strategy 3: get_by_text (catches divs/links/labels acting as buttons)
            if not pw_option_clicked:
                for option_text in ["Enter your password", "Enter password", "Password"]:
                    try:
                        opt = page.get_by_text(option_text, exact=True)
                        if await opt.first.is_visible(timeout=500):
                            await opt.first.click()
                            pw_option_clicked = True
                            break
                    except Exception:
                        continue

            # Strategy 4: CSS selectors (labels for radio, buttons, links)
            if not pw_option_clicked:
                password_option = page.locator('button:has-text("password"), a:has-text("password"), [data-test*="password" i], div:has-text("Enter your password"), label:has-text("Password"), input[type="radio"][value*="password" i]')
                try:
                    if await password_option.first.is_visible(timeout=1000):
                        await password_option.first.click()
                        pw_option_clicked = True
                except Exception:
                    pass

            # Strategy 5: Vision fallback
            if not pw_option_clicked:
                logger.info("Sign-in: trying vision for auth method picker")
                pw_option_clicked = await self._smart_click(page, "Password option (radio button or link)", "", timeout=1000)

            if pw_option_clicked:
                await page.wait_for_timeout(2000)

            # Step 4: Enter password
            await page.locator(pass_sel).first.wait_for(state="visible", timeout=10000)
            await page.locator(pass_sel).first.click()
            await page.keyboard.type(creds.password, delay=40)
            await self._multi_strategy_click(page, "Sign in", [
                "Sign in", "Continue", "Log in",
            ], 'button[type="submit"], button:has-text("Sign in")')

        await page.wait_for_timeout(3000)

        # Verify we navigated away from login page (success heuristic)
        final_url = page.url
        if "/login" in final_url or "/signin" in final_url or "/identity" in final_url:
            logger.warning("Target sign-in may have failed — still on login page: %s", final_url)

    async def _checkout_walmart(
        self, url: str, product_name: str, profile: Profile, creds: AccountCredentials, dry_run: bool = False
    ) -> CheckoutResult:
        """Walmart checkout flow."""
        context = await self._get_context("walmart")
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            # Add to cart
            if not await self._smart_click(
                page, "Add to cart",
                'button[data-tl-id="ProductPrimaryCTA-cta_add_to_cart_button"], button[data-tl-id*="addToCart"], button:has-text("Add to cart")',
            ):
                error = await self._smart_read_error(page)
                if error:
                    raise Exception(f"Cannot add to cart: {error}")
                raise Exception("Add to cart button not found")
            await page.wait_for_timeout(2000)

            # Go to checkout via button or direct navigation
            if not await self._smart_click(
                page, "Check out",
                'button[data-tl-id="IPPacCheckOutBtnBottom"], button:has-text("Check out")',
                timeout=3000,
            ):
                await page.goto("https://www.walmart.com/checkout", wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)

            # Sign in if needed — try selectors first, then vision
            sign_in_visible = False
            try:
                sign_in_visible = await page.locator('button:has-text("Sign in"), a:has-text("Sign in")').first.is_visible(timeout=2000)
            except Exception:
                pass

            if sign_in_visible:
                email_sel = 'input[name="email"], input[type="email"], input[id*="email" i], input[type="tel"], input[name="phone"], input[id*="phone" i], #phone-number, input[autocomplete="tel"]'
                pass_sel = 'input[type="password"], input[name="password"], input[id*="password" i]'

                # Check if auth method picker is already showing (Walmart with pre-filled phone)
                auth_picker_visible = False
                try:
                    pw_radio = page.get_by_role("radio", name="Password", exact=False)
                    if await pw_radio.first.is_visible(timeout=1000):
                        auth_picker_visible = True
                        await pw_radio.first.click()
                        await page.wait_for_timeout(1000)
                except Exception:
                    pass

                if not auth_picker_visible:
                    # Standard flow: fill email/phone and submit
                    email_filled = await self._smart_fill(page, "email/phone", email_sel, creds.email)
                    if email_filled:
                        await self._multi_strategy_click(page, "Continue", [
                            "Continue", "Sign in", "Next",
                        ], 'button[type="submit"]')
                        await page.wait_for_timeout(3000)

                        # Check for auth method picker after submit
                        try:
                            pw_radio = page.get_by_role("radio", name="Password", exact=False)
                            if await pw_radio.first.is_visible(timeout=2000):
                                await pw_radio.first.click()
                                await page.wait_for_timeout(1000)
                        except Exception:
                            pass

                # Now enter password
                pass_filled = await self._smart_fill(page, "password", pass_sel, creds.password)
                if pass_filled:
                    await self._multi_strategy_click(page, "Sign in", [
                        "Sign in", "Log in", "Continue",
                    ], 'button[type="submit"]')
                    await page.wait_for_timeout(3000)
                else:
                    # Full vision-assisted sign-in
                    await self._smart_sign_in(page, creds, "walmart")

            # Guest checkout fallback if not signed in
            await self._smart_click(
                page, "Continue as guest",
                'button[data-tl-id="Wel-Guest_cxo_btn"], button:has-text("Continue without account"), button:has-text("Guest")',
                timeout=2000,
            )

            # Wait for user to handle captcha if present, then place order
            if dry_run:
                try:
                    btn = page.locator('button:has-text("Place order")')
                    if await btn.first.is_visible(timeout=15000):
                        logger.info("DRY RUN: 'Place order' button found — checkout flow verified")
                        await self._save_context(context, "walmart")
                        return CheckoutResult(
                            url=url,
                            retailer="walmart",
                            product_name=product_name,
                            status=CheckoutStatus.SUCCESS,
                            error_message="DRY RUN: stopped before placing order — full flow verified",
                        )
                except Exception:
                    pass
                error = await self._smart_read_error(page)
                await self._save_context(context, "walmart")
                return CheckoutResult(
                    url=url,
                    retailer="walmart",
                    product_name=product_name,
                    status=CheckoutStatus.FAILED,
                    error_message=error or "DRY RUN: 'Place order' button not found",
                )

            if await self._smart_click(
                page, "Place order",
                'button:has-text("Place order")',
                timeout=15000,
            ):
                await page.wait_for_timeout(5000)
                await self._save_context(context, "walmart")
                return CheckoutResult(
                    url=url,
                    retailer="walmart",
                    product_name=product_name,
                    status=CheckoutStatus.SUCCESS,
                )

            error = await self._smart_read_error(page)
            await self._save_context(context, "walmart")
            return CheckoutResult(
                url=url,
                retailer="walmart",
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=error or "Checkout flow interrupted - manual intervention needed",
            )
        except Exception as e:
            return CheckoutResult(
                url=url,
                retailer="walmart",
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=str(e),
            )
        finally:
            await page.close()
            await context.close()

    async def _checkout_pokemoncenter(
        self, url: str, product_name: str, profile: Profile, creds: AccountCredentials, dry_run: bool = False
    ) -> CheckoutResult:
        """Pokemon Center checkout flow."""
        context = await self._get_context("pokemoncenter")
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # PKC often has queue — wait for page to load
            # Add to cart
            if not await self._smart_click(
                page, "Add to Cart",
                'button:has-text("Add to Cart"), button:has-text("Add to Bag")',
                timeout=10000,
            ):
                error = await self._smart_read_error(page)
                if error:
                    raise Exception(f"Cannot add to cart: {error}")
                raise Exception("Add to cart button not found")
            await page.wait_for_timeout(2000)

            # Navigate to cart/checkout
            if not await self._smart_click(
                page, "Go to Cart",
                'a[href*="cart"], a:has-text("Cart"), a:has-text("Bag")',
            ):
                raise Exception("Cart link not found")
            await page.wait_for_timeout(2000)

            if not await self._smart_click(
                page, "Checkout",
                'button:has-text("Checkout"), a:has-text("Checkout")',
            ):
                raise Exception("Checkout button not found")
            await page.wait_for_timeout(3000)

            # Sign in if needed (may redirect to access.pokemon.com SSO)
            current_url = page.url
            if "access.pokemon.com" in current_url or "sso.pokemon.com" in current_url:
                email_sel = 'input[name="email"], input[name="username"], input[type="email"], input[type="text"]'
                pass_sel = 'input[type="password"], input[name="password"]'
            else:
                email_sel = 'input[type="email"], input[name="email"], input[type="text"][autocomplete="email"], input[type="text"][autocomplete="username"], input[id*="email" i], input[id*="login" i], input[name*="email" i], input[name*="login" i]'
                pass_sel = 'input[type="password"], input[name="password"], input[id*="password" i]'

            # Try selector-based sign-in, fall back to vision
            email_filled = await self._smart_fill(page, "email", email_sel, creds.email, timeout=5000)
            if email_filled:
                await self._smart_fill(page, "password", pass_sel, creds.password)
                await self._multi_strategy_click(page, "Sign In", [
                    "Sign In", "Log In", "Continue",
                ], 'button[type="submit"], button:has-text("Sign In")')
                await page.wait_for_timeout(5000)
            else:
                # No email field found by selectors — try full vision sign-in
                await self._smart_sign_in(page, creds, "pokemoncenter")

            # Place order — assumes saved payment on account
            if dry_run:
                try:
                    btn = page.locator('button:has-text("Place Order"), button:has-text("Submit Order")')
                    if await btn.first.is_visible(timeout=15000):
                        logger.info("DRY RUN: 'Place Order' button found — checkout flow verified")
                        await self._save_context(context, "pokemoncenter")
                        return CheckoutResult(
                            url=url,
                            retailer="pokemoncenter",
                            product_name=product_name,
                            status=CheckoutStatus.SUCCESS,
                            error_message="DRY RUN: stopped before placing order — full flow verified",
                        )
                except Exception:
                    pass
                error = await self._smart_read_error(page)
                await self._save_context(context, "pokemoncenter")
                return CheckoutResult(
                    url=url,
                    retailer="pokemoncenter",
                    product_name=product_name,
                    status=CheckoutStatus.FAILED,
                    error_message=error or "DRY RUN: 'Place Order' button not found",
                )

            if await self._smart_click(
                page, "Place Order",
                'button:has-text("Place Order"), button:has-text("Submit Order")',
                timeout=15000,
            ):
                await page.wait_for_timeout(5000)
                await self._save_context(context, "pokemoncenter")
                return CheckoutResult(
                    url=url,
                    retailer="pokemoncenter",
                    product_name=product_name,
                    status=CheckoutStatus.SUCCESS,
                )

            error = await self._smart_read_error(page)
            await self._save_context(context, "pokemoncenter")
            return CheckoutResult(
                url=url,
                retailer="pokemoncenter",
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=error or "Checkout flow interrupted - manual intervention needed",
            )
        except Exception as e:
            return CheckoutResult(
                url=url,
                retailer="pokemoncenter",
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=str(e),
            )
        finally:
            await page.close()
            await context.close()

    async def _checkout_bestbuy(
        self, url: str, product_name: str, profile: Profile, creds: AccountCredentials, dry_run: bool = False
    ) -> CheckoutResult:
        """Best Buy checkout flow (limited due to invitation system)."""
        context = await self._get_context("bestbuy")
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            # Check for invitation system — use vision as backup detector
            invite_text = await page.locator('text=/invitation/i').count()
            if invite_text > 0:
                return CheckoutResult(
                    url=url,
                    retailer="bestbuy",
                    product_name=product_name,
                    status=CheckoutStatus.FAILED,
                    error_message="Product uses Best Buy invitation system - auto-checkout not possible",
                )

            # Standard add to cart
            if not await self._smart_click(
                page, "Add to Cart",
                'button.add-to-cart-button:not([disabled]), button.btn-primary.add-to-cart-button',
            ):
                error = await self._smart_read_error(page)
                if error and "invitation" in error.lower():
                    return CheckoutResult(
                        url=url, retailer="bestbuy", product_name=product_name,
                        status=CheckoutStatus.FAILED,
                        error_message="Product uses Best Buy invitation system - auto-checkout not possible",
                    )
                if error:
                    raise Exception(f"Cannot add to cart: {error}")
                raise Exception("Add to cart button not found")
            await page.wait_for_timeout(2000)

            # Go to cart via popup button or direct navigation
            if not await self._smart_click(
                page, "Go to Cart",
                'div.go-to-cart-button a, a:has-text("Go to Cart"), a[href*="/cart"]',
                timeout=3000,
            ):
                await page.goto("https://www.bestbuy.com/cart", wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)

            # Click checkout
            if not await self._smart_click(
                page, "Checkout",
                'button[data-track="Checkout - Top"], button:has-text("Checkout"), a:has-text("Checkout")',
            ):
                raise Exception("Checkout button not found")
            await page.wait_for_timeout(3000)

            # Sign in if needed — selectors first, vision fallback
            email_filled = await self._smart_fill(
                page, "email", 'input#fld-e, input[id="user.emailAddress"], input[type="email"], input[name="email"]', creds.email, timeout=3000,
            )
            if email_filled:
                await self._smart_fill(page, "password", 'input#fld-p1, input[type="password"], input[name="password"]', creds.password)
                await self._multi_strategy_click(page, "Sign In", [
                    "Sign In", "Log In", "Continue",
                ], 'button[type="submit"], button:has-text("Sign In")')
                await page.wait_for_timeout(3000)
            else:
                # Try full vision-assisted sign-in
                await self._smart_sign_in(page, creds, "bestbuy")

            # Guest checkout fallback
            await self._smart_click(
                page, "Continue as Guest",
                'button.cia-guest-content__continue.guest, button:has-text("Continue as Guest"), button:has-text("Guest")',
                timeout=2000,
            )

            if dry_run:
                try:
                    btn = page.locator('button:has-text("Place Your Order"), button:has-text("Place Order")')
                    if await btn.first.is_visible(timeout=15000):
                        logger.info("DRY RUN: 'Place Your Order' button found — checkout flow verified")
                        await self._save_context(context, "bestbuy")
                        return CheckoutResult(
                            url=url,
                            retailer="bestbuy",
                            product_name=product_name,
                            status=CheckoutStatus.SUCCESS,
                            error_message="DRY RUN: stopped before placing order — full flow verified",
                        )
                except Exception:
                    pass
                error = await self._smart_read_error(page)
                await self._save_context(context, "bestbuy")
                return CheckoutResult(
                    url=url,
                    retailer="bestbuy",
                    product_name=product_name,
                    status=CheckoutStatus.FAILED,
                    error_message=error or "DRY RUN: 'Place Your Order' button not found",
                )

            if await self._smart_click(
                page, "Place Your Order",
                'button:has-text("Place Your Order"), button:has-text("Place Order")',
                timeout=15000,
            ):
                await page.wait_for_timeout(5000)
                await self._save_context(context, "bestbuy")
                return CheckoutResult(
                    url=url,
                    retailer="bestbuy",
                    product_name=product_name,
                    status=CheckoutStatus.SUCCESS,
                )

            error = await self._smart_read_error(page)
            await self._save_context(context, "bestbuy")
            return CheckoutResult(
                url=url,
                retailer="bestbuy",
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=error or "Checkout flow interrupted - manual intervention needed",
            )
        except Exception as e:
            return CheckoutResult(
                url=url,
                retailer="bestbuy",
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=str(e),
            )
        finally:
            await page.close()
            await context.close()

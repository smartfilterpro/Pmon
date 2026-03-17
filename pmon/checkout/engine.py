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

# Stealth JS to inject into every page to reduce bot detection.
# This patches the most common signals that PerimeterX/DataDome look for.
STEALTH_JS = """
// Remove webdriver flag (Playwright sets this by default)
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
delete navigator.__proto__.webdriver;

// Languages & plugins must look realistic
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        // Return realistic plugin array (Chrome PDF plugins)
        const plugins = [
            {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'},
            {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
            {name: 'Native Client', filename: 'internal-nacl-plugin'},
        ];
        plugins.refresh = () => {};
        return plugins;
    }
});

// Chrome object must exist and look real
window.chrome = {
    runtime: {
        onMessage: {addListener: () => {}, removeListener: () => {}},
        sendMessage: () => {},
        connect: () => ({onMessage: {addListener: () => {}}, postMessage: () => {}}),
    },
    loadTimes: () => ({}),
    csi: () => ({}),
};

// Permissions API patch
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : originalQuery(parameters);

// Prevent detection via iframe contentWindow checks
const origGetOwnPropertyDescriptor = Object.getOwnPropertyDescriptor;
Object.getOwnPropertyDescriptor = function(obj, prop) {
    if (prop === 'webdriver') return undefined;
    return origGetOwnPropertyDescriptor(obj, prop);
};
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

    def _load_user_sessions(self, retailer: str, user_id: int | None = None):
        """Load stored session cookies from database for API checkout."""
        if user_id is None:
            return
        try:
            from pmon import database as db
            import json
            session = db.get_retailer_session(user_id, retailer)
            if session and session.get("cookies_json"):
                cookies = json.loads(session["cookies_json"])
                if cookies:
                    self._api.load_session_cookies(retailer, cookies)
                    self._api.reset_client(retailer)
                    logger.info("Loaded %d stored session cookies for %s", len(cookies), retailer)
        except Exception as exc:
            logger.debug("Failed to load session cookies for %s: %s", retailer, exc)

    def _load_user_credentials(
        self, retailer: str, user_id: int | None = None
    ) -> AccountCredentials | None:
        """Load credentials from database (preferred) or config.yaml (fallback).

        The dashboard stores credentials in the DB per-user.  Config.yaml is
        only used as a legacy fallback for single-user / CLI mode.
        """
        if user_id is not None:
            try:
                from pmon import database as db
                accounts = db.get_retailer_accounts(user_id)
                acct = accounts.get(retailer)
                if acct and acct.get("email"):
                    logger.debug("Loaded %s credentials from database for user %d", retailer, user_id)
                    return AccountCredentials(
                        email=acct["email"],
                        password=acct.get("password", ""),
                        card_cvv=acct.get("card_cvv", ""),
                    )
            except Exception as exc:
                logger.debug("Failed to load DB credentials for %s: %s", retailer, exc)

        # Fallback: config.yaml (single-user / CLI mode)
        return self.config.accounts.get(retailer)

    async def attempt_checkout(
        self,
        url: str,
        retailer: str,
        product_name: str,
        profile_name: str = "default",
        dry_run: bool = False,
        user_id: int | None = None,
    ) -> CheckoutResult:
        """Attempt checkout: API first, browser fallback.

        If dry_run=True, runs the full checkout flow but stops right before
        clicking "Place order". Useful for testing the entire flow without
        actually purchasing.
        """
        profile = self.config.profiles.get(profile_name) or Profile()

        # Load credentials from DB (dashboard) first, then fall back to config.yaml
        creds = self._load_user_credentials(retailer, user_id)

        # Load stored session cookies for API checkout
        self._load_user_sessions(retailer, user_id)

        if not creds:
            return CheckoutResult(
                url=url,
                retailer=retailer,
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=f"No credentials for {retailer}. Add them via Dashboard > Settings > Accounts.",
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
        from pmon.monitors.base import _CHROME_FULL, _CHROME_MAJOR

        storage_path = SESSION_DIR / f"{retailer}.json"
        ctx_kwargs = dict(
            user_agent=(
                f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{_CHROME_FULL} Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Sec-Ch-Ua": f'"Chromium";v="{_CHROME_MAJOR}", "Google Chrome";v="{_CHROME_MAJOR}", "Not-A.Brand";v="24"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
            },
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

            # Dismiss privacy/cookie overlay if present
            await self._dismiss_target_overlay(page)

            # Dismiss Health Data Consent modal if present (health-related products)
            await self._dismiss_health_consent_modal(page)

            # Check if we need to sign in
            if not await self._is_signed_in_target(page):
                await self._sign_in_target(page, creds)
                # Vision fallback if selector-based sign-in didn't work
                if not await self._is_signed_in_target(page):
                    await page.goto(url, wait_until="domcontentloaded")
                    await page.wait_for_timeout(2000)

            # Try to add to cart — prefer "Ship it" (sets delivery method immediately)
            add_to_cart_clicked = await self._smart_click(
                page, "Ship it / Add to cart",
                'button[data-test="shipItButton"], button[data-test="shippingButton"], '
                'button:has-text("Ship it"), button:has-text("Add to cart")',
            )
            if not add_to_cart_clicked:
                # Health Data Consent modal may have appeared and blocked the click
                if await self._dismiss_health_consent_modal(page):
                    # Retry add-to-cart after dismissing the consent modal
                    add_to_cart_clicked = await self._smart_click(
                        page, "Ship it / Add to cart",
                        'button[data-test="shipItButton"], button[data-test="shippingButton"], '
                        'button:has-text("Ship it"), button:has-text("Add to cart")',
                    )
                if not add_to_cart_clicked:
                    error = await self._smart_read_error(page)
                    if error:
                        raise Exception(f"Cannot add to cart: {error}")
                    raise Exception("Add to cart button not found")
            await page.wait_for_timeout(1500)

            # Health Data Consent modal can also appear AFTER clicking add-to-cart
            await self._dismiss_health_consent_modal(page)

            # Decline optional coverage/warranty if modal appears
            await self._smart_click(
                page, "No thanks / Decline coverage",
                'button[data-test="espModalContent-declineCoverageButton"], button:has-text("No thanks"), '
                'button:has-text("No, thanks")',
                timeout=2000,
            )
            await page.wait_for_timeout(500)

            # Go to cart via modal button or direct navigation
            if not await self._smart_click(
                page, "View cart & check out",
                'button[data-test="addToCartModalViewCartCheckout"], a[href*="/cart"], '
                'button:has-text("View cart"), button:has-text("View cart & check out")',
                timeout=3000,
            ):
                await page.goto("https://www.target.com/cart", wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)

            # --- Handle delivery method selection on cart page ---
            # Target requires choosing "Shipping" or "Pickup" for each item.
            # If there's a "Choose delivery method" prompt, select shipping.
            await self._target_select_delivery(page)

            # Click checkout
            if not await self._smart_click(
                page, "Check out",
                'button[data-test="checkout-button"], button:has-text("Check out"), '
                'a:has-text("Check out"), button[data-test="checkout-btn"]',
            ):
                raise Exception("Checkout button not found")
            await page.wait_for_timeout(5000)

            # --- Handle checkout page steps ---
            # Target's checkout can have multiple pages: delivery, payment, review.
            # We need to navigate through them to reach "Place your order".
            await self._target_navigate_checkout(page, creds)

            # Place order — assumes saved payment on Target account
            place_order_sel = (
                'button:has-text("Place your order"), '
                'button[data-test="placeOrderButton"], '
                'button:has-text("Place order")'
            )

            if dry_run:
                # Verify the "Place your order" button is visible, but don't click it
                try:
                    btn = page.locator(place_order_sel)
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
                place_order_sel,
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

    async def _dismiss_health_consent_modal(self, page) -> bool:
        """Dismiss Target's Health Data Consent modal if present.

        Target shows this modal for health-related products (supplements,
        vitamins, health monitors, etc.), requiring users to agree to Terms
        and Health Privacy Policy before adding to cart.  Returns True if a
        modal was dismissed.
        """
        try:
            # Look for the modal by role or common selectors
            consent_selectors = [
                # Dialog-level selectors
                '[data-test="health-consent-modal"] button:has-text("Agree")',
                '[data-test="healthConsentModal"] button:has-text("Agree")',
                # Generic modal with health-related text + agree/acknowledge button
                'dialog button:has-text("I agree")',
                'dialog button:has-text("Agree")',
                'dialog button:has-text("Acknowledge")',
                'dialog button:has-text("Accept")',
                'dialog button:has-text("Continue")',
                # Div-based modals (Target uses both <dialog> and div overlays)
                '[role="dialog"] button:has-text("I agree")',
                '[role="dialog"] button:has-text("Agree")',
                '[role="dialog"] button:has-text("Acknowledge")',
                '[role="dialog"] button:has-text("Accept")',
                '[role="dialog"] button:has-text("Continue")',
                # Broader: any visible button with agree/acknowledge text
                'button:has-text("I agree")',
                'button:has-text("Agree and continue")',
            ]
            for sel in consent_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=500):
                        await btn.click(timeout=3000)
                        logger.info("Health consent modal: dismissed via '%s'", sel)
                        await page.wait_for_timeout(1000)
                        return True
                except Exception:
                    continue

            # Vision fallback: ask Claude to find and click the agree button
            if self._vision_available:
                clicked = await self._smart_click(
                    page,
                    "Health Data Consent agree/acknowledge button",
                    "",
                    timeout=2000,
                )
                if clicked:
                    logger.info("Health consent modal: dismissed via vision fallback")
                    await page.wait_for_timeout(1000)
                    return True

            return False
        except Exception as exc:
            logger.debug("Health consent modal dismiss failed (non-fatal): %s", exc)
            return False

    async def _dismiss_target_overlay(self, page):
        """Dismiss Target's privacy/cookie consent overlay that blocks clicks."""
        try:
            # Try clicking common accept/close buttons inside the floating-ui portal overlay
            for sel in [
                '[data-floating-ui-portal] button:has-text("Accept")',
                '[data-floating-ui-portal] button:has-text("accept")',
                '[data-floating-ui-portal] button:has-text("Close")',
                '[data-floating-ui-portal] button:has-text("Got it")',
                '[data-floating-ui-portal] button:has-text("OK")',
                '.styles_overlay__AJMdo + div button',
                '#onetrust-accept-btn-handler',
                'button[id*="accept" i]',
                'button[id*="cookie" i]',
            ]:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=500):
                        await btn.click(timeout=2000)
                        logger.info("Target overlay: dismissed via button '%s'", sel)
                        await page.wait_for_timeout(500)
                        return
                except Exception:
                    continue

            # Fallback: forcibly remove the overlay and portal via JS
            removed = await page.evaluate("""() => {
                let removed = 0;
                // Remove floating-ui portal overlays
                document.querySelectorAll('[data-floating-ui-portal]').forEach(el => {
                    el.remove();
                    removed++;
                });
                // Remove any remaining overlay divs that block pointer events
                document.querySelectorAll('[class*="overlay"]').forEach(el => {
                    const style = window.getComputedStyle(el);
                    if (style.position === 'fixed' || style.position === 'absolute') {
                        el.remove();
                        removed++;
                    }
                });
                // Also remove any inert attributes that may have been set
                document.querySelectorAll('[data-floating-ui-inert]').forEach(el => {
                    el.removeAttribute('data-floating-ui-inert');
                    el.removeAttribute('aria-hidden');
                });
                return removed;
            }""")
            if removed:
                logger.info("Target overlay: removed %d blocking element(s) via JS", removed)
                await page.wait_for_timeout(500)
            else:
                logger.debug("Target overlay: no overlay detected")
        except Exception as exc:
            logger.debug("Target overlay dismiss failed (non-fatal): %s", exc)

    async def _target_select_delivery(self, page):
        """Select shipping/delivery method on Target cart page.

        Target shows "Choose a delivery method" for each item in the cart.
        We need to click "Shipping" (or the shipping radio/button) so the
        checkout button becomes active.
        """
        try:
            # Check if delivery method selection is needed
            needs_delivery = False
            for indicator in [
                'text="Choose a delivery method"',
                'text="Choose delivery method"',
                '[data-test="fulfillment-cell"]',
            ]:
                try:
                    loc = page.locator(indicator)
                    if await loc.first.is_visible(timeout=2000):
                        needs_delivery = True
                        break
                except Exception:
                    continue

            if not needs_delivery:
                logger.debug("Target cart: delivery method already selected or not needed")
                return

            logger.info("Target cart: selecting delivery method (Shipping)")

            # Try clicking shipping option — Target uses various UI patterns:
            # 1. Radio button with "Shipping" / "Ship" label
            # 2. Button/link with "Shipping" text
            # 3. Fulfillment cell with shipping icon
            shipping_clicked = False
            shipping_selectors = [
                'button[data-test="fulfillmentOptionShipping"]',
                'button:has-text("Shipping")',
                'button:has-text("Ship")',
                '[data-test="shipping-option"]',
                'label:has-text("Shipping")',
                'div[data-test="fulfillment-cell"] button:first-child',
                'input[type="radio"][value*="SHIP" i]',
                'button:has-text("Standard")',
            ]
            for sel in shipping_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=1000):
                        await loc.click()
                        shipping_clicked = True
                        logger.info("Target cart: clicked shipping via %s", sel)
                        break
                except Exception:
                    continue

            if not shipping_clicked:
                # Vision fallback
                shipping_clicked = await self._smart_click(
                    page, "Shipping delivery option",
                    "",
                    timeout=2000,
                )

            if shipping_clicked:
                await page.wait_for_timeout(2000)

                # Target sometimes shows a "Save" or "Apply" button after selecting delivery
                for save_sel in [
                    'button:has-text("Save")',
                    'button:has-text("Apply")',
                    'button:has-text("Update")',
                ]:
                    try:
                        loc = page.locator(save_sel).first
                        if await loc.is_visible(timeout=1000):
                            await loc.click()
                            await page.wait_for_timeout(1000)
                            break
                    except Exception:
                        continue
            else:
                logger.warning("Target cart: could not select delivery method — checkout may fail")

        except Exception as exc:
            logger.debug("Target delivery selection error (non-fatal): %s", exc)

    async def _target_navigate_checkout(self, page, creds: AccountCredentials | None = None):
        """Navigate through Target's multi-step checkout pages.

        Target's checkout may have these steps:
        1. Delivery address (if not saved)
        2. Delivery method / shipping speed
        3. Payment — CVV entry required even for saved cards
        4. Order review with "Place your order" button

        We try to click "Continue" / "Save and continue" through each step
        until we reach the final review page.
        """
        continue_sel = (
            'button[data-test="save-and-continue-button"], '
            'button:has-text("Save and continue"), '
            'button:has-text("Continue"), '
            'button:has-text("Save & continue")'
        )

        # Click "Continue" / "Save and continue" up to 5 times to progress through steps
        for step in range(5):
            # Check if "Place your order" is already visible — we're done
            try:
                place_btn = page.locator('button:has-text("Place your order"), button:has-text("Place order")')
                if await place_btn.first.is_visible(timeout=2000):
                    logger.info("Target checkout: reached order review page (step %d)", step)
                    return
            except Exception:
                pass

            # Check for CVV input field — Target requires CVV even for saved cards
            try:
                cvv_selectors = [
                    'input[data-test="verify-card-cvv"]',
                    'input[name="cvv"]',
                    'input[name="cardCvc"]',
                    'input[id*="cvv" i]',
                    'input[id*="cvc" i]',
                    'input[placeholder*="CVV" i]',
                    'input[placeholder*="CVC" i]',
                    'input[aria-label*="CVV" i]',
                    'input[aria-label*="security code" i]',
                    'input[autocomplete="cc-csc"]',
                ]
                cvv_filled = False
                for cvv_sel in cvv_selectors:
                    try:
                        cvv_input = page.locator(cvv_sel).first
                        if await cvv_input.is_visible(timeout=500):
                            if creds and creds.card_cvv:
                                await cvv_input.click(force=True)
                                await page.wait_for_timeout(200)
                                await cvv_input.fill(creds.card_cvv)
                                logger.info("Target checkout: entered CVV via %s", cvv_sel)
                                cvv_filled = True
                                await page.wait_for_timeout(500)
                            else:
                                logger.warning("Target checkout: CVV field found but no CVV configured! "
                                             "Add CVV via Dashboard > Settings > Accounts > Target > Edit")
                            break
                    except Exception:
                        continue

                if cvv_filled:
                    # After filling CVV, click save/continue
                    await page.wait_for_timeout(500)
            except Exception as exc:
                logger.debug("Target checkout: CVV check error (non-fatal): %s", exc)

            # Try clicking continue/save
            try:
                continue_btn = page.locator(continue_sel).first
                if await continue_btn.is_visible(timeout=3000):
                    await continue_btn.click()
                    logger.info("Target checkout: clicked continue (step %d)", step + 1)
                    await page.wait_for_timeout(3000)
                else:
                    # No continue button and no place order button — try vision
                    clicked = await self._smart_click(
                        page, "Continue or Save and continue button",
                        continue_sel,
                        timeout=2000,
                    )
                    if not clicked:
                        logger.info("Target checkout: no more continue buttons found (step %d)", step)
                        break
                    await page.wait_for_timeout(3000)
            except Exception:
                break

    async def _sign_in_target(self, page, creds: AccountCredentials):
        login_url = "https://www.target.com/login?client_id=ecom-web-1.0.0&ui_namespace=ui-default&back_button_action=browser&keep_me_signed_in=true&kmsi_default=true&actions=create_session_request_username"

        email_sel = '#username, input[name="username"], input[type="email"], input[type="tel"], input[id*="username" i], input[name*="email" i], input[autocomplete="username"], input[autocomplete="email tel"]'
        pass_sel = '#password, input[name="password"], input[type="password"], input[id*="password" i]'

        # Target's login is a React SPA — domcontentloaded fires before React
        # renders the form.  Use "load" and then poll for the email field,
        # retrying with a full page reload if the form never appears.
        email_input = None
        for attempt in range(3):
            if attempt == 0:
                await page.goto(login_url, wait_until="load")
            else:
                logger.warning("Target login: form not rendered (attempt %d/3) — reloading", attempt)
                await page.reload(wait_until="load")

            await self._dismiss_target_overlay(page)

            # Poll for the email field to appear (React hydration)
            try:
                email_input = page.locator(email_sel).first
                await email_input.wait_for(state="visible", timeout=15000)
                break  # form rendered
            except Exception:
                email_input = None

        if email_input is None:
            raise RuntimeError("Target login page did not render — email field not found after 3 attempts")

        # Step 1: Enter email/phone

        # Try keyboard.type() first (human-like), verify it worked, fall back to fill()
        await email_input.click(force=True)
        await page.wait_for_timeout(300)
        await email_input.press("Control+a")
        await page.keyboard.type(creds.email, delay=40)
        await page.wait_for_timeout(500)

        # Verify the value actually got entered — Target's JS can clear it
        try:
            actual_value = await email_input.input_value(timeout=1000)
            if actual_value != creds.email:
                logger.warning("Target sign-in: keyboard.type() produced '%s', expected '%s' — using fill()",
                              actual_value[:20], creds.email[:20])
                await email_input.fill(creds.email)
                await page.wait_for_timeout(300)
        except Exception:
            # If we can't read the value, try fill() as backup
            logger.warning("Target sign-in: could not verify email input — using fill() as backup")
            await email_input.fill(creds.email)
            await page.wait_for_timeout(300)

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

            # Check if we navigated away from login after email submit
            # (e.g. Target may redirect to homepage if already authenticated)
            post_email_url = page.url
            login_indicators = ["/login", "/signin", "/sign-in", "/identity"]
            if not any(ind in post_email_url.lower() for ind in login_indicators):
                logger.info("Target sign-in: navigated away from login after email submit (%s) — may already be signed in", post_email_url)
                return  # Let the caller check success via URL/cookies

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

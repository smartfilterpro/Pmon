"""Checkout engine: API-first with optional Playwright browser fallback.

AUDIT FINDINGS (2026-03-17):
=============================================================================
This file contains the browser-based checkout engine with Playwright. It
currently implements a Target checkout flow (_checkout_target) that is BROKEN
due to Target site changes. Key issues identified:

1. NO POPUP HANDLER: The bot has no universal popup/modal handler. Target now
   shows new interstitials (cookie consent, "sign in for deals" modals,
   age gates, delivery method pickers, "Choose a store" sheets) that block
   the checkout flow. The _dismiss_target_overlay() method only handles
   cookie/privacy overlays via a fixed list of selectors — it does NOT
   detect or dismiss arbitrary modals, dialogs, or interstitials.

2. LINEAR FLOW WITH NO RECOVERY: The _checkout_target() method runs steps
   sequentially with no retry logic. If any step fails (e.g. a popup blocks
   "Add to cart"), the entire checkout aborts. There is no mechanism to
   detect that a step failed due to an unexpected UI element vs. a real error.

3. HARDCODED SELECTORS: Selectors like 'button[data-test="shipItButton"]' and
   'button[data-test="addToCartModalViewCartCheckout"]' are brittle. Target
   frequently renames data-test attributes. The _smart_click() vision fallback
   helps but is only used after CSS selectors fail — it doesn't proactively
   sweep for blockers BEFORE attempting clicks.

4. NO PRICE GUARD: The bot will place an order at ANY price. There is no
   max_price check before clicking "Place your order". A price spike or
   wrong product could result in an unintended expensive purchase.

5. SIGN-IN FLOW FRAGILE: _sign_in_target() handles the multi-step Target
   login but has no recovery if the login page doesn't render (after 3
   retries it raises RuntimeError and aborts). The login flow also doesn't
   handle CAPTCHAs or 2FA prompts from Target.

6. STEALTH JS IS GOOD: The STEALTH_JS injection is comprehensive (webdriver
   flag removal, WebGL spoofing, canvas noise, chrome object emulation).
   This should be preserved in the rewrite.

7. SESSION PERSISTENCE WORKS: _get_context()/_save_context() correctly use
   Playwright's storage_state for cookie persistence. This pattern is sound.

8. VISION HELPERS ARE USEFUL: _smart_click(), _smart_fill(), _ask_vision()
   provide a good foundation for Claude-assisted fallback. However, they
   return coordinates for clicking — which can be fragile if the viewport
   or page layout shifts. The rewrite should use these as a last resort
   after the new PopupHandler has swept for blockers.

9. NO SCREENSHOTS FOR DEBUGGING: Failed steps don't save screenshots. The
   only screenshot usage is for vision API calls. Every major step should
   save a timestamped screenshot for post-mortem debugging.

10. MISSING max_price CONFIG: The Config/Profile dataclasses have no
    max_price field. This needs to be added to enable price guards.
=============================================================================
"""

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
from pmon.checkout.human_behavior import (
    human_click,
    human_click_element,
    human_mouse_move,
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

logger = logging.getLogger(__name__)

# Directory to store browser session data (cookies, etc.)
SESSION_DIR = Path(__file__).parent.parent.parent / ".sessions"

# Stealth JS to inject into every page to reduce bot detection.
# This patches the most common signals that PerimeterX/DataDome look for.
STEALTH_JS = """
// --- webdriver flag removal (multiple vectors) ---
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
delete navigator.__proto__.webdriver;

// Prevent detection via getOwnPropertyDescriptor
const origGetOwnPropertyDescriptor = Object.getOwnPropertyDescriptor;
Object.getOwnPropertyDescriptor = function(obj, prop) {
    if (prop === 'webdriver') return undefined;
    return origGetOwnPropertyDescriptor(obj, prop);
};

// --- Languages & plugins ---
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const plugins = [
            {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'},
            {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
            {name: 'Native Client', filename: 'internal-nacl-plugin'},
        ];
        plugins.refresh = () => {};
        Object.defineProperty(plugins, 'length', {get: () => 3});
        return plugins;
    }
});

// --- Hardware concurrency & device memory (headless defaults are suspicious) ---
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 0});
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
Object.defineProperty(navigator, 'vendor', {get: () => 'Google Inc.'});

// --- Chrome object (must exist and look realistic) ---
window.chrome = {
    app: {isInstalled: false, InstallState: {DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed'}, RunningState: {CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running'}},
    runtime: {
        onMessage: {addListener: () => {}, removeListener: () => {}, hasListeners: () => false},
        sendMessage: () => {},
        connect: () => ({onMessage: {addListener: () => {}}, postMessage: () => {}, disconnect: () => {}}),
        PlatformOs: {MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd'},
        PlatformArch: {ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64'},
        PlatformNaclArch: {ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64'},
        RequestUpdateCheckStatus: {THROTTLED: 'throttled', NO_UPDATE: 'no_update', UPDATE_AVAILABLE: 'update_available'},
    },
    loadTimes: () => ({requestTime: Date.now() / 1000, startLoadTime: Date.now() / 1000, firstPaintTime: Date.now() / 1000 + 0.1, firstPaintAfterLoadTime: 0, finishDocumentLoadTime: Date.now() / 1000 + 0.2, finishLoadTime: Date.now() / 1000 + 0.3, navigationType: 'Other'}),
    csi: () => ({startE: Date.now(), onloadT: Date.now() + 200}),
};

// --- Permissions API patch ---
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : originalQuery(parameters);

// --- WebGL vendor/renderer (headless Chrome shows "Google SwiftShader") ---
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(param) {
    if (param === 37445) return 'Google Inc. (NVIDIA)';       // UNMASKED_VENDOR_WEBGL
    if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)'; // UNMASKED_RENDERER_WEBGL
    return getParameter.call(this, param);
};
const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
WebGL2RenderingContext.prototype.getParameter = function(param) {
    if (param === 37445) return 'Google Inc. (NVIDIA)';
    if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)';
    return getParameter2.call(this, param);
};

// --- Canvas fingerprint noise (prevent exact canvas hash matching) ---
const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function(type) {
    if (this.width === 0 && this.height === 0) return origToDataURL.call(this, type);
    const ctx = this.getContext('2d');
    if (ctx) {
        const imageData = ctx.getImageData(0, 0, Math.min(this.width, 2), Math.min(this.height, 2));
        // Tiny noise to a few pixels — changes fingerprint hash without visible effect
        for (let i = 0; i < imageData.data.length && i < 12; i += 4) {
            imageData.data[i] = imageData.data[i] ^ 1;
        }
        ctx.putImageData(imageData, 0, 0);
    }
    return origToDataURL.call(this, type);
};

// --- Prevent Notification.permission detection in iframes ---
try {
    if (Notification.permission === 'default') {
        Object.defineProperty(Notification, 'permission', {get: () => 'default'});
    }
} catch(e) {}

// --- Screen dimensions (headless often has weird values) ---
Object.defineProperty(screen, 'colorDepth', {get: () => 24});
Object.defineProperty(screen, 'pixelDepth', {get: () => 24});

// --- Connection API (headless may not have it) ---
if (!navigator.connection) {
    Object.defineProperty(navigator, 'connection', {
        get: () => ({effectiveType: '4g', rtt: 50, downlink: 10, saveData: false}),
    });
}
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

        Uses human-like mouse movement and click behavior throughout.
        Returns True if click succeeded, False otherwise.
        """
        # Fast path: selectors with human-like click
        try:
            elem = page.locator(selectors)
            if await human_click_element(page, elem, timeout=timeout):
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
                await human_click(page, int(coords["x"]), int(coords["y"]))
                return True
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning(f"Vision click parse error for '{description}': {e}")
        return False

    async def _smart_fill(self, page, description: str, selectors: str, value: str, timeout: int = 5000) -> bool:
        """Try CSS selectors first; fall back to Claude vision to find and fill an input.

        Uses human-like typing throughout.
        Returns True if fill succeeded, False otherwise.
        """
        # Fast path: selectors
        try:
            elem = page.locator(selectors)
            await elem.first.wait_for(state="visible", timeout=timeout)
            await human_click_element(page, elem)
            await random_delay(page, 100, 250)
            await human_type(page, value)
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
                await human_click(page, int(coords["x"]), int(coords["y"]))
                await random_delay(page, 100, 250)
                await human_type(page, value)
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
                    await human_click(page, x, y)
                    await random_delay(page, 100, 250)
                    await human_type(page, val)
                elif action["type"] == "click":
                    await human_click(page, x, y)
                await random_delay(page, 800, 1500)

            await wait_for_page_ready(page, timeout=5000)
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
        """Multi-strategy button click: CSS → get_by_role → get_by_text → vision.

        Uses human-like click behavior throughout.
        """
        # Strategy 1: CSS selectors (fast path)
        if css_fallback:
            try:
                elem = page.locator(css_fallback)
                if await elem.first.is_visible(timeout=min(timeout, 1000)):
                    await human_click_element(page, elem)
                    return True
            except Exception:
                pass

        # Strategy 2: get_by_role with each text variation
        for btn_text in button_texts:
            try:
                btn = page.get_by_role("button", name=btn_text, exact=False)
                if await btn.first.is_visible(timeout=500):
                    await human_click_element(page, btn)
                    return True
            except Exception:
                continue

        # Strategy 3: get_by_text (catches links/divs acting as buttons)
        for btn_text in button_texts:
            try:
                link = page.get_by_text(btn_text, exact=False)
                if await link.first.is_visible(timeout=500):
                    await human_click_element(page, link)
                    return True
            except Exception:
                continue

        # Strategy 4: Vision fallback (already uses human_click internally)
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
                    "--disable-features=VizDisplayCompositor",
                    "--disable-infobars",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-default-apps",
                    "--disable-extensions",
                    "--no-first-run",
                    "--use-gl=angle",
                    "--use-angle=d3d11",
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
            screen={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
            color_scheme="light",
            extra_http_headers={
                "Sec-Ch-Ua": f'"Chromium";v="{_CHROME_MAJOR}", "Google Chrome";v="{_CHROME_MAJOR}", "Not?A_Brand";v="24"',
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
        """Target checkout flow.

        AUDIT: This is the method that broke. Root causes:
        - No popup sweep between steps (new Target modals go unhandled)
        - No retry on individual steps (one failure = total abort)
        - No price validation before placing order
        - No screenshot logging for debugging failed runs
        - Delivery method selection (_target_select_delivery) uses fixed
          selectors that Target has changed
        - The "Add to cart" confirmation modal handling is incomplete —
          Target now sometimes shows "Choose a store" or "Sign in to save"
          modals after the add-to-cart click that are not dismissed
        """
        context = await self._get_context("target")
        page = await context.new_page()

        try:
            # Navigate to product page
            await page.goto(url, wait_until="domcontentloaded")
            await wait_for_page_ready(page, timeout=15000)

            # Sweep all popups/overlays (cookie consent, promos, etc.)
            await sweep_popups(page)

            # Human-like: glance at the page before interacting
            await idle_scroll(page)
            await random_delay(page, 500, 1500)

            # Check if we need to sign in
            if not await self._is_signed_in_target(page):
                await self._sign_in_target(page, creds)
                # Vision fallback if selector-based sign-in didn't work
                if not await self._is_signed_in_target(page):
                    await page.goto(url, wait_until="domcontentloaded")
                    await wait_for_page_ready(page, timeout=15000)

            # Sweep popups again after sign-in (welcome back, promos)
            await sweep_popups(page)

            # Try to add to cart — prefer "Ship it" (sets delivery method immediately)
            add_to_cart_sel = (
                'button[data-test="shipItButton"], button[data-test="shippingButton"], '
                'button:has-text("Ship it"), button:has-text("Add to cart")'
            )
            add_to_cart_clicked = await self._smart_click(page, "Ship it / Add to cart", add_to_cart_sel)
            if not add_to_cart_clicked:
                # Popup may have blocked the click — sweep and retry
                if await sweep_popups(page):
                    add_to_cart_clicked = await self._smart_click(page, "Ship it / Add to cart", add_to_cart_sel)
                if not add_to_cart_clicked:
                    error = await self._smart_read_error(page)
                    if error:
                        raise Exception(f"Cannot add to cart: {error}")
                    raise Exception("Add to cart button not found")
            await random_delay(page, 1000, 2000)

            # Sweep popups after add-to-cart (health consent, coverage offers, etc.)
            await sweep_popups(page)

            # Decline optional coverage/warranty if modal appears
            await self._smart_click(
                page, "No thanks / Decline coverage",
                'button[data-test="espModalContent-declineCoverageButton"], button:has-text("No thanks"), '
                'button:has-text("No, thanks")',
                timeout=2000,
            )
            await random_delay(page, 300, 700)

            # Go to cart via modal button or direct navigation
            if not await self._smart_click(
                page, "View cart & check out",
                'button[data-test="addToCartModalViewCartCheckout"], a[href*="/cart"], '
                'button:has-text("View cart"), button:has-text("View cart & check out")',
                timeout=3000,
            ):
                await page.goto("https://www.target.com/cart", wait_until="domcontentloaded")
                await wait_for_page_ready(page, timeout=15000)

            # Sweep popups on cart page
            await sweep_popups(page)

            # --- Handle delivery method selection on cart page ---
            # Target requires choosing "Shipping" or "Pickup" for each item.
            # If there's a "Choose delivery method" prompt, select shipping.
            await self._target_select_delivery(page)

            # Wait for checkout button to be enabled (it's grayed out until
            # delivery method is selected and cart is validated)
            checkout_sel = 'button[data-test="checkout-button"]'
            await wait_for_button_enabled(page, checkout_sel, timeout=15000)
            await random_delay(page, 200, 500)

            # Click checkout
            if not await self._smart_click(
                page, "Check out",
                'button[data-test="checkout-button"], button:has-text("Check out"), '
                'a:has-text("Check out"), button[data-test="checkout-btn"]',
            ):
                raise Exception("Checkout button not found")
            await wait_for_page_ready(page, timeout=15000)

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
                await wait_for_page_ready(page, timeout=10000)
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
                        await human_click_element(page, btn)
                        logger.info("Health consent modal: dismissed via '%s'", sel)
                        await random_delay(page, 800, 1500)
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
                    await random_delay(page, 800, 1500)
                    return True

            return False
        except Exception as exc:
            logger.debug("Health consent modal dismiss failed (non-fatal): %s", exc)
            return False

    async def _dismiss_target_overlay(self, page):
        """Dismiss Target's popups, overlays, and modals.

        Delegates to the shared sweep_popups() utility which handles cookie
        consent, sign-in prompts, store pickers, age gates, health consent,
        and generic dialogs.  Kept as a method for backward compatibility.
        """
        await sweep_popups(page)

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
                        await human_click_element(page, loc)
                        shipping_clicked = True
                        logger.info("Target cart: clicked shipping via %s", sel)
                        break
                except Exception:
                    continue

            if not shipping_clicked:
                # Vision fallback (already uses human_click internally)
                shipping_clicked = await self._smart_click(
                    page, "Shipping delivery option",
                    "",
                    timeout=2000,
                )

            if shipping_clicked:
                await wait_for_page_ready(page, timeout=5000)

                # Target sometimes shows a "Save" or "Apply" button after selecting delivery
                for save_sel in [
                    'button:has-text("Save")',
                    'button:has-text("Apply")',
                    'button:has-text("Update")',
                ]:
                    try:
                        loc = page.locator(save_sel).first
                        if await loc.is_visible(timeout=1000):
                            await human_click_element(page, loc)
                            await random_delay(page, 800, 1500)
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

        AUDIT: This method has no popup handling between steps. If Target shows
        an "address suggestion" modal, a "promo code" interstitial, or any other
        unexpected dialog during checkout navigation, this method will fail to
        find the "Continue" button (it's behind the modal) and silently break.
        The rewrite must call popup_handler.sweep() before each continue click.
        Also: no price check is performed before reaching the "Place order" page.
        """
        continue_sel = (
            'button[data-test="save-and-continue-button"], '
            'button:has-text("Save and continue"), '
            'button:has-text("Continue"), '
            'button:has-text("Save & continue")'
        )
        continue_css = 'button[data-test="save-and-continue-button"]'

        # Click "Continue" / "Save and continue" up to 5 times to progress through steps
        for step in range(5):
            # Sweep popups before each step (address suggestions, promos, etc.)
            await sweep_popups(page)

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
                                await human_click_element(page, cvv_input)
                                await random_delay(page, 150, 300)
                                await human_type(page, creds.card_cvv)
                                logger.info("Target checkout: entered CVV via %s", cvv_sel)
                                cvv_filled = True
                                await random_delay(page, 300, 600)
                            else:
                                logger.warning("Target checkout: CVV field found but no CVV configured! "
                                             "Add CVV via Dashboard > Settings > Accounts > Target > Edit")
                            break
                    except Exception:
                        continue

                if cvv_filled:
                    # After filling CVV, wait for continue button to enable
                    await random_delay(page, 300, 600)
            except Exception as exc:
                logger.debug("Target checkout: CVV check error (non-fatal): %s", exc)

            # Wait for the continue button to be enabled before clicking
            await wait_for_button_enabled(page, continue_css, timeout=10000)
            await random_delay(page, 100, 300)

            # Try clicking continue/save
            try:
                continue_btn = page.locator(continue_sel).first
                if await continue_btn.is_visible(timeout=3000):
                    await human_click_element(page, continue_btn)
                    logger.info("Target checkout: clicked continue (step %d)", step + 1)
                    await wait_for_page_ready(page, timeout=10000)
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
                    await wait_for_page_ready(page, timeout=10000)
            except Exception:
                break

    async def _sign_in_target(self, page, creds: AccountCredentials):
        # Target has NO dedicated /login page — it redirects to homepage.
        # Login is triggered by clicking the account icon on the homepage,
        # which opens a side panel with "Sign in or create account".

        email_sel = '#username, input[name="username"], input[type="email"], input[type="tel"], input[id*="username" i], input[name*="email" i], input[autocomplete="username"], input[autocomplete="email tel"]'
        pass_sel = '#password, input[name="password"], input[type="password"], input[id*="password" i]'
        submit_sel = 'button[type="submit"]'

        account_link_sel = (
            '[data-test="@web/AccountLink"], #account, #accountNav, '
            'a[href*="/account"], a:has-text("Sign in"), '
            'button:has-text("Sign in"), '
            '[data-test="accountNav-signIn"], '
            '[data-test="@web/AccountLink-signIn"]'
        )

        sign_in_panel_btn_sel = (
            'a:has-text("Sign in or create account"), '
            'button:has-text("Sign in or create account"), '
            'a[href*="/login"], '
            '[data-test="accountNav-signIn"]'
        )

        # --- Start network monitor to observe OAuth flow ---
        net_monitor = NetworkMonitor(page)
        await net_monitor.start()

        # Step 0: Navigate to homepage and open the sign-in panel
        email_input = None
        for attempt in range(3):
            if attempt == 0:
                await page.goto("https://www.target.com", wait_until="domcontentloaded")
            else:
                logger.warning("Target login: form not rendered (attempt %d/3) — reloading", attempt)
                await page.reload(wait_until="domcontentloaded")

            await wait_for_page_ready(page, timeout=15000)
            await sweep_popups(page)

            # Human-like: browse the page before clicking sign-in
            await random_mouse_jitter(page)
            await random_delay(page, 500, 1500)

            # Click account icon to open side panel
            try:
                account_link = page.locator(account_link_sel)
                if await account_link.first.is_visible(timeout=8000):
                    await human_click_element(page, account_link)
                    await random_delay(page, 1000, 2000)
                else:
                    # Vision fallback for account icon
                    await self._smart_click(page, "Account or Sign in icon", account_link_sel, timeout=5000)
                    await random_delay(page, 1000, 2000)
            except Exception:
                await self._smart_click(page, "Account or Sign in icon", account_link_sel, timeout=5000)
                await random_delay(page, 1000, 2000)

            # Click "Sign in or create account" in the side panel
            try:
                sign_in_btn = page.locator(sign_in_panel_btn_sel)
                if await sign_in_btn.first.is_visible(timeout=5000):
                    await human_click_element(page, sign_in_btn)
                    logger.info("Target login: clicked 'Sign in or create account' in side panel")
                    await wait_for_page_ready(page, timeout=15000)
                    await random_delay(page, 500, 1500)
            except Exception:
                # May have navigated directly to login form
                pass

            await sweep_popups(page)
            await random_mouse_jitter(page)

            # Poll for the email field to appear (React hydration)
            try:
                email_input = page.locator(email_sel).first
                await email_input.wait_for(state="visible", timeout=15000)
                break  # form rendered
            except Exception:
                email_input = None

        if email_input is None:
            await net_monitor.stop()
            raise RuntimeError("Target login form did not render — email field not found after 3 attempts")

        # Step 1: Enter email/phone — human-like

        # Move mouse to the field, dwell, then click
        await human_click_element(page, page.locator(email_sel))
        await random_delay(page, 200, 400)
        await email_input.press("Control+a")
        # Type with variable speed (not fixed delay)
        await human_type(page, creds.email)
        await random_delay(page, 300, 600)

        # Verify the value actually got entered — Target's JS can clear it
        try:
            actual_value = await email_input.input_value(timeout=1000)
            if actual_value != creds.email:
                logger.warning("Target sign-in: human_type() produced '%s', expected '%s' — using fill()",
                              actual_value[:20], creds.email[:20])
                await email_input.fill(creds.email)
                await random_delay(page, 200, 400)
        except Exception:
            # If we can't read the value, try fill() as backup
            logger.warning("Target sign-in: could not verify email input — using fill() as backup")
            await email_input.fill(creds.email)
            await random_delay(page, 200, 400)

        # Check if password is already visible (single-step) or multi-step
        pass_visible = False
        try:
            pass_visible = await page.locator(pass_sel).first.is_visible(timeout=1000)
        except Exception:
            pass

        if pass_visible:
            # Single-step: fill password and submit
            await human_click_element(page, page.locator(pass_sel))
            await random_delay(page, 150, 300)
            await human_type(page, creds.password)
            await random_delay(page, 200, 500)

            # Wait for submit button to be enabled (grayed-out fix)
            await wait_for_button_enabled(page, submit_sel, timeout=15000)
            await random_delay(page, 100, 300)

            pre_url = page.url
            await self._multi_strategy_click(page, "Sign in", [
                "Sign in", "Continue", "Log in",
            ], 'button[type="submit"], button:has-text("Sign in")')

            # Wait for login to complete via network monitoring
            login_done = await net_monitor.wait_for_login_complete(timeout=15000)
            if not login_done:
                # Fallback: check if URL changed away from login
                await wait_for_url_change(page, pre_url, timeout=10000)
        else:
            # Step 2: Submit email — wait for button enabled first
            await wait_for_button_enabled(page, submit_sel, timeout=15000)
            await random_delay(page, 100, 300)

            await self._multi_strategy_click(page, "Continue with email", [
                "Continue with email", "Continue", "Sign in", "Next",
            ], 'button[type="submit"], button:has-text("Continue")')

            # Wait for the page to respond (not a fixed 3s wait)
            await wait_for_page_ready(page, timeout=10000)

            # Sweep for any popups that appeared after email submit
            await sweep_popups(page)

            # Check if we navigated away from login after email submit
            # (e.g. Target may redirect to homepage if already authenticated)
            post_email_url = page.url
            login_indicators = ["/login", "/signin", "/sign-in", "/identity"]
            if not any(ind in post_email_url.lower() for ind in login_indicators):
                logger.info("Target sign-in: navigated away from login after email submit (%s) — may already be signed in", post_email_url)
                await net_monitor.stop()
                return  # Let the caller check success via URL/cookies

            # Step 3: Auth method picker — Target shows "Enter your password"
            pw_option_clicked = False

            # Strategy 1: get_by_role("button") for button-style pickers
            for option_text in ["Enter your password", "Enter password", "Password", "Use password"]:
                try:
                    opt = page.get_by_role("button", name=option_text, exact=False)
                    if await opt.first.is_visible(timeout=500):
                        await human_click_element(page, opt)
                        pw_option_clicked = True
                        break
                except Exception:
                    continue

            # Strategy 2: get_by_role("radio") for radio-button pickers (Walmart)
            if not pw_option_clicked:
                try:
                    opt = page.get_by_role("radio", name="Password", exact=False)
                    if await opt.first.is_visible(timeout=500):
                        await human_click_element(page, opt)
                        pw_option_clicked = True
                except Exception:
                    pass

            # Strategy 3: get_by_text (catches divs/links/labels acting as buttons)
            if not pw_option_clicked:
                for option_text in ["Enter your password", "Enter password", "Password"]:
                    try:
                        opt = page.get_by_text(option_text, exact=True)
                        if await opt.first.is_visible(timeout=500):
                            await human_click_element(page, opt)
                            pw_option_clicked = True
                            break
                    except Exception:
                        continue

            # Strategy 4: CSS selectors (labels for radio, buttons, links)
            if not pw_option_clicked:
                password_option = page.locator('button:has-text("password"), a:has-text("password"), [data-test*="password" i], div:has-text("Enter your password"), label:has-text("Password"), input[type="radio"][value*="password" i]')
                try:
                    if await password_option.first.is_visible(timeout=1000):
                        await human_click_element(page, password_option)
                        pw_option_clicked = True
                except Exception:
                    pass

            # Strategy 5: Vision fallback
            if not pw_option_clicked:
                logger.info("Sign-in: trying vision for auth method picker")
                pw_option_clicked = await self._smart_click(page, "Password option (radio button or link)", "", timeout=1000)

            if pw_option_clicked:
                await wait_for_page_ready(page, timeout=8000)

            # Step 4: Enter password — human-like
            await page.locator(pass_sel).first.wait_for(state="visible", timeout=10000)
            await human_click_element(page, page.locator(pass_sel))
            await random_delay(page, 150, 300)
            await human_type(page, creds.password)
            await random_delay(page, 200, 500)

            # Wait for Sign In button to be enabled (grayed-out fix)
            await wait_for_button_enabled(page, submit_sel, timeout=15000)
            await random_delay(page, 100, 300)

            pre_url = page.url
            await self._multi_strategy_click(page, "Sign in", [
                "Sign in", "Continue", "Log in",
            ], 'button[type="submit"], button:has-text("Sign in")')

            # Wait for login to complete via network monitoring instead of fixed wait
            login_done = await net_monitor.wait_for_login_complete(timeout=20000)
            if not login_done:
                # Fallback: check if URL changed
                await wait_for_url_change(page, pre_url, timeout=10000)

        # Check if PerimeterX blocked us during login
        if net_monitor.was_blocked():
            blocked = net_monitor.get_blocked_details()
            logger.warning("Target sign-in: PerimeterX blocked %d request(s) during login", len(blocked))

        await net_monitor.stop()

        # Sweep any post-login popups (Target sometimes shows "welcome back" modals)
        await sweep_popups(page)

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
            await wait_for_page_ready(page, timeout=10000)

            # Add to cart
            await sweep_popups(page)
            if not await self._smart_click(
                page, "Add to cart",
                'button[data-tl-id="ProductPrimaryCTA-cta_add_to_cart_button"], button[data-tl-id*="addToCart"], button:has-text("Add to cart")',
            ):
                error = await self._smart_read_error(page)
                if error:
                    raise Exception(f"Cannot add to cart: {error}")
                raise Exception("Add to cart button not found")
            await random_delay(page, 1500, 2500)

            # Go to checkout via button or direct navigation
            if not await self._smart_click(
                page, "Check out",
                'button[data-tl-id="IPPacCheckOutBtnBottom"], button:has-text("Check out")',
                timeout=3000,
            ):
                await page.goto("https://www.walmart.com/checkout", wait_until="domcontentloaded")
                await wait_for_page_ready(page, timeout=10000)

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
                        await human_click_element(page, pw_radio)
                        await random_delay(page, 800, 1500)
                except Exception:
                    pass

                if not auth_picker_visible:
                    # Standard flow: fill email/phone and submit (human-like via _smart_fill)
                    email_filled = await self._smart_fill(page, "email/phone", email_sel, creds.email)
                    if email_filled:
                        await wait_for_button_enabled(page, 'button[type="submit"]', timeout=10000)
                        await self._multi_strategy_click(page, "Continue", [
                            "Continue", "Sign in", "Next",
                        ], 'button[type="submit"]')
                        await wait_for_page_ready(page, timeout=10000)

                        # Check for auth method picker after submit
                        try:
                            pw_radio = page.get_by_role("radio", name="Password", exact=False)
                            if await pw_radio.first.is_visible(timeout=2000):
                                await human_click_element(page, pw_radio)
                                await random_delay(page, 800, 1500)
                        except Exception:
                            pass

                # Now enter password (human-like via _smart_fill)
                pass_filled = await self._smart_fill(page, "password", pass_sel, creds.password)
                if pass_filled:
                    await wait_for_button_enabled(page, 'button[type="submit"]', timeout=10000)
                    await self._multi_strategy_click(page, "Sign in", [
                        "Sign in", "Log in", "Continue",
                    ], 'button[type="submit"]')
                    await wait_for_page_ready(page, timeout=10000)
                else:
                    # Full vision-assisted sign-in
                    await self._smart_sign_in(page, creds, "walmart")

            # Guest checkout fallback if not signed in
            await self._smart_click(
                page, "Continue as guest",
                'button[data-tl-id="Wel-Guest_cxo_btn"], button:has-text("Continue without account"), button:has-text("Guest")',
                timeout=2000,
            )

            # Wait for checkout page to be ready, then place order
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
                await wait_for_page_ready(page, timeout=10000)
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
            await wait_for_page_ready(page, timeout=15000)

            # PKC often has queue — wait for page to load
            # Add to cart
            await sweep_popups(page)
            if not await self._smart_click(
                page, "Add to Cart",
                'button:has-text("Add to Cart"), button:has-text("Add to Bag")',
                timeout=10000,
            ):
                error = await self._smart_read_error(page)
                if error:
                    raise Exception(f"Cannot add to cart: {error}")
                raise Exception("Add to cart button not found")
            await random_delay(page, 1500, 2500)

            # Navigate to cart/checkout
            if not await self._smart_click(
                page, "Go to Cart",
                'a[href*="cart"], a:has-text("Cart"), a:has-text("Bag")',
            ):
                raise Exception("Cart link not found")
            await wait_for_page_ready(page, timeout=10000)

            if not await self._smart_click(
                page, "Checkout",
                'button:has-text("Checkout"), a:has-text("Checkout")',
            ):
                raise Exception("Checkout button not found")
            await wait_for_page_ready(page, timeout=10000)

            # Sign in if needed (may redirect to access.pokemon.com SSO)
            current_url = page.url
            if "access.pokemon.com" in current_url or "sso.pokemon.com" in current_url:
                email_sel = 'input[name="email"], input[name="username"], input[type="email"], input[type="text"]'
                pass_sel = 'input[type="password"], input[name="password"]'
            else:
                email_sel = 'input[type="email"], input[name="email"], input[type="text"][autocomplete="email"], input[type="text"][autocomplete="username"], input[id*="email" i], input[id*="login" i], input[name*="email" i], input[name*="login" i]'
                pass_sel = 'input[type="password"], input[name="password"], input[id*="password" i]'

            # Try selector-based sign-in (human-like via _smart_fill), fall back to vision
            email_filled = await self._smart_fill(page, "email", email_sel, creds.email, timeout=5000)
            if email_filled:
                await self._smart_fill(page, "password", pass_sel, creds.password)
                await wait_for_button_enabled(page, 'button[type="submit"]', timeout=10000)
                await self._multi_strategy_click(page, "Sign In", [
                    "Sign In", "Log In", "Continue",
                ], 'button[type="submit"], button:has-text("Sign In")')
                await wait_for_page_ready(page, timeout=10000)
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
                await wait_for_page_ready(page, timeout=10000)
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
            await wait_for_page_ready(page, timeout=10000)

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
            await sweep_popups(page)
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
            await random_delay(page, 1500, 2500)

            # Go to cart via popup button or direct navigation
            if not await self._smart_click(
                page, "Go to Cart",
                'div.go-to-cart-button a, a:has-text("Go to Cart"), a[href*="/cart"]',
                timeout=3000,
            ):
                await page.goto("https://www.bestbuy.com/cart", wait_until="domcontentloaded")
                await wait_for_page_ready(page, timeout=10000)

            # Click checkout
            if not await self._smart_click(
                page, "Checkout",
                'button[data-track="Checkout - Top"], button:has-text("Checkout"), a:has-text("Checkout")',
            ):
                raise Exception("Checkout button not found")
            await wait_for_page_ready(page, timeout=10000)

            # Sign in if needed — selectors first (human-like via _smart_fill), vision fallback
            email_filled = await self._smart_fill(
                page, "email", 'input#fld-e, input[id="user.emailAddress"], input[type="email"], input[name="email"]', creds.email, timeout=3000,
            )
            if email_filled:
                await self._smart_fill(page, "password", 'input#fld-p1, input[type="password"], input[name="password"]', creds.password)
                await wait_for_button_enabled(page, 'button[type="submit"]', timeout=10000)
                await self._multi_strategy_click(page, "Sign In", [
                    "Sign In", "Log In", "Continue",
                ], 'button[type="submit"], button:has-text("Sign In")')
                await wait_for_page_ready(page, timeout=10000)
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
                await wait_for_page_ready(page, timeout=10000)
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

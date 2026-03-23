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
import re as _re
from pathlib import Path

from pmon.config import Config, AccountCredentials, Profile
from pmon.models import CheckoutResult, CheckoutStatus
from pmon import database as db
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
                        card_number=acct.get("card_number", ""),
                        card_exp_month=acct.get("card_exp_month", ""),
                        card_exp_year=acct.get("card_exp_year", ""),
                        card_name=acct.get("card_name", ""),
                        phone_last4=acct.get("phone_last4", ""),
                        account_last_name=acct.get("account_last_name", ""),
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
                    return await handler(url, product_name, profile, creds, dry_run=dry_run, user_id=user_id)
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

    async def _get_context(self, retailer: str, *, load_cookies: bool = True):
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
        if load_cookies and storage_path.exists():
            ctx_kwargs["storage_state"] = str(storage_path)
        context = await self._browser.new_context(**ctx_kwargs)
        await context.add_init_script(STEALTH_JS)
        return context

    async def _save_context(self, context, retailer: str):
        """Save browser cookies/state for reuse."""
        storage_path = SESSION_DIR / f"{retailer}.json"
        await context.storage_state(path=str(storage_path))

    async def _quick_target_stock_check(self, url: str) -> bool | None:
        """Quick Redsky API stock check before starting browser checkout.

        Returns True if in stock, False if confirmed OOS, None if unable
        to determine (e.g. API error — proceed with checkout attempt).
        """
        import httpx

        match = _re.search(r"A-(\d+)", url)
        if not match:
            return None  # can't extract TCIN, let checkout handle it

        tcin = match.group(1)
        api_key = "e59ce3b531b2c39afb2e2b8a71ff10113aac2a14"

        try:
            import uuid as _uuid
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                resp = await client.get(
                    "https://redsky.target.com/redsky_aggregations/v1/web/product_fulfillment_v1",
                    params={
                        "key": api_key,
                        "tcin": tcin,
                        "store_id": "2845",
                        "zip": "21224",
                        "state": "MD",
                        "latitude": "39.282024",
                        "longitude": "-76.569695",
                        "pricing_store_id": "2845",
                        "has_pricing_store_id": "true",
                        "has_store_positions_store_id": "true",
                        "store_positions_store_id": "2845",
                        "is_bot": "false",
                        "visitor_id": _uuid.uuid4().hex,
                        "channel": "WEB",
                        "page": f"/p/A-{tcin}",
                    },
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                        "Accept": "application/json",
                        "Referer": "https://www.target.com/",
                        "Origin": "https://www.target.com",
                    },
                )
                if resp.status_code != 200:
                    logger.debug("Quick stock check: HTTP %d for TCIN %s", resp.status_code, tcin)
                    return None

                data = resp.json()
                product = data.get("data", {}).get("product", {})
                fulfillment = product.get("fulfillment", {})

                # Check shipping availability
                shipping = fulfillment.get("shipping_options", {})
                avail_status = shipping.get("availability_status", "")
                if avail_status == "IN_STOCK":
                    return True

                # Check availability_status_v2
                for method in shipping.get("availability_status_v2", []):
                    if method.get("is_available", False):
                        return True

                # Check store pickup
                for store in fulfillment.get("store_options", []):
                    pickup = store.get("order_pickup", {})
                    if pickup.get("availability_status") == "IN_STOCK":
                        return True

                # Check product-level availability
                product_avail = product.get("availability", {})
                status = product_avail.get("availability_status", "")
                if status in ("IN_STOCK", "LIMITED_STOCK", "PRE_ORDER"):
                    return True

                # Check if explicitly OOS
                if fulfillment.get("is_out_of_stock_in_all_store_locations", False):
                    return False
                if avail_status in ("OUT_OF_STOCK", "UNAVAILABLE"):
                    return False
                if status in ("OUT_OF_STOCK", "UNAVAILABLE"):
                    return False

                # Can't determine — let checkout proceed
                return None

        except Exception as exc:
            logger.debug("Quick stock check failed for %s: %s", url, exc)
            return None

    async def _checkout_target(
        self, url: str, product_name: str, profile: Profile, creds: AccountCredentials,
        dry_run: bool = False, **kwargs,
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
        # Quick stock re-verification before launching browser + login flow
        stock_status = await self._quick_target_stock_check(url)
        if stock_status is False:
            logger.info("Target: quick stock re-check confirms %s is OUT OF STOCK — skipping checkout", product_name)
            return CheckoutResult(
                url=url, retailer="target", product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message="Product is out of stock (confirmed on re-check before checkout)",
            )

        context = await self._get_context("target")
        page = await context.new_page()

        try:
            # Navigate to product page
            await page.goto(url, wait_until="domcontentloaded")
            await wait_for_page_ready(page, timeout=15000)

            # Sweep all popups/overlays (cookie consent, promos, health consent, etc.)
            await sweep_popups(page)
            await self._dismiss_health_consent_modal(page)

            # Human-like: glance at the page before interacting
            await idle_scroll(page)
            await random_delay(page, 500, 1500)

            # Check if we need to sign in
            if not await self._is_signed_in_target(page):
                await self._sign_in_target(page, creds)
                # Navigate back to product page — login flow leaves us on homepage
                await page.goto(url, wait_until="domcontentloaded")
                await wait_for_page_ready(page, timeout=15000)
                if not await self._is_signed_in_target(page):
                    logger.warning("Target: sign-in may have failed — proceeding anyway")

            # Sweep popups again after sign-in (welcome back, promos)
            await sweep_popups(page)

            # Dismiss Health Data Consent modal if present — Target shows this on
            # health-related products and it blocks all page interaction.
            await self._dismiss_health_consent_modal(page)

            # Try to add to cart — prefer "Ship it" (sets delivery method immediately)
            add_to_cart_sel = (
                'button[data-test="shipItButton"], button[data-test="shippingButton"], '
                'button:has-text("Ship it"), button:has-text("Add to cart")'
            )
            add_to_cart_clicked = await self._smart_click(page, "Ship it / Add to cart", add_to_cart_sel)
            if not add_to_cart_clicked:
                # Popup may have blocked the click — sweep and retry
                await self._dismiss_health_consent_modal(page)
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

            # Sweep popups on cart page and browse briefly
            await sweep_popups(page)
            await random_mouse_jitter(page)
            await random_delay(page, 300, 800)

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
        # Check 1: account nav text (works on most Target pages)
        try:
            account = page.locator('#account, [data-test="accountNav"]')
            text = await account.inner_text(timeout=3000)
            if "sign in" not in text.lower():
                return True
        except Exception:
            pass

        # Check 2: auth cookies — Target sets accessToken/refreshToken on login
        try:
            cookies = await page.context.cookies("https://www.target.com")
            cookie_names = {c["name"] for c in cookies}
            if "accessToken" in cookie_names or "refreshToken" in cookie_names:
                return True
        except Exception:
            pass

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
                # Target's known data-test attribute for health consent
                '[data-test="healthFlagModalAcceptButton"]',
                '[data-test="health-consent-modal"] button:has-text("Agree")',
                '[data-test="healthConsentModal"] button:has-text("Agree")',
                # "I understand" / "I accept" — common health consent wording
                'button:has-text("I understand")',
                'button:has-text("I accept")',
                # Generic modal with health-related text + agree/acknowledge button
                '[role="dialog"] button:has-text("I agree")',
                '[role="dialog"] button:has-text("Agree")',
                '[role="dialog"] button:has-text("Acknowledge")',
                '[role="dialog"] button:has-text("Accept")',
                '[role="dialog"] button:has-text("Continue")',
                '[role="dialog"] button:has-text("confirm")',
                '[role="dialog"] button:has-text("agree")',
                '[aria-modal="true"] button:has-text("I agree")',
                '[aria-modal="true"] button:has-text("Agree")',
                '[aria-modal="true"] button:has-text("I understand")',
                '[aria-modal="true"] button:has-text("Accept")',
                'dialog button:has-text("I agree")',
                'dialog button:has-text("Agree")',
                'dialog button:has-text("Acknowledge")',
                'dialog button:has-text("Accept")',
                'dialog button:has-text("Continue")',
                'dialog button:has-text("I understand")',
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

            # Human-like: small jitter between checkout steps
            await random_mouse_jitter(page)
            await random_delay(page, 200, 600)

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

            # Re-dismiss overlays (Target's consent modal can reappear after email submit)
            await sweep_popups(page)

            # Check if password field is already visible (no picker needed)
            try:
                if await page.locator(pass_sel).first.is_visible(timeout=2000):
                    pw_option_clicked = True
                    logger.info("Target sign-in: password field already visible (no auth picker)")
            except Exception:
                pass

            pw_texts = [
                "Enter your password", "Enter password",
                "Password", "Use password", "Sign in with password",
                "password",
            ]

            # Strategy 1: get_by_role("button") for button-style pickers
            if not pw_option_clicked:
                for option_text in pw_texts:
                    try:
                        opt = page.get_by_role("button", name=option_text, exact=False)
                        if await opt.first.is_visible(timeout=500):
                            await human_click_element(page, opt)
                            pw_option_clicked = True
                            logger.info("Target sign-in: clicked auth method via get_by_role('button', '%s')", option_text)
                            break
                    except Exception:
                        continue

            # Strategy 2: get_by_role("link") — Target often uses <a> tags for auth options
            if not pw_option_clicked:
                for option_text in pw_texts:
                    try:
                        opt = page.get_by_role("link", name=option_text, exact=False)
                        if await opt.first.is_visible(timeout=500):
                            await human_click_element(page, opt)
                            pw_option_clicked = True
                            logger.info("Target sign-in: clicked auth method via get_by_role('link', '%s')", option_text)
                            break
                    except Exception:
                        continue

            # Strategy 3: get_by_role("radio") for radio-button pickers
            if not pw_option_clicked:
                try:
                    opt = page.get_by_role("radio", name="Password", exact=False)
                    if await opt.first.is_visible(timeout=500):
                        await human_click_element(page, opt)
                        pw_option_clicked = True
                        logger.info("Target sign-in: clicked auth method via get_by_role('radio')")
                except Exception:
                    pass

            # Strategy 4: get_by_text (catches divs/spans/labels acting as buttons)
            if not pw_option_clicked:
                for option_text in pw_texts:
                    try:
                        opt = page.get_by_text(option_text, exact=False)
                        if await opt.first.is_visible(timeout=500):
                            await human_click_element(page, opt)
                            pw_option_clicked = True
                            logger.info("Target sign-in: clicked auth method via get_by_text('%s')", option_text)
                            break
                    except Exception:
                        continue

            # Strategy 5: CSS selectors (labels for radio, buttons, links, list items)
            if not pw_option_clicked:
                password_option = page.locator(
                    'button:has-text("password"), a:has-text("password"), '
                    '[data-test*="password" i], div:has-text("Enter your password"), '
                    'label:has-text("Password"), input[type="radio"][value*="password" i], '
                    'li:has-text("password"), span:has-text("Enter your password")'
                )
                try:
                    if await password_option.first.is_visible(timeout=1000):
                        await human_click_element(page, password_option)
                        pw_option_clicked = True
                        logger.info("Target sign-in: clicked auth method via CSS selector")
                except Exception:
                    pass

            # Strategy 6: JS click — find any clickable element containing "password" text
            if not pw_option_clicked:
                try:
                    clicked_js = await page.evaluate("""() => {
                        const els = document.querySelectorAll('a, button, [role="button"], [role="link"], li, div[tabindex], span[tabindex]');
                        for (const el of els) {
                            const text = (el.textContent || '').toLowerCase().trim();
                            if (text.includes('password') && !text.includes('forgot') && el.offsetParent !== null) {
                                el.click();
                                return el.tagName + ': ' + text.substring(0, 60);
                            }
                        }
                        return null;
                    }""")
                    if clicked_js:
                        pw_option_clicked = True
                        logger.info("Target sign-in: clicked auth method via JS: %s", clicked_js)
                except Exception:
                    pass

            # Strategy 7: Vision fallback
            if not pw_option_clicked:
                logger.info("Target sign-in: trying vision for auth method picker")
                pw_option_clicked = await self._smart_click(
                    page, "Password option (radio button or link to select password sign-in method)", "", timeout=2000
                )

            if pw_option_clicked:
                await wait_for_page_ready(page, timeout=8000)
            else:
                logger.warning("Target sign-in: could not find password auth method option")

            # Step 4: Enter password — human-like, with vision fallback
            pass_found = False
            try:
                await page.locator(pass_sel).first.wait_for(state="visible", timeout=10000)
                pass_found = True
            except Exception:
                pass

            if pass_found:
                await human_click_element(page, page.locator(pass_sel))
                await random_delay(page, 150, 300)
                await human_type(page, creds.password)
                await random_delay(page, 200, 500)
                # Verify password was entered; if empty, fall back to fill()
                try:
                    pw_value = await page.locator(pass_sel).first.input_value(timeout=1000)
                    if not pw_value:
                        logger.warning("Target sign-in: human_type() did not fill password — using fill()")
                        await page.locator(pass_sel).first.fill(creds.password)
                        await random_delay(page, 200, 400)
                except Exception:
                    pass
            else:
                # Vision fallback for password entry
                logger.warning("Target sign-in: password field not found via selectors — trying vision")
                vision_filled = await self._smart_fill(
                    page, "password input field", pass_sel, creds.password
                )
                if not vision_filled:
                    raise RuntimeError("Target sign-in: password field did not appear after selecting auth method")

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
        self, url: str, product_name: str, profile: Profile, creds: AccountCredentials,
        dry_run: bool = False, **kwargs,
    ) -> CheckoutResult:
        """Walmart checkout flow."""
        # Try without cookies first — fresh session is less likely to hit stale
        # auth state or anti-bot flags from old sessions.  Fall back to saved
        # cookies only if the fresh attempt fails at sign-in.
        context = await self._get_context("walmart", load_cookies=False)
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded")
            await wait_for_page_ready(page, timeout=10000)

            # Human-like: browse the product page before interacting
            await sweep_popups(page)
            await random_mouse_jitter(page)
            await idle_scroll(page)
            await random_delay(page, 500, 1500)

            # Add to cart
            await sweep_popups(page)
            if not await self._smart_click(
                page, "Add to cart",
                'button[data-tl-id="ProductPrimaryCTA-cta_add_to_cart_button"], button[data-tl-id*="addToCart"], button:has-text("Add to cart")',
            ):
                # Popup may have blocked the click — sweep and retry
                if await sweep_popups(page):
                    await self._smart_click(
                        page, "Add to cart",
                        'button[data-tl-id="ProductPrimaryCTA-cta_add_to_cart_button"], button[data-tl-id*="addToCart"], button:has-text("Add to cart")',
                    )
                else:
                    error = await self._smart_read_error(page)
                    if error:
                        raise Exception(f"Cannot add to cart: {error}")
                    raise Exception("Add to cart button not found")
            await random_delay(page, 1500, 2500)

            # Sweep popups after add-to-cart (coverage offers, promo modals)
            await sweep_popups(page)

            # Go to checkout via button or direct navigation
            if not await self._smart_click(
                page, "Check out",
                'button[data-tl-id="IPPacCheckOutBtnBottom"], button:has-text("Check out")',
                timeout=3000,
            ):
                await page.goto("https://www.walmart.com/checkout", wait_until="domcontentloaded")
                await wait_for_page_ready(page, timeout=10000)

            await sweep_popups(page)

            # Sign in if needed — try selectors first, then vision
            sign_in_visible = False
            try:
                sign_in_visible = await page.locator('button:has-text("Sign in"), a:has-text("Sign in")').first.is_visible(timeout=2000)
            except Exception:
                pass

            if sign_in_visible:
                # Start network monitor to track login completion
                net_monitor = NetworkMonitor(page)
                net_monitor.add_pattern("wmt_auth", "/account/electrode/api/signin")
                net_monitor.add_pattern("wmt_token", "/orchestra/snb/graphql")
                await net_monitor.start()

                email_sel = 'input[name="email"], input[type="email"], input[id*="email" i], input[type="tel"], input[name="phone"], input[id*="phone" i], #phone-number, input[autocomplete="tel"]'
                pass_sel = 'input[type="password"], input[name="password"], input[id*="password" i]'

                # Human-like: jitter before interacting with login form
                await random_mouse_jitter(page)

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
                    # Standard flow: fill email/phone and submit
                    email_filled = await self._smart_fill(page, "email/phone", email_sel, creds.email)
                    if email_filled:
                        # Verify the value got entered (Walmart's React may clear it)
                        try:
                            actual = await page.locator(email_sel).first.input_value(timeout=1000)
                            if actual != creds.email:
                                logger.warning("Walmart sign-in: email value mismatch — using fill()")
                                await page.locator(email_sel).first.fill(creds.email)
                                await random_delay(page, 200, 400)
                        except Exception:
                            pass

                        await wait_for_button_enabled(page, 'button[type="submit"]', timeout=10000)
                        await self._multi_strategy_click(page, "Continue", [
                            "Continue", "Sign in", "Next",
                        ], 'button[type="submit"]')
                        await wait_for_page_ready(page, timeout=10000)

                        # Sweep popups after email submit
                        await sweep_popups(page)

                        # Check for auth method picker after submit
                        try:
                            pw_radio = page.get_by_role("radio", name="Password", exact=False)
                            if await pw_radio.first.is_visible(timeout=2000):
                                await human_click_element(page, pw_radio)
                                await random_delay(page, 800, 1500)
                        except Exception:
                            pass

                # Now enter password
                pass_filled = await self._smart_fill(page, "password", pass_sel, creds.password)
                if pass_filled:
                    # Verify password was entered
                    try:
                        pw_value = await page.locator(pass_sel).first.input_value(timeout=1000)
                        if not pw_value:
                            logger.warning("Walmart sign-in: password empty — using fill()")
                            await page.locator(pass_sel).first.fill(creds.password)
                            await random_delay(page, 200, 400)
                    except Exception:
                        pass

                    await wait_for_button_enabled(page, 'button[type="submit"]', timeout=10000)

                    pre_url = page.url
                    await self._multi_strategy_click(page, "Sign in", [
                        "Sign in", "Log in", "Continue",
                    ], 'button[type="submit"]')

                    # Wait for login to complete via network monitoring
                    login_done = await net_monitor.wait_for("wmt_auth", timeout=15000)
                    if not login_done:
                        await wait_for_url_change(page, pre_url, timeout=10000)
                else:
                    # Full vision-assisted sign-in
                    await self._smart_sign_in(page, creds, "walmart")

                # Check if blocked during login
                login_blocked = net_monitor.was_blocked()
                if login_blocked:
                    blocked = net_monitor.get_blocked_details()
                    logger.warning("Walmart sign-in: blocked %d request(s) during login", len(blocked))

                await net_monitor.stop()

                # If sign-in failed/blocked on fresh session, retry with saved cookies
                storage_path = SESSION_DIR / f"walmart.json"
                if login_blocked and storage_path.exists():
                    logger.info("Walmart: fresh session sign-in blocked — retrying with saved cookies")
                    await page.close()
                    await context.close()
                    context = await self._get_context("walmart", load_cookies=True)
                    page = await context.new_page()
                    await page.goto("https://www.walmart.com/checkout", wait_until="domcontentloaded")
                    await wait_for_page_ready(page, timeout=10000)

                # Sweep post-login popups
                await sweep_popups(page)

            # Guest checkout fallback if not signed in
            await self._smart_click(
                page, "Continue as guest",
                'button[data-tl-id="Wel-Guest_cxo_btn"], button:has-text("Continue without account"), button:has-text("Guest")',
                timeout=2000,
            )

            # Sweep popups on checkout page before placing order
            await sweep_popups(page)

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

    async def _sign_in_pokemoncenter(self, page, creds: AccountCredentials):
        """Pokemon Center sign-in via homepage modal.

        Mirrors the Target login approach: navigate to homepage first to
        establish tracking cookies (PerimeterX/__ssobj, Monetate, Quantum
        Metric, Datadog, OneTrust), then open the sign-in modal from the
        header, fill email/password with human-like behaviour, and wait
        for the auth API response.

        Network flow observed:
          1. GET https://www.pokemoncenter.com  (homepage — warms cookies)
          2. Click header sign-in icon → login modal renders
          3. Form fields: #login-email (type=email), #login-password (type=password)
          4. Submit → POST /tpci-ecommweb-api/auth/login
          5. On success: profile API call, SSAccountSignInCustomEvent tracking,
             redirect to /account
        """

        # --- CSS selectors (multiple fallbacks, ordered by specificity) ---
        email_sel = (
            '#login-email, '
            'input[id*="login-email" i], '
            'input[name="email"][type="email"], '
            'input[type="email"], '
            'input[autocomplete="email"], '
            'input[autocomplete="username"]'
        )
        pass_sel = (
            '#login-password, '
            'input[id*="login-password" i], '
            'input[name="password"], '
            'input[type="password"]'
        )
        submit_sel = (
            'button[type="submit"], '
            'button:has-text("Sign In"), '
            'button:has-text("Log In")'
        )

        # Header sign-in link/icon (multiple selector strategies)
        # PKC uses a span with class like "header-sign-in-mobile--YnxZz" inside the header.
        # The login is a modal on the homepage — there is NO /account/login page.
        sign_in_header_sel = (
            'span[class*="header-sign-in" i], '
            '[class*="header-sign-in" i], '
            'a:has-text("Sign In"), '
            'button:has-text("Sign In"), '
            'a[href*="/account/login"], '
            '[class*="sign-in" i], '
            '[data-testid*="sign-in" i], '
            '[data-testid*="signin" i], '
            'a:has-text("Log In"), '
            'button:has-text("Log In")'
        )

        # --- Start network monitor to track auth API calls ---
        net_monitor = NetworkMonitor(page)
        # Add Pokemon Center specific patterns
        net_monitor.add_pattern("pkc_auth_login", "tpci-ecommweb-api/auth/login")
        net_monitor.add_pattern("pkc_profile", "tpci-ecommweb-api/profile/data")
        net_monitor.add_pattern("pkc_cart", "tpci-ecommweb-api/cart/data")
        net_monitor.add_pattern("pkc_account_event", "SSAccountSignInCustomEvent")
        net_monitor.add_pattern("pkc_resource_api", "site/resourceapi/account")
        await net_monitor.start()

        # ──────────────────────────────────────────────────────────
        # Step 0: Navigate to homepage and warm up cookies
        # ──────────────────────────────────────────────────────────
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

            # Human-like: browse page briefly before clicking sign-in
            await random_mouse_jitter(page)
            await idle_scroll(page)
            await random_delay(page, 800, 2000)

            # ──────────────────────────────────────────────────────
            # Step 1: Click header sign-in to open the login modal
            # ──────────────────────────────────────────────────────
            sign_in_clicked = False

            # Strategy 1: CSS selectors for header sign-in link
            try:
                sign_in_link = page.locator(sign_in_header_sel)
                if await sign_in_link.first.is_visible(timeout=8000):
                    await human_click_element(page, sign_in_link)
                    sign_in_clicked = True
                    logger.info("PKC login: clicked header sign-in link via CSS")
            except Exception:
                pass

            # Strategy 2: get_by_role for links/buttons
            if not sign_in_clicked:
                for role_type in ["link", "button"]:
                    for text in ["Sign In", "Log In", "Sign in", "Log in"]:
                        try:
                            elem = page.get_by_role(role_type, name=text, exact=False)
                            if await elem.first.is_visible(timeout=1000):
                                await human_click_element(page, elem)
                                sign_in_clicked = True
                                logger.info("PKC login: clicked sign-in via get_by_role('%s', '%s')", role_type, text)
                                break
                        except Exception:
                            continue
                    if sign_in_clicked:
                        break

            # Strategy 3: Vision fallback for sign-in icon/link
            if not sign_in_clicked:
                logger.info("PKC login: trying vision for sign-in link")
                sign_in_clicked = await self._smart_click(
                    page, "Sign In link or account icon in the page header",
                    sign_in_header_sel, timeout=5000,
                )

            if not sign_in_clicked:
                # PKC login is modal-only on the homepage — no dedicated login page.
                # Try JS click on any element with "sign-in" in its class name.
                logger.warning("PKC login: header sign-in not found — trying JS click on sign-in class")
                try:
                    clicked_js = await page.evaluate("""() => {
                        const els = document.querySelectorAll('span, a, button, div');
                        for (const el of els) {
                            const cls = (el.className || '').toString().toLowerCase();
                            const text = (el.textContent || '').trim().toLowerCase();
                            if ((cls.includes('sign-in') || cls.includes('signin') || cls.includes('header-sign-in'))
                                && el.offsetParent !== null) {
                                el.click();
                                return el.tagName + '.' + el.className.substring(0, 60);
                            }
                        }
                        return null;
                    }""")
                    if clicked_js:
                        sign_in_clicked = True
                        logger.info("PKC login: clicked sign-in via JS class match: %s", clicked_js)
                except Exception:
                    pass

            await random_delay(page, 1000, 2500)
            await sweep_popups(page)

            # ──────────────────────────────────────────────────────
            # Step 2: Wait for email field to render (React hydration)
            # ──────────────────────────────────────────────────────
            try:
                email_input = page.locator(email_sel).first
                await email_input.wait_for(state="visible", timeout=10000)
                break  # form rendered successfully
            except Exception:
                email_input = None

        if email_input is None:
            await net_monitor.stop()
            raise RuntimeError(
                "PKC login form did not render — email field not found after 3 attempts"
            )

        # ──────────────────────────────────────────────────────────
        # Step 3: Fill email — human-like typing
        # ──────────────────────────────────────────────────────────
        await random_mouse_jitter(page)
        await human_click_element(page, page.locator(email_sel))
        await random_delay(page, 200, 500)
        await email_input.press("Control+a")
        await human_type(page, creds.email)
        await random_delay(page, 300, 700)

        # Verify the value actually got entered (PKC React may clear it)
        try:
            actual_value = await email_input.input_value(timeout=1000)
            if actual_value != creds.email:
                logger.warning(
                    "PKC sign-in: human_type() produced '%s', expected '%s' — using fill()",
                    actual_value[:20], creds.email[:20],
                )
                await email_input.fill(creds.email)
                await random_delay(page, 200, 400)
        except Exception:
            logger.warning("PKC sign-in: could not verify email input — using fill() as backup")
            await email_input.fill(creds.email)
            await random_delay(page, 200, 400)

        # ──────────────────────────────────────────────────────────
        # Step 4: Fill password — human-like typing
        # ──────────────────────────────────────────────────────────
        pass_found = False
        try:
            await page.locator(pass_sel).first.wait_for(state="visible", timeout=5000)
            pass_found = True
        except Exception:
            pass

        if pass_found:
            await human_click_element(page, page.locator(pass_sel))
            await random_delay(page, 150, 400)
            await human_type(page, creds.password)
            await random_delay(page, 200, 600)

            # Verify password was entered
            try:
                pw_value = await page.locator(pass_sel).first.input_value(timeout=1000)
                if not pw_value:
                    logger.warning("PKC sign-in: human_type() did not fill password — using fill()")
                    await page.locator(pass_sel).first.fill(creds.password)
                    await random_delay(page, 200, 400)
            except Exception:
                pass
        else:
            # Vision fallback for password field
            logger.warning("PKC sign-in: password field not found via selectors — trying vision")
            vision_filled = await self._smart_fill(
                page, "password input field", pass_sel, creds.password,
            )
            if not vision_filled:
                await net_monitor.stop()
                raise RuntimeError("PKC sign-in: password field did not appear")

        # ──────────────────────────────────────────────────────────
        # Step 5: Click Sign In button
        # ──────────────────────────────────────────────────────────
        await wait_for_button_enabled(page, submit_sel, timeout=10000)
        await random_delay(page, 200, 500)

        pre_url = page.url
        await self._multi_strategy_click(page, "Sign In", [
            "Sign In", "Log In", "Continue", "Submit",
        ], submit_sel)

        # ──────────────────────────────────────────────────────────
        # Step 6: Wait for login to complete via network monitoring
        # ──────────────────────────────────────────────────────────
        # PKC login POSTs to /tpci-ecommweb-api/auth/login, then
        # fires profile/cart API calls and tracking events on success.
        auth_ok = await net_monitor.wait_for("pkc_auth_login", expected_count=1, timeout=15000)

        if auth_ok:
            logger.info("PKC login: auth API responded")
            # Give profile/cart calls time to fire (confirms session)
            try:
                await net_monitor.wait_for("pkc_profile", expected_count=1, timeout=5000)
            except Exception:
                pass
            try:
                await net_monitor.wait_for("pkc_cart", expected_count=1, timeout=3000)
            except Exception:
                pass
        else:
            # Fallback: check URL change (successful login navigates away)
            logger.warning("PKC login: auth API not observed — checking URL change")
            await wait_for_url_change(page, pre_url, timeout=10000)

        # Wait for page to settle after login
        await wait_for_page_ready(page, timeout=10000)

        # Check for PerimeterX / bot blocks during login
        if net_monitor.was_blocked():
            blocked = net_monitor.get_blocked_details()
            logger.warning("PKC login: blocked %d request(s) during login", len(blocked))

        await net_monitor.stop()

        # Sweep post-login popups (cookie consent, welcome back, etc.)
        await sweep_popups(page)

        # ──────────────────────────────────────────────────────────
        # Step 7: Verify login success
        # ──────────────────────────────────────────────────────────
        # PKC login is a modal on the homepage — the URL stays on pokemoncenter.com
        # and does NOT redirect to /account. The best success signal is the network
        # monitor: if SSAccountSignInCustomEvent or profile API fired, login succeeded.

        # Best signal: network monitor saw the auth/login API respond successfully
        if auth_ok:
            logger.info("PKC login: success — auth API responded (network monitor confirmed)")
            return

        final_url = page.url

        # Secondary: check if we landed on /account (rare, but possible)
        if "/account" in final_url and "/login" not in final_url:
            logger.info("PKC login: success — redirected to %s", final_url)
            return

        # Tertiary: we're on the homepage — check if the sign-in header element
        # has been replaced by an account indicator (modal closed = success)
        # Look for account-related elements that appear after login
        try:
            acct_indicators = page.locator(
                'a:has-text("My Account"), a[href="/account"], '
                '[class*="account-icon" i], [class*="account-menu" i], '
                'span[class*="header-sign-in" i]:has-text("Hi")'
            )
            if await acct_indicators.first.is_visible(timeout=3000):
                logger.info("PKC login: success — account indicator visible in header")
                return
        except Exception:
            pass

        # Check if sign-in link is still visible (failure indicator)
        try:
            still_sign_in = page.locator(
                'span[class*="header-sign-in" i]:has-text("Sign In"), '
                'a:has-text("Sign In")'
            )
            if await still_sign_in.first.is_visible(timeout=2000):
                logger.warning("PKC login: may have failed — 'Sign In' still visible in header")
            else:
                logger.info("PKC login: success — sign-in link no longer visible")
                return
        except Exception:
            logger.info("PKC login: assuming success (sign-in check inconclusive)")
            return

        # If we got here with no clear success signal, check for error messages
        error_msg = await self._smart_read_error(page)
        if error_msg:
            logger.warning("PKC login: error detected — %s", error_msg)
        else:
            logger.info("PKC login: completed (final URL: %s)", final_url)

    async def _checkout_pokemoncenter(
        self, url: str, product_name: str, profile: Profile, creds: AccountCredentials,
        dry_run: bool = False, **kwargs,
    ) -> CheckoutResult:
        """Pokemon Center checkout flow.

        Uses the same anti-bot approach as Target: homepage warm-up to
        establish tracking cookies, human-like sign-in via modal, then
        proceed to product page for add-to-cart and checkout.
        """
        context = await self._get_context("pokemoncenter")
        page = await context.new_page()

        try:
            # Step 1: Sign in via homepage modal (establishes cookies first)
            await self._sign_in_pokemoncenter(page, creds)
            await self._save_context(context, "pokemoncenter")

            # Step 2: Navigate to product page
            await page.goto(url, wait_until="domcontentloaded")
            await wait_for_page_ready(page, timeout=15000)
            await sweep_popups(page)

            # Human-like: brief page browse before adding to cart
            await random_mouse_jitter(page)
            await idle_scroll(page)
            await random_delay(page, 500, 1500)

            # Step 3: Add to cart
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

            # Step 4: Navigate to cart/checkout
            if not await self._smart_click(
                page, "Go to Cart",
                'a[href*="cart"], a:has-text("Cart"), a:has-text("Bag"), '
                'button:has-text("View Cart"), a:has-text("View Cart")',
            ):
                # Try navigating directly
                await page.goto("https://www.pokemoncenter.com/cart", wait_until="domcontentloaded")
            await wait_for_page_ready(page, timeout=10000)

            if not await self._smart_click(
                page, "Checkout",
                'button:has-text("Checkout"), a:has-text("Checkout"), '
                'button:has-text("Check Out"), a:has-text("Check Out")',
            ):
                raise Exception("Checkout button not found")
            await wait_for_page_ready(page, timeout=15000)

            # Step 5: Handle payment form (PKC requires payment entry every time)
            await self._pkc_fill_checkout_form(page, profile, creds)

            # Step 6: Place order
            if dry_run:
                try:
                    btn = page.locator(
                        'button:has-text("Place Order"), button:has-text("Submit Order"), '
                        'button:has-text("Complete Order"), button:has-text("Pay Now")'
                    )
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
                'button:has-text("Place Order"), button:has-text("Submit Order"), '
                'button:has-text("Complete Order"), button:has-text("Pay Now")',
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

    async def _pkc_fill_checkout_form(self, page, profile: Profile, creds: AccountCredentials):
        """Fill the Pokemon Center checkout form (shipping + payment).

        PKC requires payment entry on every checkout (no saved cards).
        The checkout page has shipping address fields and payment fields
        (card number, expiry, CVV). Shipping address may be pre-filled
        if saved on the account.

        Uses human-like typing and the same anti-bot patterns as the
        login flow.
        """
        await sweep_popups(page)
        await random_mouse_jitter(page)

        # --- Shipping address (fill only if fields are empty) ---
        shipping_fields = [
            ('#firstName, input[name="firstName"], input[name="first_name"], '
             'input[autocomplete="given-name"]', profile.first_name),
            ('#lastName, input[name="lastName"], input[name="last_name"], '
             'input[autocomplete="family-name"]', profile.last_name),
            ('#address1, input[name="address1"], input[name="address_line1"], '
             'input[autocomplete="address-line1"]', profile.address_line1),
            ('#address2, input[name="address2"], input[name="address_line2"], '
             'input[autocomplete="address-line2"]', profile.address_line2),
            ('#city, input[name="city"], input[autocomplete="address-level2"]', profile.city),
            ('#zipCode, input[name="zipCode"], input[name="zip"], input[name="postalCode"], '
             'input[autocomplete="postal-code"]', profile.zip_code),
            ('#phone, input[name="phone"], input[type="tel"], '
             'input[autocomplete="tel"]', profile.phone),
            ('#email, input[name="email"][autocomplete="email"]', profile.email or creds.email),
        ]

        for sel, value in shipping_fields:
            if not value:
                continue
            try:
                field_elem = page.locator(sel).first
                if await field_elem.is_visible(timeout=1000):
                    current_val = await field_elem.input_value(timeout=500)
                    if not current_val:  # only fill if empty (may be pre-filled)
                        await human_click_element(page, field_elem)
                        await random_delay(page, 100, 250)
                        await human_type(page, value)
                        await random_delay(page, 150, 400)
            except Exception:
                continue

        # Handle state dropdown (if present and not pre-filled)
        if profile.state:
            try:
                state_sel = '#state, select[name="state"], select[name="region"], select[autocomplete="address-level1"]'
                state_elem = page.locator(state_sel).first
                if await state_elem.is_visible(timeout=1000):
                    current = await state_elem.input_value(timeout=500)
                    if not current:
                        await state_elem.select_option(value=profile.state)
                        await random_delay(page, 200, 400)
            except Exception:
                pass

        # Click Continue/Next to proceed to payment (if multi-step checkout)
        try:
            continue_btn = page.locator(
                'button:has-text("Continue"), button:has-text("Next"), '
                'button:has-text("Continue to Payment"), '
                'button:has-text("Save & Continue")'
            )
            if await continue_btn.first.is_visible(timeout=3000):
                await human_click_element(page, continue_btn)
                await wait_for_page_ready(page, timeout=10000)
                await random_delay(page, 500, 1000)
        except Exception:
            pass

        await sweep_popups(page)

        # --- Payment information (PKC requires every time) ---
        if not creds.card_number:
            logger.warning("PKC checkout: no card number in credentials — payment form cannot be filled")
            return

        # Card number — may be in an iframe (common with payment processors)
        card_filled = False

        # Strategy 1: Direct input on page
        card_sel = (
            '#cardNumber, input[name="cardNumber"], input[name="card_number"], '
            'input[name="ccnumber"], input[autocomplete="cc-number"], '
            'input[data-testid*="card-number" i], input[placeholder*="Card number" i]'
        )
        try:
            card_input = page.locator(card_sel).first
            if await card_input.is_visible(timeout=3000):
                await human_click_element(page, card_input)
                await random_delay(page, 100, 250)
                await human_type(page, creds.card_number)
                card_filled = True
                await random_delay(page, 200, 500)
        except Exception:
            pass

        # Strategy 2: Payment iframe (Stripe, Braintree, etc.)
        if not card_filled:
            payment_iframe_sels = [
                'iframe[name*="card" i]', 'iframe[name*="payment" i]',
                'iframe[src*="braintree" i]', 'iframe[src*="stripe" i]',
                'iframe[title*="card" i]', 'iframe[title*="payment" i]',
                'iframe[id*="card" i]', 'iframe[id*="braintree" i]',
            ]
            for iframe_sel in payment_iframe_sels:
                try:
                    iframe_elem = page.locator(iframe_sel).first
                    if await iframe_elem.is_visible(timeout=2000):
                        frame = page.frame_locator(iframe_sel)
                        # Card number inside iframe
                        iframe_card_sel = (
                            'input[name="cardnumber"], input[name="card-number"], '
                            'input[autocomplete="cc-number"], input[name="number"], '
                            'input[data-fieldtype="encryptedCardNumber"], '
                            'input[placeholder*="Card" i]'
                        )
                        card_in_frame = frame.locator(iframe_card_sel).first
                        await card_in_frame.wait_for(state="visible", timeout=3000)
                        await card_in_frame.click()
                        await random_delay(page, 100, 250)
                        await card_in_frame.type(creds.card_number, delay=50)
                        card_filled = True

                        # Expiry inside iframe
                        exp_sel = (
                            'input[name="exp-date"], input[name="expiryDate"], '
                            'input[autocomplete="cc-exp"], '
                            'input[data-fieldtype="encryptedExpiryDate"], '
                            'input[placeholder*="MM" i]'
                        )
                        try:
                            exp_input = frame.locator(exp_sel).first
                            if await exp_input.is_visible(timeout=1000):
                                exp_value = f"{creds.card_exp_month}/{creds.card_exp_year[-2:]}" if creds.card_exp_year else creds.card_exp_month
                                await exp_input.click()
                                await random_delay(page, 100, 200)
                                await exp_input.type(exp_value, delay=50)
                        except Exception:
                            pass

                        # CVV inside iframe
                        cvv_sel = (
                            'input[name="cvc"], input[name="cvv"], '
                            'input[autocomplete="cc-csc"], '
                            'input[data-fieldtype="encryptedSecurityCode"], '
                            'input[placeholder*="CVV" i], input[placeholder*="CVC" i]'
                        )
                        try:
                            cvv_input = frame.locator(cvv_sel).first
                            if await cvv_input.is_visible(timeout=1000):
                                await cvv_input.click()
                                await random_delay(page, 100, 200)
                                await cvv_input.type(creds.card_cvv, delay=50)
                        except Exception:
                            pass

                        break
                except Exception:
                    continue

        # Strategy 3: Vision fallback for card number
        if not card_filled:
            logger.info("PKC checkout: card number field not found — trying vision")
            card_filled = await self._smart_fill(
                page, "credit card number input field", card_sel, creds.card_number,
            )

        if not card_filled:
            logger.warning("PKC checkout: could not fill card number")
            return

        # Expiry fields (separate month/year or combined — on main page)
        if creds.card_exp_month:
            # Try combined MM/YY field first
            exp_combined_sel = (
                'input[name="expiry"], input[name="expiryDate"], '
                'input[autocomplete="cc-exp"], input[placeholder*="MM/YY" i], '
                'input[placeholder*="MM / YY" i]'
            )
            exp_filled = False
            try:
                exp_input = page.locator(exp_combined_sel).first
                if await exp_input.is_visible(timeout=1000):
                    exp_value = f"{creds.card_exp_month}/{creds.card_exp_year[-2:]}" if creds.card_exp_year else creds.card_exp_month
                    await human_click_element(page, exp_input)
                    await random_delay(page, 100, 200)
                    await human_type(page, exp_value)
                    exp_filled = True
                    await random_delay(page, 200, 400)
            except Exception:
                pass

            # Try separate month/year fields
            if not exp_filled:
                month_sel = (
                    '#expiryMonth, select[name="expiryMonth"], select[name="exp_month"], '
                    'input[name="expiryMonth"], input[autocomplete="cc-exp-month"], '
                    'select[autocomplete="cc-exp-month"]'
                )
                year_sel = (
                    '#expiryYear, select[name="expiryYear"], select[name="exp_year"], '
                    'input[name="expiryYear"], input[autocomplete="cc-exp-year"], '
                    'select[autocomplete="cc-exp-year"]'
                )
                try:
                    month_elem = page.locator(month_sel).first
                    if await month_elem.is_visible(timeout=1000):
                        tag = await month_elem.evaluate("el => el.tagName.toLowerCase()")
                        if tag == "select":
                            await month_elem.select_option(value=creds.card_exp_month)
                        else:
                            await human_click_element(page, month_elem)
                            await random_delay(page, 100, 200)
                            await human_type(page, creds.card_exp_month)
                        await random_delay(page, 150, 300)
                except Exception:
                    pass

                if creds.card_exp_year:
                    try:
                        year_elem = page.locator(year_sel).first
                        if await year_elem.is_visible(timeout=1000):
                            tag = await year_elem.evaluate("el => el.tagName.toLowerCase()")
                            if tag == "select":
                                # Try full year first, then last 2 digits
                                try:
                                    await year_elem.select_option(value=creds.card_exp_year)
                                except Exception:
                                    await year_elem.select_option(value=creds.card_exp_year[-2:])
                            else:
                                await human_click_element(page, year_elem)
                                await random_delay(page, 100, 200)
                                await human_type(page, creds.card_exp_year)
                            await random_delay(page, 150, 300)
                    except Exception:
                        pass

        # CVV (on main page)
        if creds.card_cvv:
            cvv_sel = (
                '#cvv, #securityCode, input[name="cvv"], input[name="cvc"], '
                'input[name="securityCode"], input[autocomplete="cc-csc"], '
                'input[placeholder*="CVV" i], input[placeholder*="CVC" i], '
                'input[placeholder*="Security" i]'
            )
            try:
                cvv_input = page.locator(cvv_sel).first
                if await cvv_input.is_visible(timeout=2000):
                    await human_click_element(page, cvv_input)
                    await random_delay(page, 100, 200)
                    await human_type(page, creds.card_cvv)
                    await random_delay(page, 200, 400)
            except Exception:
                logger.warning("PKC checkout: could not fill CVV field")

        # Cardholder name (if field exists)
        if creds.card_name:
            name_sel = (
                '#cardholderName, input[name="cardholderName"], input[name="name"], '
                'input[autocomplete="cc-name"], input[placeholder*="Name on card" i], '
                'input[placeholder*="Cardholder" i]'
            )
            try:
                name_input = page.locator(name_sel).first
                if await name_input.is_visible(timeout=1000):
                    await human_click_element(page, name_input)
                    await random_delay(page, 100, 200)
                    await human_type(page, creds.card_name)
                    await random_delay(page, 200, 400)
            except Exception:
                pass

        # Billing address — check "same as shipping" checkbox first
        try:
            same_as_shipping = page.locator(
                'input[type="checkbox"][name*="billing" i], '
                'input[type="checkbox"][id*="sameAs" i], '
                'label:has-text("Same as shipping")'
            )
            if await same_as_shipping.first.is_visible(timeout=1000):
                is_checked = await same_as_shipping.first.is_checked()
                if not is_checked:
                    await human_click_element(page, same_as_shipping)
                    await random_delay(page, 200, 400)
        except Exception:
            pass

        logger.info("PKC checkout: payment form filled")

    async def _bestbuy_handle_verification(self, page, creds: AccountCredentials, profile: Profile):
        """Handle Best Buy's identity verification step.

        After submitting the email, Best Buy may ask for the last 4 digits of
        the phone number on the account and the account holder's last name
        before showing the password field.
        """
        # Detect the verification page — look for phone last 4 or last name fields
        verification_selectors = (
            'input[id*="phone" i], input[name*="phone" i], '
            'input[id*="last4" i], input[name*="last4" i], '
            'input[id*="lastDigits" i], input[name*="lastDigits" i], '
            'input[id*="phoneLast" i], input[name*="phoneLast" i]'
        )
        last_name_selectors = (
            'input[id*="lastName" i], input[name*="lastName" i], '
            'input[id*="last_name" i], input[name*="last_name" i], '
            'input[id*="familyName" i], input[name*="familyName" i]'
        )

        phone_field_found = False
        try:
            phone_loc = page.locator(verification_selectors)
            phone_field_found = await phone_loc.first.is_visible(timeout=3000)
        except Exception:
            pass

        # Also check if password field or auth picker is already visible —
        # if so, skip the expensive vision call and return immediately.
        if not phone_field_found:
            try:
                pw_visible = await page.locator('input#fld-p1, input[type="password"]').first.is_visible(timeout=500)
                if pw_visible:
                    logger.info("Best Buy verification: password field already visible, skipping")
                    return
            except Exception:
                pass
            try:
                picker_visible = await page.locator(
                    'text=/choose.*sign.?in/i, text=/use password/i, '
                    'text=/one-time code/i, label:has-text("Use password")'
                ).first.is_visible(timeout=500)
                if picker_visible:
                    logger.info("Best Buy verification: auth picker visible, skipping verification")
                    return
            except Exception:
                pass

            # Vision fallback only if no other UI elements detected
            screenshot = await self._screenshot_b64(page)
            answer = self._ask_vision(
                screenshot,
                'Does this page ask for "last 4 digits of phone number" and/or "last name" '
                'as an identity verification step? Return ONLY JSON: '
                '{"verification": true/false, "phone_field": {"x": N, "y": N}, "last_name_field": {"x": N, "y": N}, "submit": {"x": N, "y": N}}. '
                'Use null coordinates if a field is not visible.',
            )
            if answer:
                try:
                    result = json.loads(answer.strip())
                    if result.get("verification"):
                        phone_last4 = creds.phone_last4 or (profile.phone[-4:] if len(profile.phone) >= 4 else "")
                        last_name = creds.account_last_name or profile.last_name

                        if not phone_last4 or not last_name:
                            logger.warning("Best Buy verification: missing phone_last4 or last_name — cannot complete verification")
                            return

                        # Fill phone last 4 via vision coordinates
                        phone_coords = result.get("phone_field", {})
                        if phone_coords.get("x") is not None:
                            await human_click(page, int(phone_coords["x"]), int(phone_coords["y"]))
                            await random_delay(page, 100, 250)
                            await human_type(page, phone_last4)
                            await random_delay(page, 300, 600)

                        # Fill last name via vision coordinates
                        name_coords = result.get("last_name_field", {})
                        if name_coords.get("x") is not None:
                            await human_click(page, int(name_coords["x"]), int(name_coords["y"]))
                            await random_delay(page, 100, 250)
                            await human_type(page, last_name)
                            await random_delay(page, 300, 600)

                        # Submit
                        submit_coords = result.get("submit", {})
                        if submit_coords.get("x") is not None:
                            await human_click(page, int(submit_coords["x"]), int(submit_coords["y"]))
                        else:
                            await self._multi_strategy_click(page, "Continue", [
                                "Continue", "Verify", "Submit", "Next",
                            ], 'button[type="submit"], button:has-text("Continue"), button:has-text("Verify")')
                        await wait_for_page_ready(page, timeout=10000)
                except (json.JSONDecodeError, TypeError, ValueError) as e:
                    logger.warning("Best Buy verification vision parse error: %s", e)
            return

        # Selector-based verification flow
        phone_last4 = creds.phone_last4 or (profile.phone[-4:] if len(profile.phone) >= 4 else "")
        last_name = creds.account_last_name or profile.last_name

        if not phone_last4 or not last_name:
            logger.warning("Best Buy verification: missing phone_last4 (%s) or last_name (%s)", bool(phone_last4), bool(last_name))
            return

        logger.info("Best Buy: identity verification step detected — filling phone last 4 + last name")

        # Fill phone last 4 digits
        await self._smart_fill(page, "phone last 4 digits", verification_selectors, phone_last4)
        await random_delay(page, 300, 600)

        # Fill last name
        await self._smart_fill(page, "last name", last_name_selectors, last_name)
        await random_delay(page, 300, 600)

        # Submit verification
        await wait_for_button_enabled(page, 'button[type="submit"]', timeout=10000)
        await self._multi_strategy_click(page, "Continue", [
            "Continue", "Verify", "Submit", "Next",
        ], 'button[type="submit"], button:has-text("Continue"), button:has-text("Verify")')
        await wait_for_page_ready(page, timeout=10000)

    async def _wait_for_otp_code(
        self, page, user_id: int | None, retailer: str,
        product_name: str, url: str,
        timeout_seconds: int = 300,
    ) -> CheckoutResult | None:
        """Wait for user to submit an OTP code via the dashboard or phone shortcut.

        Creates a DB OTP request, sends a Discord notification, then polls the DB
        for the submitted code. When received, enters it into the page.

        Returns None on success (code entered, continue checkout).
        Returns a CheckoutResult on failure (timeout, no user_id, etc.).
        """
        if user_id is None:
            logger.error("%s: OTP required but no user_id available — cannot create OTP request", retailer)
            return CheckoutResult(
                url=url, retailer=retailer, product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=(
                    f"{retailer.title()} is requesting a verification code but no user "
                    "context is available. Try checkout from the dashboard."
                ),
            )

        logger.info("%s: OTP code requested — creating OTP relay request for user %d", retailer, user_id)
        otp_id = db.create_otp_request(user_id, retailer, context=f"checkout:{product_name}")

        # Send Discord notification if configured
        try:
            settings = db.get_user_settings(user_id)
            webhook_url = settings.get("discord_webhook", "")
            if webhook_url:
                import httpx
                server_url = os.environ.get("PMON_SERVER_URL", "")
                api_key = settings.get("api_key", "")
                submit_url = f"{server_url}/api/otp/submit?key={api_key}&code=" if server_url and api_key else ""
                embed = {
                    "title": f"🔐 Verification Code Needed: {retailer.title()}",
                    "description": (
                        f"**{product_name}** checkout needs a verification code.\n\n"
                        f"Enter it in the dashboard or reply with your phone shortcut."
                    ),
                    "color": 0xFFA500,
                    "fields": [],
                }
                if submit_url:
                    embed["fields"].append({
                        "name": "Phone Shortcut URL",
                        "value": f"`{submit_url}YOUR_CODE`",
                    })
                embed["fields"].append({
                    "name": "Expires",
                    "value": "5 minutes",
                    "inline": True,
                })
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.post(webhook_url, json={"embeds": [embed]})
        except Exception as e:
            logger.warning("Failed to send OTP Discord notification: %s", e)

        # Poll for the code
        import time
        deadline = time.time() + timeout_seconds
        poll_interval = 3  # seconds
        while time.time() < deadline:
            code = db.get_otp_code(otp_id)
            if code:
                logger.info("%s: OTP code received for request %d, entering it now", retailer, otp_id)
                # Find the OTP input field and enter the code
                try:
                    otp_input = page.locator(
                        'input[type="text"], input[type="tel"], input[type="number"], '
                        'input[inputmode="numeric"], input[autocomplete="one-time-code"]'
                    ).first
                    await otp_input.click()
                    await otp_input.fill(code)
                    await asyncio.sleep(0.5)

                    # Verify the code was actually filled (Best Buy can clear it)
                    try:
                        filled_value = await otp_input.input_value(timeout=1000)
                        if not filled_value:
                            logger.warning("%s: OTP input empty after fill — retrying with type()", retailer)
                            await otp_input.click()
                            await otp_input.type(code, delay=80)
                            await asyncio.sleep(0.3)
                    except Exception:
                        pass

                    # Submit the OTP — try button click first, then Enter, then Tab+Enter
                    otp_page_indicators = (
                        'text=/one-time code/i, text=/enter your code/i, '
                        'text=/enter the code/i, text=/verification code/i, '
                        'text=/enter your one-time/i'
                    )

                    async def _otp_submit_button():
                        return await self._multi_strategy_click(page, "Verify Code", [
                            "Continue", "Verify", "Verify Code", "Submit", "Sign In",
                        ], 'button[type="submit"], input[type="submit"], button:has-text("Continue"), button:has-text("Verify"), button:has-text("Sign In")')

                    async def _otp_submit_enter():
                        await otp_input.press("Enter")

                    async def _otp_submit_tab_enter():
                        await otp_input.press("Tab")
                        await asyncio.sleep(0.2)
                        await page.keyboard.press("Enter")

                    submitted = False
                    for attempt_label, submit_action in [
                        ("button click", _otp_submit_button),
                        ("Enter key", _otp_submit_enter),
                        ("Tab+Enter", _otp_submit_tab_enter),
                    ]:
                        try:
                            await submit_action()
                        except Exception:
                            pass
                        await wait_for_page_ready(page, timeout=8000)

                        # Check if we left the OTP page
                        try:
                            still_on_otp = await page.locator(otp_page_indicators).first.is_visible(timeout=2000)
                        except Exception:
                            still_on_otp = False

                        if not still_on_otp:
                            submitted = True
                            logger.info("%s: OTP submitted successfully via %s", retailer, attempt_label)
                            break
                        logger.warning("%s: still on OTP page after %s — trying next method", retailer, attempt_label)

                    if not submitted:
                        logger.warning("%s: OTP may not have submitted — all methods tried, proceeding anyway", retailer)
                    return None  # Continue checkout
                except Exception as e:
                    logger.error("%s: failed to enter OTP code: %s", retailer, e)
                    db.expire_otp_request(otp_id)
                    return CheckoutResult(
                        url=url, retailer=retailer, product_name=product_name,
                        status=CheckoutStatus.FAILED,
                        error_message=f"Received OTP code but failed to enter it: {e}",
                    )
            await asyncio.sleep(poll_interval)

        # Timed out
        logger.warning("%s: OTP code not received within %d seconds", retailer, timeout_seconds)
        db.expire_otp_request(otp_id)
        return CheckoutResult(
            url=url, retailer=retailer, product_name=product_name,
            status=CheckoutStatus.FAILED,
            error_message=(
                f"{retailer.title()} is requesting a verification code. "
                f"No code was submitted within {timeout_seconds // 60} minutes. "
                "Check your texts and submit the code via the dashboard or phone shortcut."
            ),
        )

    async def _checkout_bestbuy(
        self, url: str, product_name: str, profile: Profile, creds: AccountCredentials,
        dry_run: bool = False, user_id: int | None = None,
    ) -> CheckoutResult:
        """Best Buy checkout flow (limited due to invitation system)."""
        context = await self._get_context("bestbuy")
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded")
            await wait_for_page_ready(page, timeout=10000)

            # Check for invitation system — use vision as backup detector
            invite_text = await page.locator('text=/invitation|ask for an invite|exclusive sales event/i').count()
            if invite_text > 0:
                return CheckoutResult(
                    url=url,
                    retailer="bestbuy",
                    product_name=product_name,
                    status=CheckoutStatus.FAILED,
                    error_message="Product uses Best Buy invitation system - auto-checkout not possible",
                )

            # Human-like: browse the product page before interacting
            await sweep_popups(page)
            await random_mouse_jitter(page)
            await idle_scroll(page)
            await random_delay(page, 500, 1500)

            # Standard add to cart
            await sweep_popups(page)
            if not await self._smart_click(
                page, "Add to Cart",
                'button.add-to-cart-button:not([disabled]), button.btn-primary.add-to-cart-button',
            ):
                # Popup may have blocked the click — sweep and retry
                if await sweep_popups(page):
                    await self._smart_click(
                        page, "Add to Cart",
                        'button.add-to-cart-button:not([disabled]), button.btn-primary.add-to-cart-button',
                    )
                else:
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

            # Sweep popups after add-to-cart (protection plan offers, etc.)
            await sweep_popups(page)

            # Go to cart via popup button or direct navigation
            if not await self._smart_click(
                page, "Go to Cart",
                'div.go-to-cart-button a, a:has-text("Go to Cart"), a[href*="/cart"]',
                timeout=3000,
            ):
                await page.goto("https://www.bestbuy.com/cart", wait_until="domcontentloaded")
                await wait_for_page_ready(page, timeout=10000)

            # Sweep popups on cart page
            await sweep_popups(page)

            # Click checkout
            if not await self._smart_click(
                page, "Checkout",
                'button[data-track="Checkout - Top"], button:has-text("Checkout"), a:has-text("Checkout")',
            ):
                raise Exception("Checkout button not found")
            await wait_for_page_ready(page, timeout=10000)
            await sweep_popups(page)

            # Sign in if needed — selectors first (human-like via _smart_fill), vision fallback
            email_filled = await self._smart_fill(
                page, "email", 'input#fld-e, input[id="user.emailAddress"], input[type="email"], input[name="email"]', creds.email, timeout=3000,
            )
            if email_filled:
                # Start network monitor to track login completion
                # Best Buy patterns (bb_auth, bb_token, bb_account_menu, etc.)
                # are pre-configured in NetworkMonitor
                net_monitor = NetworkMonitor(page)
                net_monitor.add_pattern("bb_signin_page", "/identity/signin")
                await net_monitor.start()

                # Human-like: jitter before interacting with login form
                await random_mouse_jitter(page)

                # Verify the email value got entered
                try:
                    actual = await page.locator('input#fld-e, input[type="email"]').first.input_value(timeout=1000)
                    if actual != creds.email:
                        logger.warning("Best Buy sign-in: email value mismatch — using fill()")
                        await page.locator('input#fld-e, input[type="email"]').first.fill(creds.email)
                        await random_delay(page, 200, 400)
                except Exception:
                    pass

                # Submit email first (Best Buy uses multi-step sign-in)
                await wait_for_button_enabled(page, 'button[type="submit"]', timeout=10000)
                await self._multi_strategy_click(page, "Continue", [
                    "Continue", "Sign In", "Next",
                ], 'button[type="submit"], button:has-text("Continue"), button:has-text("Sign In")')
                await wait_for_page_ready(page, timeout=10000)

                # Sweep popups after email submit
                await sweep_popups(page)

                # Best Buy's post-email flow varies: it may show verification
                # (phone last 4 + last name), auth method picker, or password
                # field directly — in any order.  Detect what's on the page and
                # handle whichever step appears, then check again after.
                await self._bestbuy_handle_verification(page, creds, profile)

                # --- Auth method picker: Best Buy may show "Choose a sign-in method" ---
                # or it may auto-send a one-time code (landing on OTP page directly).
                # Must select "Use password" before filling the password field.
                # Best Buy uses styled radio buttons where <input type="radio"> is
                # visually hidden and the <label> is what the user sees/clicks.
                pw_option_clicked = False

                # Check if password field is already visible (no picker needed)
                try:
                    if await page.locator('input#fld-p1, input[type="password"]').first.is_visible(timeout=2000):
                        pw_option_clicked = True
                        logger.info("Best Buy sign-in: password field already visible")
                except Exception:
                    pass

                # Detect if we landed on the OTP page directly (no auth picker shown)
                # If so, look for a link to switch to password sign-in
                if not pw_option_clicked:
                    otp_page = False
                    try:
                        otp_page = await page.locator(
                            'text=/one-time code/i, text=/enter your code/i, '
                            'text=/enter the code/i, text=/verification code/i'
                        ).first.is_visible(timeout=1500)
                    except Exception:
                        pass

                    if otp_page:
                        logger.info("Best Buy sign-in: landed on OTP page — looking for password sign-in link")
                        # Try to find "Try another way", "Other sign-in options", "Use password instead" etc.
                        try:
                            switch_clicked = await page.evaluate("""() => {
                                const targets = ['try another way', 'other sign-in', 'sign in another way',
                                    'use password', 'different method', 'other options', 'more sign-in options',
                                    'back to sign-in', 'sign-in options'];
                                const els = document.querySelectorAll('a, button, span[role="button"], div[role="button"], [tabindex]');
                                for (const el of els) {
                                    const text = (el.textContent || '').toLowerCase().trim();
                                    for (const target of targets) {
                                        if (text.includes(target) && el.offsetParent !== null) {
                                            el.click();
                                            return el.tagName + ': ' + text.substring(0, 60);
                                        }
                                    }
                                }
                                return null;
                            }""")
                            if switch_clicked:
                                logger.info("Best Buy sign-in: clicked switch link on OTP page: %s", switch_clicked)
                                await wait_for_page_ready(page, timeout=5000)
                                await random_delay(page, 500, 1000)
                            else:
                                # Vision fallback for the switch link
                                switch_clicked = await self._smart_click(
                                    page,
                                    "Link or button to 'Try another way' or 'Use password instead' or 'Other sign-in options' to switch away from one-time code",
                                    'a:has-text("another"), a:has-text("password"), a:has-text("options")',
                                    timeout=3000,
                                )
                                if switch_clicked:
                                    await wait_for_page_ready(page, timeout=5000)
                                    await random_delay(page, 500, 1000)
                        except Exception as e:
                            logger.debug("Best Buy sign-in: OTP switch link search failed: %s", e)

                        # OTP switch didn't work — fall through to "Use password" strategies below
                        logger.info("Best Buy sign-in: OTP switch links not found, will try 'Use password' strategies")

                # Now try to click "Use password" option (auth method picker page)
                otp_relay_handled = False
                if not pw_option_clicked:
                    # Strategy 1: JS click — comprehensive search for password option.
                    # Best Buy uses styled radio buttons where <input type="radio"> is
                    # visually hidden and the <label> is what the user sees/clicks.
                    # Also handles cases where the option is a div, span, or other element.
                    try:
                        clicked_js = await page.evaluate("""() => {
                            // Try 1: labels with password-related text (Best Buy's primary pattern)
                            const labels = document.querySelectorAll('label');
                            for (const label of labels) {
                                const text = (label.textContent || '').trim().toLowerCase();
                                if (text === 'use password' || text === 'password'
                                    || text.includes('sign in with password')
                                    || text.includes('use a password')
                                    || text.includes('use your password')) {
                                    label.click();
                                    return 'LABEL: ' + label.textContent.trim().substring(0, 60);
                                }
                            }
                            // Try 2: radio inputs with password-related value/name/id
                            const radios = document.querySelectorAll('input[type="radio"]');
                            for (const radio of radios) {
                                const val = (radio.value || '').toLowerCase();
                                const name = (radio.name || '').toLowerCase();
                                const id = radio.id || '';
                                if (val.includes('password') || name.includes('password') || id.toLowerCase().includes('password')) {
                                    const label = document.querySelector('label[for="' + id + '"]');
                                    if (label) { label.click(); return 'LABEL[for]: ' + label.textContent.trim().substring(0, 40); }
                                    radio.click();
                                    radio.dispatchEvent(new Event('change', {bubbles: true}));
                                    return 'RADIO: value=' + val + ' id=' + id;
                                }
                            }
                            // Try 3: any clickable element with password-related text (not "forgot password")
                            const allEls = document.querySelectorAll('label, span, div, a, button, li, p, [role="radio"], [role="option"], [role="tab"], [tabindex]');
                            const pwPhrases = ['use password', 'use a password', 'use your password',
                                'sign in with password', 'sign in with a password', 'password'];
                            for (const el of allEls) {
                                const text = (el.textContent || '').trim().toLowerCase();
                                if (el.offsetParent === null) continue;
                                if (text.includes('forgot')) continue;
                                // Exact match or known phrase match
                                for (const phrase of pwPhrases) {
                                    if (text === phrase || text.startsWith(phrase)) {
                                        el.click();
                                        return el.tagName + ': ' + (el.textContent || '').trim().substring(0, 60);
                                    }
                                }
                            }
                            // Try 4: data attributes containing "password" on interactive elements
                            const dataEls = document.querySelectorAll('[data-track*="password" i], [data-value*="password" i], [data-method*="password" i], [data-option*="password" i], [value*="password" i]');
                            for (const el of dataEls) {
                                if (el.offsetParent !== null || el.type === 'radio') {
                                    const label = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
                                    if (label) { label.click(); return 'DATA-LABEL: ' + label.textContent.trim().substring(0, 40); }
                                    el.click();
                                    if (el.type === 'radio') el.dispatchEvent(new Event('change', {bubbles: true}));
                                    return 'DATA-ATTR: ' + el.tagName + ' ' + (el.textContent || el.value || '').substring(0, 40);
                                }
                            }
                            return null;
                        }""")
                        if clicked_js:
                            pw_option_clicked = True
                            logger.info("Best Buy sign-in: clicked 'Use password' via JS: %s", clicked_js)
                    except Exception as e:
                        logger.debug("Best Buy sign-in: JS click failed: %s", e)

                    # Strategy 2: Playwright label locator with multiple text variants
                    if not pw_option_clicked:
                        for label_text in ["Use password", "Password", "Use a password", "Use your password"]:
                            try:
                                label = page.locator(f'label:has-text("{label_text}")')
                                if await label.first.is_visible(timeout=800):
                                    await human_click_element(page, label)
                                    pw_option_clicked = True
                                    logger.info("Best Buy sign-in: clicked '%s' via label locator", label_text)
                                    break
                            except Exception:
                                continue

                    # Strategy 3: get_by_label with force check (for hidden radios)
                    if not pw_option_clicked:
                        for label_text in ["Use password", "Password"]:
                            try:
                                opt = page.get_by_label(label_text, exact=False)
                                await opt.first.check(timeout=1500, force=True)
                                pw_option_clicked = True
                                logger.info("Best Buy sign-in: checked '%s' via get_by_label", label_text)
                                break
                            except Exception:
                                continue

                    # Strategy 4: get_by_text with multiple variants
                    if not pw_option_clicked:
                        for pw_text in ["Use password", "Password", "Use a password", "Sign in with password"]:
                            try:
                                opt = page.get_by_text(pw_text, exact=True)
                                if await opt.first.is_visible(timeout=500):
                                    await human_click_element(page, opt)
                                    pw_option_clicked = True
                                    logger.info("Best Buy sign-in: clicked '%s' via get_by_text", pw_text)
                                    break
                            except Exception:
                                continue

                    # Strategy 5: get_by_role for radio/tab/option with password text
                    if not pw_option_clicked:
                        for role in ["radio", "tab", "option", "button", "link"]:
                            for pw_text in ["Use password", "Password"]:
                                try:
                                    opt = page.get_by_role(role, name=pw_text, exact=False)
                                    if await opt.first.is_visible(timeout=500):
                                        await opt.first.click(force=True)
                                        pw_option_clicked = True
                                        logger.info("Best Buy sign-in: clicked '%s' via get_by_role('%s')", pw_text, role)
                                        break
                                except Exception:
                                    continue
                            if pw_option_clicked:
                                break

                    # Strategy 6: Vision fallback — screenshot the page and ask for "Use password"
                    if not pw_option_clicked:
                        logger.info("Best Buy sign-in: trying vision for 'Use password' option")
                        pw_option_clicked = await self._smart_click(
                            page,
                            "The 'Use password' or 'Password' option/radio button/tab to select password-based sign-in instead of one-time code. Click the password option, NOT the password input field.",
                            'label:has-text("password"), [role="radio"]:has-text("password"), [role="tab"]:has-text("password")',
                            timeout=5000,
                        )

                if pw_option_clicked:
                    # Wait for password field to appear after selecting "Use password"
                    try:
                        await page.locator('input#fld-p1, input[type="password"]').first.wait_for(
                            state="visible", timeout=5000
                        )
                        logger.info("Best Buy sign-in: password field appeared after selecting 'Use password'")
                    except Exception:
                        logger.warning("Best Buy sign-in: password field did not appear after selecting 'Use password'")
                        # Verification may appear AFTER selecting password auth method
                        await self._bestbuy_handle_verification(page, creds, profile)
                    await random_delay(page, 300, 700)
                else:
                    logger.warning("Best Buy sign-in: could not select password sign-in method")
                    # Dump page diagnostics for debugging
                    try:
                        diag = await page.evaluate("""() => {
                            const info = {url: location.href, title: document.title};
                            const els = document.querySelectorAll('label, a, button, [role="radio"], [role="tab"], [role="option"], [role="button"], input[type="radio"], [tabindex]');
                            info.interactive = [];
                            for (const el of els) {
                                const text = (el.textContent || '').trim().substring(0, 80);
                                if (!text) continue;
                                info.interactive.push({
                                    tag: el.tagName, role: el.getAttribute('role'),
                                    type: el.type || null, id: el.id || null,
                                    text: text, visible: el.offsetParent !== null,
                                });
                            }
                            const headings = document.querySelectorAll('h1, h2, h3');
                            info.headings = Array.from(headings).map(h => h.textContent.trim().substring(0, 100));
                            return info;
                        }""")
                        logger.info("Best Buy sign-in: auth picker page diagnostics: %s", diag)
                    except Exception:
                        pass

                    # If we're stuck on the OTP page and can't switch to password,
                    # try the OTP relay as a last resort
                    still_on_otp = False
                    try:
                        still_on_otp = await page.locator(
                            'text=/one-time code/i, text=/enter your code/i, '
                            'text=/enter the code/i, text=/verification code/i'
                        ).first.is_visible(timeout=1000)
                    except Exception:
                        pass

                    if still_on_otp:
                        logger.info("Best Buy sign-in: still on OTP page after all strategies — trying OTP relay")
                        otp_result = await self._wait_for_otp_code(
                            page, user_id, "bestbuy", product_name, url,
                        )
                        if otp_result is not None:
                            return otp_result
                        # OTP entered successfully — skip password entry, go to post-login flow
                        otp_relay_handled = True

                # Now look for the password field (skip if OTP relay already handled auth)
                pass_filled = False
                if not otp_relay_handled:
                    pass_filled = await self._smart_fill(
                        page, "password", 'input#fld-p1, input[type="password"], input[name="password"]', creds.password, timeout=5000,
                    )
                if pass_filled:
                    # Verify password was entered
                    try:
                        pw_value = await page.locator('input#fld-p1, input[type="password"]').first.input_value(timeout=1000)
                        if not pw_value:
                            logger.warning("Best Buy sign-in: password empty — using fill()")
                            await page.locator('input#fld-p1, input[type="password"]').first.fill(creds.password)
                            await random_delay(page, 200, 400)
                    except Exception:
                        pass

                    await wait_for_button_enabled(page, 'button[type="submit"]', timeout=10000)

                    pre_url = page.url
                    await self._multi_strategy_click(page, "Sign In", [
                        "Sign In", "Log In", "Continue",
                    ], 'button[type="submit"], button:has-text("Sign In")')

                    # Wait for login to complete via network monitoring
                    # Uses Best Buy-specific detection: /identity/authenticate,
                    # /oauth/token, account-menu, and welcome-back-toast signals
                    login_done = await net_monitor.wait_for_login_complete(
                        timeout=20000, retailer="bestbuy"
                    )
                    if not login_done:
                        logger.warning("Best Buy sign-in: network login signals not detected, falling back to URL change")
                        await wait_for_url_change(page, pre_url, timeout=10000)

                # Check if blocked during login
                if net_monitor.was_blocked():
                    blocked = net_monitor.get_blocked_details()
                    logger.warning("Best Buy sign-in: blocked %d request(s) during login", len(blocked))

                # Check for reCAPTCHA challenge that may have fired
                recaptcha_count = net_monitor.response_count("bb_recaptcha")
                if recaptcha_count > 0:
                    logger.info("Best Buy sign-in: reCAPTCHA Enterprise fired %d time(s) during login", recaptcha_count)

                # Verify login success via post-login indicators
                account_menu_loaded = net_monitor.response_count("bb_account_menu") > 0
                welcome_back_loaded = net_monitor.response_count("bb_welcome_back") > 0
                if account_menu_loaded or welcome_back_loaded:
                    logger.info(
                        "Best Buy sign-in: post-login confirmation — account_menu=%s, welcome_back=%s",
                        account_menu_loaded, welcome_back_loaded,
                    )

                await net_monitor.stop()

                # --- Post-login OTP detection + relay ---
                # Best Buy may require a one-time code AFTER password submission.
                # Skip if OTP relay already handled the auth during pre-login.
                if not otp_relay_handled:
                    try:
                        post_login_otp = await page.locator(
                            'text=/one-time code/i, text=/enter your code/i, '
                            'text=/enter the code/i, text=/verification code/i, '
                            'text=/enter your one-time/i'
                        ).first.is_visible(timeout=2000)
                    except Exception:
                        post_login_otp = False

                    if post_login_otp:
                        otp_result = await self._wait_for_otp_code(
                            page, user_id, "bestbuy", product_name, url,
                        )
                        if otp_result is not None:
                            return otp_result

                # Sweep post-login popups
                await sweep_popups(page)

                # Save cookies immediately after sign-in so they persist
                # even if checkout fails later (add-to-cart, queue, etc.)
                await self._save_context(context, "bestbuy")
            else:
                # Try full vision-assisted sign-in
                await self._smart_sign_in(page, creds, "bestbuy")

            # Guest checkout fallback
            await self._smart_click(
                page, "Continue as Guest",
                'button.cia-guest-content__continue.guest, button:has-text("Continue as Guest"), button:has-text("Guest")',
                timeout=2000,
            )

            # Sweep popups before placing order
            await sweep_popups(page)

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
                # Best Buy uses a queue/waiting room system ("fast-track")
                # after clicking "Place Your Order". The flow is:
                #   1. Click "Place Your Order"
                #   2. Redirect to /checkout/c/fast-track (queue)
                #   3. Wait in queue (seconds to minutes)
                #   4. Redirect to /checkout/thank-you?orderId=... (confirmation)
                # We must wait for the thank-you page, not just page ready.
                order_number = ""
                order_confirmed = False
                queue_timeout_ms = 300000  # 5 minutes max for queue

                try:
                    # Wait for either thank-you page or an error
                    # The queue page auto-refreshes/redirects when your turn comes
                    logger.info("Best Buy checkout: waiting for order confirmation (queue may take up to 5 minutes)")
                    await page.wait_for_url(
                        "**/checkout/thank-you**",
                        timeout=queue_timeout_ms,
                        wait_until="domcontentloaded",
                    )
                    order_confirmed = True

                    # Extract order ID from URL
                    current_url = page.url
                    import re as _re
                    order_match = _re.search(r'orderId=([a-f0-9-]+)', current_url)
                    if order_match:
                        order_number = order_match.group(1)
                        logger.info("Best Buy checkout: order confirmed! Order ID: %s", order_number)
                    else:
                        logger.info("Best Buy checkout: thank-you page reached but no orderId in URL")

                except Exception as e:
                    # Check if we're still on queue page or got an error
                    current_url = page.url
                    if "thank-you" in current_url:
                        order_confirmed = True
                        order_match = _re.search(r'orderId=([a-f0-9-]+)', current_url)
                        if order_match:
                            order_number = order_match.group(1)
                    elif "fast-track" in current_url:
                        logger.warning("Best Buy checkout: queue timed out after %ds", queue_timeout_ms // 1000)
                    else:
                        logger.warning("Best Buy checkout: unexpected page after placing order: %s (%s)", current_url, e)

                await self._save_context(context, "bestbuy")

                if order_confirmed:
                    return CheckoutResult(
                        url=url,
                        retailer="bestbuy",
                        product_name=product_name,
                        status=CheckoutStatus.SUCCESS,
                        order_number=order_number,
                    )
                else:
                    # Queue timed out or unexpected state — still might have gone through
                    error = await self._smart_read_error(page)
                    return CheckoutResult(
                        url=url,
                        retailer="bestbuy",
                        product_name=product_name,
                        status=CheckoutStatus.FAILED,
                        error_message=error or f"Queue wait timed out — check order status manually (last URL: {page.url})",
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
            # Save cookies even on failure so sign-in session persists
            try:
                await self._save_context(context, "bestbuy")
            except Exception:
                pass
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

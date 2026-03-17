"""Human-like browser behavior utilities.

Shared between CheckoutEngine and dashboard test login to make bot
interactions appear natural to PerimeterX, FullStory, and other
bot-detection systems.

Usage:
    from pmon.checkout.human_behavior import (
        human_mouse_move, human_click, human_type, idle_scroll,
        random_delay, wait_for_page_ready, wait_for_button_enabled,
        sweep_popups,
    )

    # Move mouse like a human and click
    await human_click(page, 683, 412)

    # Type with variable speed
    await human_type(page, "user@example.com")

    # Wait for a grayed-out button to become clickable
    await wait_for_button_enabled(page, 'button[type="submit"]')
"""

from __future__ import annotations

import asyncio
import logging
import math
import random

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mouse movement
# ---------------------------------------------------------------------------

async def _get_mouse_position(page) -> tuple[float, float]:
    """Best-effort read of the current mouse position.

    Playwright doesn't expose cursor position directly, so we track it via
    a tiny JS snippet.  Falls back to (0, 0) on first call.
    """
    try:
        pos = await page.evaluate("""() => {
            return {x: window.__pmon_mx || 0, y: window.__pmon_my || 0};
        }""")
        return pos["x"], pos["y"]
    except Exception:
        return 0.0, 0.0


async def _install_mouse_tracker(page) -> None:
    """Inject a one-time mousemove listener to track cursor position."""
    try:
        await page.evaluate("""() => {
            if (!window.__pmon_mouse_installed) {
                document.addEventListener('mousemove', e => {
                    window.__pmon_mx = e.clientX;
                    window.__pmon_my = e.clientY;
                });
                window.__pmon_mouse_installed = true;
            }
        }""")
    except Exception:
        pass


def _bezier_point(t: float, p0: float, p1: float, p2: float, p3: float) -> float:
    """Cubic Bezier interpolation at parameter *t*."""
    u = 1 - t
    return u * u * u * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t * t * t * p3


async def human_mouse_move(page, target_x: float, target_y: float, *, steps: int | None = None) -> None:
    """Move the mouse from its current position to (*target_x*, *target_y*)
    along a slightly curved, human-like path using a cubic Bezier curve.

    Parameters
    ----------
    page : playwright Page
    target_x, target_y : destination coordinates
    steps : number of intermediate move events (auto-calculated if None)
    """
    await _install_mouse_tracker(page)
    sx, sy = await _get_mouse_position(page)

    distance = math.hypot(target_x - sx, target_y - sy)
    if distance < 5:
        # Already close enough, just move directly
        await page.mouse.move(target_x, target_y)
        return

    if steps is None:
        steps = max(8, min(40, int(distance / 30)))

    # Generate two random control points that create a gentle curve
    mid_x = (sx + target_x) / 2
    mid_y = (sy + target_y) / 2
    spread = distance * 0.15  # How far control points deviate from the straight line

    cp1_x = sx + (mid_x - sx) * 0.4 + random.uniform(-spread, spread)
    cp1_y = sy + (mid_y - sy) * 0.4 + random.uniform(-spread, spread)
    cp2_x = mid_x + (target_x - mid_x) * 0.6 + random.uniform(-spread, spread)
    cp2_y = mid_y + (target_y - mid_y) * 0.6 + random.uniform(-spread, spread)

    for i in range(1, steps + 1):
        t = i / steps
        x = _bezier_point(t, sx, cp1_x, cp2_x, target_x)
        y = _bezier_point(t, sy, cp1_y, cp2_y, target_y)

        # Small jitter on intermediate points (not the final one)
        if i < steps:
            x += random.uniform(-1.5, 1.5)
            y += random.uniform(-1.0, 1.0)

        await page.mouse.move(x, y)
        # Variable delay between moves — faster in the middle, slower at start/end
        base_ms = random.randint(4, 14)
        # Slow down at start and end (ease-in / ease-out feel)
        if t < 0.2 or t > 0.8:
            base_ms = int(base_ms * 1.4)
        await page.wait_for_timeout(base_ms)


async def human_click(page, x: float, y: float) -> None:
    """Move the mouse to (*x*, *y*) like a human, dwell briefly, then click."""
    await human_mouse_move(page, x, y)
    # Dwell time — humans pause 80-250ms before clicking
    await page.wait_for_timeout(random.randint(80, 250))
    await page.mouse.click(x, y)
    # Post-click settle time
    await page.wait_for_timeout(random.randint(150, 400))


async def human_click_element(page, locator, *, timeout: int = 5000) -> bool:
    """Move mouse to a Playwright locator's center and click it like a human.

    Returns True if the element was found and clicked, False otherwise.
    """
    try:
        elem = locator.first
        await elem.wait_for(state="visible", timeout=timeout)
        box = await elem.bounding_box()
        if not box:
            return False
        # Click near center with slight offset (humans don't hit dead center)
        cx = box["x"] + box["width"] / 2 + random.uniform(-3, 3)
        cy = box["y"] + box["height"] / 2 + random.uniform(-2, 2)
        await human_click(page, cx, cy)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Typing
# ---------------------------------------------------------------------------

async def human_type(page, text: str, *, wpm: int | None = None) -> None:
    """Type *text* character-by-character with realistic, variable delays.

    Parameters
    ----------
    page : playwright Page (keyboard must be focused on an input)
    text : the string to type
    wpm : typing speed in words-per-minute (randomised 40-60 if None)
    """
    if wpm is None:
        wpm = random.randint(40, 60)

    base_delay_ms = 60_000 / (wpm * 5)  # average ms per character

    for i, char in enumerate(text):
        # --- per-character timing variation ---
        if char in "@._-!#$%&+":
            # Special chars require Shift or are on awkward keys
            delay = base_delay_ms * random.uniform(1.5, 2.5)
        elif i > 0 and char == text[i - 1]:
            # Repeated character (same key, fast double-tap)
            delay = base_delay_ms * random.uniform(0.4, 0.7)
        elif char.isupper():
            # Holding Shift adds time
            delay = base_delay_ms * random.uniform(1.2, 1.8)
        elif char.isdigit():
            # Number row — slightly slower for touch typists
            delay = base_delay_ms * random.uniform(1.0, 1.4)
        else:
            delay = base_delay_ms * random.uniform(0.7, 1.3)

        # Occasional longer pause (thinking / typo correction simulation)
        if random.random() < 0.03:
            delay += random.uniform(100, 300)

        await page.keyboard.press(char)
        await page.wait_for_timeout(max(15, int(delay)))


# ---------------------------------------------------------------------------
# Scrolling
# ---------------------------------------------------------------------------

async def idle_scroll(page) -> None:
    """Mimic a human casually glancing at the page: scroll down, pause, scroll up."""
    scroll_down = random.randint(150, 400)
    await page.mouse.wheel(0, scroll_down)
    await page.wait_for_timeout(random.randint(400, 1200))
    scroll_up = random.randint(scroll_down // 3, scroll_down // 2)
    await page.mouse.wheel(0, -scroll_up)
    await page.wait_for_timeout(random.randint(200, 600))


async def random_mouse_jitter(page) -> None:
    """Small random mouse movements within the viewport — simulates idle cursor."""
    try:
        viewport = page.viewport_size or {"width": 1366, "height": 768}
        for _ in range(random.randint(2, 5)):
            x = random.randint(100, viewport["width"] - 100)
            y = random.randint(100, viewport["height"] - 100)
            await human_mouse_move(page, x, y, steps=random.randint(5, 12))
            await page.wait_for_timeout(random.randint(200, 800))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Random delays
# ---------------------------------------------------------------------------

async def random_delay(page, low_ms: int = 500, high_ms: int = 1500) -> None:
    """Wait a random duration between *low_ms* and *high_ms* milliseconds."""
    await page.wait_for_timeout(random.randint(low_ms, high_ms))


# ---------------------------------------------------------------------------
# Wait-for-ready helpers
# ---------------------------------------------------------------------------

async def wait_for_page_ready(page, *, timeout: int = 30_000) -> None:
    """Wait for the page to be truly interactive (network settled + DOM stable).

    Combines ``networkidle`` with a short request-quiescence check and a
    small human-like "reading" delay.
    """
    # 1. Wait for network to settle
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        # Some pages never reach networkidle (long-poll, websocket, analytics)
        logger.debug("networkidle timed out — continuing (page may still be loading)")

    # 2. Wait for no recent resource loads (catches late-firing XHR)
    try:
        await page.wait_for_function("""
            () => {
                const entries = performance.getEntriesByType('resource');
                if (entries.length === 0) return true;
                const now = performance.now();
                const recent = entries.filter(e => e.responseEnd > now - 800);
                return recent.length === 0;
            }
        """, timeout=min(timeout, 10_000))
    except Exception:
        pass

    # 3. Human "reading" pause
    await page.wait_for_timeout(random.randint(400, 1000))


async def wait_for_button_enabled(
    page,
    selector: str,
    *,
    timeout: int = 30_000,
    poll_interval: int = 500,
) -> bool:
    """Poll until the button matching *selector* is enabled and clickable.

    Checks for:
      - ``disabled`` attribute removed
      - ``aria-disabled`` not "true"
      - ``pointer-events`` not "none"
      - ``opacity`` >= 0.5

    Returns True if the button became enabled within *timeout*, False otherwise.
    """
    js = """
        (selector) => {
            const btn = document.querySelector(selector);
            if (!btn) return false;
            if (btn.disabled) return false;
            if (btn.getAttribute('aria-disabled') === 'true') return false;
            const style = window.getComputedStyle(btn);
            if (style.pointerEvents === 'none') return false;
            if (parseFloat(style.opacity) < 0.5) return false;
            if (style.cursor === 'not-allowed') return false;
            return true;
        }
    """
    try:
        await page.wait_for_function(js, selector, timeout=timeout, polling=poll_interval)
        logger.debug("Button '%s' is now enabled", selector)
        return True
    except Exception:
        logger.warning("Button '%s' still disabled after %dms", selector, timeout)
        return False


async def wait_for_element_stable(
    page,
    selector: str,
    *,
    timeout: int = 10_000,
    stability_ms: int = 500,
) -> bool:
    """Wait until the element matching *selector* is visible AND its bounding
    box hasn't changed for *stability_ms* (i.e. it's done animating).

    Returns True if stable, False on timeout.
    """
    js = f"""
        async () => {{
            const sel = {repr(selector)};
            const stabilityMs = {stability_ms};
            const start = Date.now();
            let lastBox = null;
            let stableSince = null;

            while (Date.now() - start < {timeout}) {{
                const el = document.querySelector(sel);
                if (el) {{
                    const rect = el.getBoundingClientRect();
                    const box = `${{rect.x}},${{rect.y}},${{rect.width}},${{rect.height}}`;
                    if (box === lastBox) {{
                        if (!stableSince) stableSince = Date.now();
                        if (Date.now() - stableSince >= stabilityMs) return true;
                    }} else {{
                        lastBox = box;
                        stableSince = null;
                    }}
                }}
                await new Promise(r => setTimeout(r, 100));
            }}
            return false;
        }}
    """
    try:
        return await page.evaluate(js)
    except Exception:
        return False


async def wait_for_url_change(page, current_url: str, *, timeout: int = 15_000) -> bool:
    """Wait until ``page.url`` differs from *current_url*.

    Returns True if the URL changed, False on timeout.
    """
    try:
        await page.wait_for_function(
            f"() => window.location.href !== {repr(current_url)}",
            timeout=timeout,
        )
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Popup sweep
# ---------------------------------------------------------------------------

# Selectors for known Target popups/overlays, ordered by likelihood.
_POPUP_DISMISS_SELECTORS = [
    # Cookie / privacy consent (floating-ui portal)
    ('[data-floating-ui-portal] button:has-text("Accept")', "cookie consent (Accept)"),
    ('[data-floating-ui-portal] button:has-text("Close")', "cookie consent (Close)"),
    ('[data-floating-ui-portal] button:has-text("Got it")', "cookie consent (Got it)"),
    ('#onetrust-accept-btn-handler', "OneTrust cookie banner"),
    # Generic close buttons on dialogs/modals
    ('[role="dialog"] button[aria-label="close"]', "dialog close (aria-label)"),
    ('[role="dialog"] button[aria-label="Close"]', "dialog close (aria-label)"),
    ('[aria-modal="true"] button[aria-label="close"]', "modal close (aria-label)"),
    ('[aria-modal="true"] button[aria-label="Close"]', "modal close (aria-label)"),
    # "Not now" / "Skip" / "No thanks" buttons (sign-in prompts, promos)
    ('[role="dialog"] button:has-text("Not now")', "dialog (Not now)"),
    ('[role="dialog"] button:has-text("No thanks")', "dialog (No thanks)"),
    ('[role="dialog"] button:has-text("No, thanks")', "dialog (No, thanks)"),
    ('[role="dialog"] button:has-text("Skip")', "dialog (Skip)"),
    ('[role="dialog"] button:has-text("Close")', "dialog (Close)"),
    ('[aria-modal="true"] button:has-text("Not now")', "modal (Not now)"),
    ('[aria-modal="true"] button:has-text("Close")', "modal (Close)"),
    # Health consent
    ('dialog button:has-text("I agree")', "health consent (I agree)"),
    ('dialog button:has-text("Agree")', "health consent (Agree)"),
    # Age gate
    ('button:has-text("Yes, I am")', "age gate (Yes)"),
    ('[data-test="ageGateConfirm"]', "age gate (confirm)"),
    # Store picker
    ('[data-test="storePickerClose"]', "store picker close"),
    # Generic overlay buttons (last resort)
    ('button[id*="accept" i]', "generic accept button"),
    ('button[id*="cookie" i]', "generic cookie button"),
]

# JS to remove stubborn overlays that can't be clicked away.
_OVERLAY_REMOVAL_JS = """() => {
    let removed = 0;
    // Remove floating-ui portal overlays
    document.querySelectorAll('[data-floating-ui-portal]').forEach(el => {
        el.remove();
        removed++;
    });
    // Restore inert elements
    document.querySelectorAll('[data-floating-ui-inert]').forEach(el => {
        el.removeAttribute('data-floating-ui-inert');
        el.removeAttribute('aria-hidden');
    });
    // Remove pointer-events-blocking overlays
    document.querySelectorAll('div[class*="overlay"]').forEach(el => {
        const style = window.getComputedStyle(el);
        if (style.position === 'fixed' || style.position === 'absolute') {
            if (style.pointerEvents !== 'none') {
                el.style.pointerEvents = 'none';
                removed++;
            }
        }
    });
    return removed;
}"""


async def sweep_popups(page, *, use_js_fallback: bool = True) -> int:
    """Detect and dismiss any visible popups/overlays on the page.

    Iterates through known popup selectors and clicks the first visible
    dismiss button found.  Repeats up to 3 times (for stacked popups).
    Falls back to JS removal if no clickable dismiss button is found.

    Returns the total number of popups dismissed.
    """
    dismissed = 0

    for _round in range(3):
        found_this_round = False

        for sel, description in _POPUP_DISMISS_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=400):
                    await btn.click(timeout=2000)
                    logger.info("Popup sweep: dismissed %s via '%s'", description, sel)
                    dismissed += 1
                    found_this_round = True
                    await page.wait_for_timeout(random.randint(300, 600))
                    break  # Re-scan from the top (popup might reveal another)
            except Exception:
                continue

        if not found_this_round:
            break

    # JS fallback: remove stubborn overlays that have no clickable button
    if use_js_fallback:
        try:
            js_removed = await page.evaluate(_OVERLAY_REMOVAL_JS)
            if js_removed:
                logger.info("Popup sweep: removed %d blocking overlay(s) via JS", js_removed)
                dismissed += js_removed
                await page.wait_for_timeout(300)
        except Exception:
            pass

    return dismissed

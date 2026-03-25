"""Automated PerimeterX 'Press & Hold' CAPTCHA solver using Playwright.

When the Walmart (or Sam's Club) monitor detects a CAPTCHA block page,
this module launches a headless browser with stealth JS, navigates to the
blocked URL, finds the press-and-hold button, holds it for the required
duration, and returns the resulting session cookies that bypass the block.

The cookies are then fed back into the monitor's httpx client so
subsequent requests proceed without interruption.
"""

from __future__ import annotations

import asyncio
import logging
import random

logger = logging.getLogger(__name__)

# How long to hold the button (PerimeterX typically requires 5-10s)
_HOLD_MIN_MS = 6_000
_HOLD_MAX_MS = 10_000

# Max time to wait for CAPTCHA element to appear
_CAPTCHA_WAIT_MS = 10_000

# Max time to wait for page to redirect after solving
_REDIRECT_WAIT_MS = 15_000

# Selectors for the PerimeterX press-and-hold CAPTCHA element
_PX_CAPTCHA_SELECTORS = [
    "#px-captcha",
    "[id*='px-captcha']",
    "[class*='px-captcha']",
    "div[role='button']:has-text('press')",
    "button:has-text('Press & Hold')",
    "div:has-text('Press & Hold'):not(:has(div:has-text('Press & Hold')))",
]


async def solve_px_captcha(url: str, existing_cookies: dict[str, str] | None = None) -> dict[str, str] | None:
    """Attempt to solve a PerimeterX press-and-hold CAPTCHA for the given URL.

    Parameters
    ----------
    url : str
        The product page URL that triggered the CAPTCHA.
    existing_cookies : dict
        Any existing session cookies to load into the browser context.

    Returns
    -------
    dict[str, str] | None
        Fresh cookies from the solved session, or None if solving failed.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Playwright not installed — cannot solve CAPTCHA automatically")
        return None

    logger.info("CAPTCHA solver: attempting to solve press-and-hold for %s", url)

    pw = None
    browser = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
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

        # Import stealth JS from checkout engine
        from pmon.checkout.engine import STEALTH_JS
        from pmon.monitors.base import _CHROME_FULL, _CHROME_MAJOR

        context = await browser.new_context(
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
        await context.add_init_script(STEALTH_JS)

        # Apply existing cookies if provided
        if existing_cookies:
            cookie_list = []
            for name, value in existing_cookies.items():
                cookie_list.append({
                    "name": name,
                    "value": str(value),
                    "domain": ".walmart.com",
                    "path": "/",
                })
            if cookie_list:
                await context.add_cookies(cookie_list)

        page = await context.new_page()

        # Navigate to the URL that triggered the CAPTCHA
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        except Exception as e:
            logger.warning("CAPTCHA solver: navigation failed: %s", e)
            return None

        # Wait a moment for the page to fully render
        await page.wait_for_timeout(random.randint(1500, 3000))

        # Find the press-and-hold CAPTCHA element
        captcha_elem = None
        for selector in _PX_CAPTCHA_SELECTORS:
            try:
                elem = page.locator(selector).first
                if await elem.is_visible(timeout=1500):
                    captcha_elem = elem
                    logger.info("CAPTCHA solver: found CAPTCHA element via '%s'", selector)
                    break
            except Exception:
                continue

        if not captcha_elem:
            # Check if we're actually not on a CAPTCHA page (maybe cookies worked)
            html = await page.content()
            if "press & hold" not in html.lower() and "/blocked" not in page.url:
                logger.info("CAPTCHA solver: no CAPTCHA detected — page loaded normally")
                cookies = await _extract_cookies(context)
                await context.close()
                return cookies

            logger.warning("CAPTCHA solver: CAPTCHA page detected but could not find interactive element")
            return None

        # Get the bounding box of the CAPTCHA element
        box = await captcha_elem.bounding_box()
        if not box:
            logger.warning("CAPTCHA solver: CAPTCHA element has no bounding box")
            return None

        # Calculate click position (center with slight human-like offset)
        cx = box["x"] + box["width"] / 2 + random.uniform(-5, 5)
        cy = box["y"] + box["height"] / 2 + random.uniform(-3, 3)

        # Pre-interaction: move mouse around naturally before engaging CAPTCHA
        viewport = page.viewport_size or {"width": 1366, "height": 768}
        for _ in range(random.randint(2, 4)):
            rx = random.randint(100, viewport["width"] - 100)
            ry = random.randint(100, viewport["height"] - 100)
            await page.mouse.move(rx, ry, steps=random.randint(5, 12))
            await page.wait_for_timeout(random.randint(200, 600))

        # Move to the CAPTCHA button with human-like motion
        await _human_mouse_move(page, cx, cy)
        await page.wait_for_timeout(random.randint(200, 500))

        # Press and HOLD the mouse button
        hold_duration = random.randint(_HOLD_MIN_MS, _HOLD_MAX_MS)
        logger.info("CAPTCHA solver: pressing and holding for %dms", hold_duration)
        await page.mouse.down()

        # Hold with slight micro-movements (humans can't hold perfectly still)
        elapsed = 0
        while elapsed < hold_duration:
            jitter_ms = random.randint(200, 600)
            await page.wait_for_timeout(jitter_ms)
            elapsed += jitter_ms
            # Tiny micro-movements while holding
            jx = cx + random.uniform(-2, 2)
            jy = cy + random.uniform(-1.5, 1.5)
            await page.mouse.move(jx, jy)

        # Release the mouse button
        await page.mouse.up()
        logger.info("CAPTCHA solver: released after hold")

        # Wait for the page to redirect or update after solving
        await page.wait_for_timeout(random.randint(1000, 2000))

        # Check if CAPTCHA was solved (page should redirect away from block page)
        solved = False
        for attempt in range(10):
            current_url = page.url
            try:
                html = await page.content()
            except Exception as content_err:
                # If page.content() fails because the page is navigating,
                # that means the CAPTCHA was solved and PerimeterX is
                # redirecting us — treat this as success.
                err_msg = str(content_err).lower()
                if "navigating" in err_msg or "changing" in err_msg:
                    logger.info(
                        "CAPTCHA solver: page is navigating after solve — "
                        "treating as success (attempt %d)", attempt + 1,
                    )
                    # Give the navigation time to settle, then extract cookies
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=_REDIRECT_WAIT_MS)
                    except Exception:
                        # Even if this times out, the cookies may still be set
                        await page.wait_for_timeout(3000)
                    solved = True
                    break
                # Some other error — log and keep trying
                logger.debug("CAPTCHA solver: page.content() error: %s", content_err)
                await page.wait_for_timeout(1500)
                continue

            if "/blocked" not in current_url and "press & hold" not in html.lower():
                solved = True
                break
            await page.wait_for_timeout(1500)

        if solved:
            logger.info("CAPTCHA solver: successfully solved! Extracting cookies.")
            cookies = await _extract_cookies(context)
            await context.close()
            return cookies
        else:
            logger.warning("CAPTCHA solver: CAPTCHA may not have been solved — page still blocked")
            return None

    except Exception as e:
        logger.error("CAPTCHA solver: unexpected error: %s", e)
        return None
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass


async def _extract_cookies(context) -> dict[str, str]:
    """Extract all cookies from the browser context as a flat dict."""
    all_cookies = await context.cookies()
    result = {}
    for cookie in all_cookies:
        # Only keep walmart.com cookies
        if "walmart.com" in cookie.get("domain", ""):
            result[cookie["name"]] = cookie["value"]
    return result


async def _human_mouse_move(page, target_x: float, target_y: float) -> None:
    """Simplified human-like mouse movement (avoids importing from checkout)."""
    import math

    try:
        pos = await page.evaluate(
            "() => ({x: window.__pmon_mx || 0, y: window.__pmon_my || 0})"
        )
        sx, sy = pos["x"], pos["y"]
    except Exception:
        sx, sy = random.randint(100, 400), random.randint(100, 300)

    distance = math.hypot(target_x - sx, target_y - sy)
    steps = max(10, min(40, int(distance / 25)))

    # Bezier control points for a natural curve
    spread = distance * 0.15
    mid_x = (sx + target_x) / 2
    mid_y = (sy + target_y) / 2
    cp1_x = sx + (mid_x - sx) * 0.4 + random.uniform(-spread, spread)
    cp1_y = sy + (mid_y - sy) * 0.4 + random.uniform(-spread, spread)
    cp2_x = mid_x + (target_x - mid_x) * 0.6 + random.uniform(-spread, spread)
    cp2_y = mid_y + (target_y - mid_y) * 0.6 + random.uniform(-spread, spread)

    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        x = u**3 * sx + 3 * u**2 * t * cp1_x + 3 * u * t**2 * cp2_x + t**3 * target_x
        y = u**3 * sy + 3 * u**2 * t * cp1_y + 3 * u * t**2 * cp2_y + t**3 * target_y

        if i < steps:
            x += random.uniform(-1.5, 1.5)
            y += random.uniform(-1.0, 1.0)

        await page.mouse.move(x, y)
        base_ms = random.randint(5, 15)
        if t < 0.2 or t > 0.8:
            base_ms = int(base_ms * 1.4)
        await page.wait_for_timeout(base_ms)

    # Install tracker for future calls
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

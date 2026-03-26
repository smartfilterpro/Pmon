"""Anti-detection utilities for browser automation.

REVIEWED [Mission 5C] — Implements realistic human-like behavior patterns
to reduce bot detection risk across all retailers.

These utilities complement the existing human_behavior.py module with
additional anti-fingerprinting measures at the session/context level.
"""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page, BrowserContext

# Realistic User-Agent pool — rotated per session
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.7680.80 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.7568.99 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.7680.80 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.7680.80 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.7423.118 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.7568.99 Safari/537.36",
]

# Viewport dimensions pool — never reuse same dimensions consecutively
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1280, "height": 720},
    {"width": 1600, "height": 900},
    {"width": 1680, "height": 1050},
    {"width": 1360, "height": 768},
]

# Track last used values to avoid repetition
_last_ua: str = ""
_last_viewport: dict = {}


def get_random_user_agent() -> str:
    """Get a random User-Agent string, avoiding the previously used one."""
    global _last_ua
    choices = [ua for ua in USER_AGENTS if ua != _last_ua]
    ua = random.choice(choices) if choices else random.choice(USER_AGENTS)
    _last_ua = ua
    return ua


def get_random_viewport() -> dict:
    """Get random viewport dimensions, avoiding the previously used ones."""
    global _last_viewport
    choices = [v for v in VIEWPORTS if v != _last_viewport]
    vp = random.choice(choices) if choices else random.choice(VIEWPORTS)
    _last_viewport = vp
    return vp


async def random_mouse_path(page: "Page", target_x: float, target_y: float) -> None:
    """Move mouse to target using a bezier-curve path with random waypoints.

    This provides more natural mouse movement than straight-line moves.
    Uses cubic bezier interpolation with randomized control points.
    """
    # Get current position
    try:
        pos = await page.evaluate("""() => ({
            x: window.__pmon_mx || 0,
            y: window.__pmon_my || 0
        })""")
        sx, sy = pos["x"], pos["y"]
    except Exception:
        sx, sy = random.randint(100, 400), random.randint(100, 300)

    distance = math.hypot(target_x - sx, target_y - sy)
    if distance < 5:
        await page.mouse.move(target_x, target_y)
        return

    steps = max(8, min(45, int(distance / 25)))

    # Random control points for bezier curve
    spread = distance * 0.2
    cp1_x = sx + (target_x - sx) * 0.3 + random.uniform(-spread, spread)
    cp1_y = sy + (target_y - sy) * 0.3 + random.uniform(-spread, spread)
    cp2_x = sx + (target_x - sx) * 0.7 + random.uniform(-spread, spread)
    cp2_y = sy + (target_y - sy) * 0.7 + random.uniform(-spread, spread)

    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        x = u**3 * sx + 3 * u**2 * t * cp1_x + 3 * u * t**2 * cp2_x + t**3 * target_x
        y = u**3 * sy + 3 * u**2 * t * cp1_y + 3 * u * t**2 * cp2_y + t**3 * target_y

        if i < steps:
            x += random.uniform(-1.5, 1.5)
            y += random.uniform(-1.0, 1.0)

        await page.mouse.move(x, y)
        # Variable inter-move delay with ease-in/ease-out
        delay = random.randint(4, 16)
        if t < 0.2 or t > 0.8:
            delay = int(delay * 1.3)
        await page.wait_for_timeout(delay)


async def randomized_typing(page: "Page", text: str) -> None:
    """Type text with randomized per-character delays (80-180ms range).

    Simulates realistic typing patterns with variable speed.
    """
    for i, char in enumerate(text):
        # Base delay: 80-180ms per character
        delay = random.randint(80, 180)

        # Special characters are slower
        if char in "@._-!#$%&+/":
            delay = random.randint(150, 280)
        # Repeated characters are faster (double-tap)
        elif i > 0 and char == text[i - 1]:
            delay = random.randint(40, 90)
        # Digits require reaching to number row
        elif char.isdigit():
            delay = random.randint(100, 200)

        # Occasional longer pause (thinking)
        if random.random() < 0.03:
            delay += random.randint(150, 400)

        await page.keyboard.press(char)
        await page.wait_for_timeout(delay)


async def pre_action_pause(page: "Page") -> None:
    """Random pause before any click or form fill (50-300ms).

    Humans never click instantly — there's always a small perception-to-action gap.
    """
    await page.wait_for_timeout(random.randint(50, 300))


def get_stealth_context_options(
    user_agent: str | None = None,
    viewport: dict | None = None,
) -> dict:
    """Generate browser context options with anti-detection settings.

    Returns kwargs suitable for browser.new_context(**kwargs).
    """
    ua = user_agent or get_random_user_agent()
    vp = viewport or get_random_viewport()

    # Extract Chrome version for Sec-Ch-Ua
    import re
    chrome_match = re.search(r"Chrome/(\d+)", ua)
    chrome_major = chrome_match.group(1) if chrome_match else "146"

    return {
        "user_agent": ua,
        "viewport": vp,
        "screen": {"width": vp["width"] + 200, "height": vp["height"] + 200},
        "locale": "en-US",
        "timezone_id": random.choice([
            "America/New_York", "America/Chicago",
            "America/Denver", "America/Los_Angeles",
        ]),
        "color_scheme": "light",
        "extra_http_headers": {
            "Sec-Ch-Ua": f'"Chromium";v="{chrome_major}", "Google Chrome";v="{chrome_major}", "Not?A_Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        },
    }

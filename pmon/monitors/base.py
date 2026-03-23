"""Base monitor class for stock checking."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod

import httpx

from pmon.models import StockResult, StockStatus

logger = logging.getLogger(__name__)

# Current Chrome version — MUST match the actual Playwright Chromium binary.
# Mismatch between UA string and real browser version is a top bot signal.
# Run: ~/.cache/ms-playwright/chromium-*/chrome-linux/chrome --version
_CHROME_MAJOR = "141"
_CHROME_FULL = "141.0.7390.37"

# Realistic browser headers that match the actual Playwright Chromium version.
# Includes Sec-Ch-Ua and Sec-Fetch-* headers that modern browsers always send.
DEFAULT_HEADERS = {
    "User-Agent": (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{_CHROME_FULL} Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    # Client-hint headers — Chrome sends these on every navigation request.
    "Sec-Ch-Ua": f'"Chromium";v="{_CHROME_MAJOR}", "Google Chrome";v="{_CHROME_MAJOR}", "Not?A_Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    # Sec-Fetch headers — their absence is the #1 bot signal for PerimeterX.
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# Lighter header set for XHR / JSON API calls (mimics fetch() from page context).
API_HEADERS = {
    "User-Agent": DEFAULT_HEADERS["User-Agent"],
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Sec-Ch-Ua": DEFAULT_HEADERS["Sec-Ch-Ua"],
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}


class BaseMonitor(ABC):
    """Base class for all retailer stock monitors."""

    retailer_name: str = "unknown"

    # Per-retailer minimum seconds between requests.  Retailers that aggressively
    # rate-limit (e.g. Walmart) override this with a higher value.
    _min_request_interval: float = 2.0

    # Domain used for setting cookies on the httpx client.
    _cookie_domain: str = ""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        # Timestamp (monotonic) of the last request we made
        self._last_request_at: float = 0.0
        # Rate-limit cooldown: if we get a 429 we back off until this time
        self._rate_limit_until: float = 0.0
        # Consecutive 429 count — drives exponential backoff
        self._consecutive_429s: int = 0
        # Session cookies loaded from the database ({name: value})
        self._session_cookies: dict[str, str] = {}

    def load_session_cookies(self, cookies: dict[str, str]):
        """Load session cookies and apply them to the current client.

        Called by the engine on startup and by the dashboard when cookies
        are imported.  Triggers a client reset so the new cookies take effect.
        Skips reset if cookies haven't changed.
        """
        new_cookies = dict(cookies)
        if new_cookies == self._session_cookies:
            return  # No change — skip client reset
        self._session_cookies = new_cookies
        # Force client recreation so cookies are applied on next request
        if self._client and not self._client.is_closed:
            asyncio.get_event_loop().create_task(self._client.aclose())
        self._client = None
        logger.info(
            "%s: loaded %d session cookies for monitoring",
            self.retailer_name, len(cookies),
        )

    async def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=DEFAULT_HEADERS,
                follow_redirects=True,
                timeout=httpx.Timeout(15.0),
                http2=True,  # Target/Walmart expect h2; plain h1.1 is a bot signal
            )
            # Apply session cookies if available
            if self._session_cookies and self._cookie_domain:
                for name, value in self._session_cookies.items():
                    self._client.cookies.set(name, str(value), domain=self._cookie_domain)
                logger.debug(
                    "%s: applied %d session cookies to monitor client",
                    self.retailer_name, len(self._session_cookies),
                )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Rate-limit helpers
    # ------------------------------------------------------------------

    def is_rate_limited(self) -> bool:
        """Return True if we are currently in a cooldown period from a 429."""
        return time.monotonic() < self._rate_limit_until

    def rate_limit_remaining(self) -> float:
        """Seconds remaining in the current cooldown, or 0."""
        return max(0.0, self._rate_limit_until - time.monotonic())

    def record_rate_limit(self, retry_after: float | None = None):
        """Record that we received a 429 and compute the next cooldown.

        Uses exponential backoff: 60s, 120s, 240s, capped at 5 minutes.
        If the server sends a Retry-After header we honour it (with a floor
        of 60 s so we don't hammer them).
        """
        self._consecutive_429s += 1
        backoff = min(60 * (2 ** (self._consecutive_429s - 1)), 300)
        if retry_after is not None:
            backoff = max(retry_after, 60)
        self._rate_limit_until = time.monotonic() + backoff
        logger.warning(
            "%s rate-limited (429). Backing off for %.0fs (attempt %d)",
            self.retailer_name, backoff, self._consecutive_429s,
        )

    def record_success(self):
        """Reset the consecutive-429 counter after a successful request."""
        self._consecutive_429s = 0

    async def throttle(self):
        """Sleep if needed to respect _min_request_interval between requests."""
        now = time.monotonic()
        elapsed = now - self._last_request_at
        if elapsed < self._min_request_interval:
            wait = self._min_request_interval - elapsed
            await asyncio.sleep(wait)
        self._last_request_at = time.monotonic()

    @abstractmethod
    async def check_stock(self, url: str, product_name: str) -> StockResult:
        """Check if a product is in stock. Must be implemented by each retailer."""
        ...

    async def safe_check(self, url: str, product_name: str) -> StockResult:
        """Check stock with error handling and rate-limit awareness."""
        # If we're in a cooldown from a previous 429, skip this check entirely
        if self.is_rate_limited():
            remaining = self.rate_limit_remaining()
            logger.debug(
                "Skipping %s check for %s — rate-limit cooldown (%.0fs left)",
                self.retailer_name, product_name, remaining,
            )
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.ERROR,
                error_message=f"Rate limited — retrying in {remaining:.0f}s",
            )

        # Throttle to respect per-retailer minimum interval
        await self.throttle()

        try:
            return await self.check_stock(url, product_name)
        except httpx.TimeoutException:
            logger.warning(f"Timeout checking {product_name} at {self.retailer_name}")
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.ERROR,
                error_message="Request timed out",
            )
        except Exception as e:
            logger.error(f"Error checking {product_name} at {self.retailer_name}: {e}")
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.ERROR,
                error_message=str(e),
            )

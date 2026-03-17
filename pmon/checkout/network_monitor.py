"""Network request monitor for Playwright pages.

Uses ``page.route()`` and response interception to track critical API calls
during the Target login and checkout flows.  Instead of guessing with fixed
``wait_for_timeout()`` calls, the bot can observe the actual network activity
and wait for specific requests to complete.

Usage:
    monitor = NetworkMonitor(page)
    await monitor.start()

    # ... perform login actions ...

    # Wait until both token_validations calls have completed
    ok = await monitor.wait_for("token_validations", expected_count=2, timeout=15000)

    # Check if PerimeterX blocked the session
    if monitor.was_blocked():
        handle_block()

    await monitor.stop()
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class NetworkMonitor:
    """Observe network requests/responses on a Playwright page.

    Tracks matching requests and exposes async helpers to wait until
    expected requests complete, detect bot blocks, and observe OAuth flows.
    """

    def __init__(self, page):
        self._page = page
        self._active = False

        # Tracked responses keyed by pattern name
        # Each entry: list of {"url": str, "status": int, "timestamp": float}
        self._responses: dict[str, list[dict]] = {}

        # Patterns to watch for.  Key = friendly name, value = URL substring.
        self._patterns: dict[str, str] = {
            "token_validations": "oauth_validations/v3/token_validations",
            "auth_codes": "authentications/v1/auth_codes",
            "profile_details": "guest_profile_details/v1/profile_details",
            "cart": "web_checkouts/v1/cart",
            "px_collector": "px-cloud.net/api/v2/collector",
            "telemetry": "telemetry_data/v1/traces",
        }

        # Track blocked requests (403, challenge pages)
        self._blocked_responses: list[dict] = []

        # Pending futures waiting for specific patterns
        self._waiters: list[dict] = []

    # ----- lifecycle -----

    async def start(self) -> None:
        """Start intercepting responses on the page."""
        if self._active:
            return
        self._active = True
        self._page.on("response", self._on_response)
        logger.debug("NetworkMonitor started")

    async def stop(self) -> None:
        """Stop intercepting and clean up."""
        if not self._active:
            return
        self._active = False
        try:
            self._page.remove_listener("response", self._on_response)
        except Exception:
            pass
        # Cancel any pending waiters
        for waiter in self._waiters:
            if not waiter["future"].done():
                waiter["future"].cancel()
        self._waiters.clear()
        logger.debug("NetworkMonitor stopped")

    # ----- internal handlers -----

    async def _on_response(self, response) -> None:
        """Called for every response on the page."""
        url = response.url
        status = response.status

        # Check against tracked patterns
        for name, pattern in self._patterns.items():
            if pattern in url:
                entry = {
                    "url": url,
                    "status": status,
                    "timestamp": time.monotonic(),
                }
                self._responses.setdefault(name, []).append(entry)
                logger.debug("NetworkMonitor: %s [%d] %s", name, status, url[:120])

                # Track blocked responses
                if status in (403, 429):
                    self._blocked_responses.append(entry)
                    logger.warning("NetworkMonitor: BLOCKED %s [%d]", name, status)

                # Resolve any waiters for this pattern
                self._resolve_waiters(name)
                break

    def _resolve_waiters(self, pattern_name: str) -> None:
        """Check if any waiters for *pattern_name* can be resolved."""
        remaining = []
        for waiter in self._waiters:
            if waiter["pattern"] == pattern_name:
                count = len(self._responses.get(pattern_name, []))
                if count >= waiter["expected_count"]:
                    if not waiter["future"].done():
                        waiter["future"].set_result(True)
                    continue
            remaining.append(waiter)
        self._waiters = remaining

    # ----- public API -----

    async def wait_for(
        self,
        pattern_name: str,
        *,
        expected_count: int = 1,
        timeout: int = 15_000,
    ) -> bool:
        """Wait until *pattern_name* has been observed at least *expected_count* times.

        Returns True if observed within *timeout* ms, False otherwise.
        """
        # Check if already satisfied
        count = len(self._responses.get(pattern_name, []))
        if count >= expected_count:
            return True

        # Create a future and register as waiter
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        waiter = {
            "pattern": pattern_name,
            "expected_count": expected_count,
            "future": future,
        }
        self._waiters.append(waiter)

        try:
            await asyncio.wait_for(future, timeout=timeout / 1000)
            return True
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.debug(
                "NetworkMonitor: wait_for('%s', count=%d) timed out after %dms (saw %d)",
                pattern_name,
                expected_count,
                timeout,
                len(self._responses.get(pattern_name, [])),
            )
            return False

    async def wait_for_login_complete(self, *, timeout: int = 20_000) -> bool:
        """Wait for the Target OAuth login to fully complete.

        Looks for both ``token_validations`` calls (Target fires this twice)
        and the subsequent ``profile_details`` call that confirms the session.

        Returns True if login completed within *timeout*, False otherwise.
        """
        # Token validation is the critical signal — Target calls it twice
        token_ok = await self.wait_for(
            "token_validations",
            expected_count=2,
            timeout=timeout,
        )
        if not token_ok:
            # Partial success: check if at least one validation came through
            count = len(self._responses.get("token_validations", []))
            if count >= 1:
                logger.info("NetworkMonitor: only %d/2 token validations seen, proceeding anyway", count)
                return True
            return False

        # Give a brief moment for the profile/cart calls to fire
        try:
            await self.wait_for("profile_details", expected_count=1, timeout=3000)
        except Exception:
            pass

        return True

    def was_blocked(self) -> bool:
        """Return True if any tracked request returned 403 or 429."""
        return len(self._blocked_responses) > 0

    def get_blocked_details(self) -> list[dict]:
        """Return details of all blocked responses."""
        return list(self._blocked_responses)

    def get_responses(self, pattern_name: str) -> list[dict]:
        """Return all recorded responses for *pattern_name*."""
        return list(self._responses.get(pattern_name, []))

    def response_count(self, pattern_name: str) -> int:
        """How many times *pattern_name* has been observed."""
        return len(self._responses.get(pattern_name, []))

    def add_pattern(self, name: str, url_substring: str) -> None:
        """Register an additional URL pattern to track."""
        self._patterns[name] = url_substring

    def reset(self) -> None:
        """Clear all recorded responses and blocked list."""
        self._responses.clear()
        self._blocked_responses.clear()

"""Lightweight in-memory rate limiter for FastAPI.

REVIEWED [Mission 4C] — Rate limits auth endpoints to prevent brute-force.
No external dependencies (no Redis, no slowapi). Uses a simple sliding
window counter per IP address.

Usage:
    from pmon.rate_limit import RateLimiter, rate_limit_check

    limiter = RateLimiter()

    @app.post("/api/auth/login")
    async def login(request: Request):
        rate_limit_check(request, limiter, max_requests=5, window_seconds=60)
        ...
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from fastapi import Request, HTTPException

logger = logging.getLogger(__name__)

# Default limits
LOGIN_LIMIT = 5          # attempts per window
LOGIN_WINDOW = 60        # seconds
REGISTER_LIMIT = 3       # attempts per window
REGISTER_WINDOW = 3600   # seconds (1 hour)


class RateLimiter:
    """Simple sliding window rate limiter keyed by IP address.

    Stores timestamps of recent requests per key. Old entries are pruned
    on each check to prevent unbounded memory growth.
    """

    def __init__(self):
        # {key: [timestamp, timestamp, ...]}
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._last_prune: float = 0.0

    def check(self, key: str, max_requests: int, window_seconds: int) -> bool:
        """Check if the request is within rate limits.

        Returns True if allowed, False if rate limited.
        """
        now = time.monotonic()

        # Prune old entries every 60 seconds to prevent memory leak
        if now - self._last_prune > 60:
            self._prune(now, max_window=max(window_seconds, 3600))
            self._last_prune = now

        # Remove expired entries for this key
        cutoff = now - window_seconds
        timestamps = self._requests[key]
        self._requests[key] = [t for t in timestamps if t > cutoff]

        if len(self._requests[key]) >= max_requests:
            return False

        self._requests[key].append(now)
        return True

    def remaining(self, key: str, max_requests: int, window_seconds: int) -> int:
        """Return number of requests remaining in the current window."""
        now = time.monotonic()
        cutoff = now - window_seconds
        count = sum(1 for t in self._requests.get(key, []) if t > cutoff)
        return max(0, max_requests - count)

    def _prune(self, now: float, max_window: int):
        """Remove entries older than max_window from all keys."""
        cutoff = now - max_window
        empty_keys = []
        for key, timestamps in self._requests.items():
            self._requests[key] = [t for t in timestamps if t > cutoff]
            if not self._requests[key]:
                empty_keys.append(key)
        for key in empty_keys:
            del self._requests[key]


def _get_client_ip(request: Request) -> str:
    """Extract client IP from request, respecting X-Forwarded-For."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Take the first IP (client IP) from the chain
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def rate_limit_check(
    request: Request,
    limiter: RateLimiter,
    max_requests: int,
    window_seconds: int,
    endpoint_name: str = "",
):
    """Check rate limit and raise 429 if exceeded.

    Call this at the top of rate-limited endpoint handlers.
    """
    ip = _get_client_ip(request)
    key = f"{endpoint_name}:{ip}" if endpoint_name else ip

    if not limiter.check(key, max_requests, window_seconds):
        remaining = limiter.remaining(key, max_requests, window_seconds)
        logger.warning(
            "Rate limit exceeded: %s from %s (%d/%d in %ds)",
            endpoint_name, ip, max_requests, max_requests, window_seconds,
        )
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(window_seconds)},
        )

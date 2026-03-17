"""Base monitor class for stock checking."""

from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod

import httpx

from pmon.models import StockResult, StockStatus

logger = logging.getLogger(__name__)

# Current Chrome version — keep this updated to avoid stale UA detection.
# Last updated: 2026-03.  Check https://chromereleases.googleblog.com for latest.
_CHROME_MAJOR = "133"
_CHROME_FULL = "133.0.6943.127"

# Realistic browser headers that match a real Chrome 133 on Windows 10/11.
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
    "Sec-Ch-Ua": f'"Chromium";v="{_CHROME_MAJOR}", "Google Chrome";v="{_CHROME_MAJOR}", "Not-A.Brand";v="24"',
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

    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    async def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=DEFAULT_HEADERS,
                follow_redirects=True,
                timeout=httpx.Timeout(15.0),
                http2=True,  # Target/Walmart expect h2; plain h1.1 is a bot signal
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    @abstractmethod
    async def check_stock(self, url: str, product_name: str) -> StockResult:
        """Check if a product is in stock. Must be implemented by each retailer."""
        ...

    async def safe_check(self, url: str, product_name: str) -> StockResult:
        """Check stock with error handling."""
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

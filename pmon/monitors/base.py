"""Base monitor class for stock checking."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

import httpx

from pmon.models import StockResult, StockStatus

logger = logging.getLogger(__name__)

# Common browser-like headers
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
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

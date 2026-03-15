"""Target stock monitor."""

from __future__ import annotations

import json
import logging
import re

from pmon.models import StockResult, StockStatus
from .base import BaseMonitor

logger = logging.getLogger(__name__)


class TargetMonitor(BaseMonitor):
    retailer_name = "target"

    # Target's fulfillment API for stock checking
    FULFILLMENT_URL = "https://redsky.target.com/redsky_aggregations/v1/web/pdp_fulfillment_v1"

    def _extract_tcin(self, url: str) -> str | None:
        """Extract TCIN (Target product ID) from URL."""
        # Target URLs look like: /p/product-name/-/A-12345678
        match = re.search(r"A-(\d+)", url)
        return match.group(1) if match else None

    async def check_stock(self, url: str, product_name: str) -> StockResult:
        tcin = self._extract_tcin(url)
        if not tcin:
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.ERROR,
                error_message="Could not extract TCIN from URL",
            )

        client = await self.get_client()

        # Try the Redsky API first (faster and more reliable)
        params = {
            "key": "9f36aeafbe60771e321a7cc95a78140772ab3e96",
            "tcin": tcin,
            "store_id": "none",
            "has_store_id": "false",
            "scheduled_delivery_store_id": "none",
            "has_scheduled_delivery_store_id": "false",
        }

        try:
            resp = await client.get(self.FULFILLMENT_URL, params=params)
            if resp.status_code == 200:
                data = resp.json()
                return self._parse_fulfillment(url, product_name, data)
        except Exception as e:
            logger.debug(f"Redsky API failed for {tcin}: {e}")

        # Fallback: scrape the product page
        return await self._scrape_page(url, product_name, client)

    def _parse_fulfillment(self, url: str, product_name: str, data: dict) -> StockResult:
        try:
            product = data.get("data", {}).get("product", {})
            fulfillment = product.get("fulfillment", {})

            # Check shipping availability
            shipping = fulfillment.get("shipping_options", {})
            is_available = shipping.get("availability_status", "") == "IN_STOCK"

            # Also check online purchase availability
            product_info = fulfillment.get("product_id")

            price_info = product.get("price", {})
            price = price_info.get("formatted_current_price", "")

            if is_available:
                return StockResult(
                    url=url,
                    retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.IN_STOCK,
                    price=price,
                )

            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.OUT_OF_STOCK,
                price=price,
            )
        except (KeyError, TypeError) as e:
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.UNKNOWN,
                error_message=f"Could not parse fulfillment data: {e}",
            )

    async def _scrape_page(self, url: str, product_name: str, client) -> StockResult:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text

        # Look for stock indicators in the page
        if re.search(r'"availability_status"\s*:\s*"IN_STOCK"', html):
            price = ""
            price_match = re.search(r'"formatted_current_price"\s*:\s*"([^"]+)"', html)
            if price_match:
                price = price_match.group(1)
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.IN_STOCK,
                price=price,
            )

        if re.search(r"(out of stock|sold out|temporarily unavailable)", html, re.I):
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.OUT_OF_STOCK,
            )

        return StockResult(
            url=url,
            retailer=self.retailer_name,
            product_name=product_name,
            status=StockStatus.UNKNOWN,
            error_message="Could not determine stock status from page",
        )

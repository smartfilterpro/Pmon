"""Discord webhook notifications."""

from __future__ import annotations

import logging

import httpx

from pmon.models import StockResult, CheckoutResult, CheckoutStatus
from .base import BaseNotifier

logger = logging.getLogger(__name__)


class DiscordNotifier(BaseNotifier):
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self._client = httpx.AsyncClient(timeout=10.0)

    async def _send(self, payload: dict):
        if not self.webhook_url:
            return
        try:
            resp = await self._client.post(self.webhook_url, json=payload)
            if resp.status_code == 429:
                logger.warning("Discord rate limited, notification may be delayed")
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to send Discord notification: {e}")

    async def notify_in_stock(self, result: StockResult):
        embed = {
            "title": f"🟢 IN STOCK: {result.product_name}",
            "color": 0x00FF00,
            "fields": [
                {"name": "Retailer", "value": result.retailer.title(), "inline": True},
                {"name": "Price", "value": result.price or "N/A", "inline": True},
                {"name": "URL", "value": result.url},
            ],
            "timestamp": result.timestamp.isoformat(),
        }
        await self._send({"embeds": [embed]})

    async def notify_checkout(self, result: CheckoutResult):
        if result.status == CheckoutStatus.SUCCESS:
            color = 0x00FF00
            title = f"✅ CHECKOUT SUCCESS: {result.product_name}"
        else:
            color = 0xFF0000
            title = f"❌ CHECKOUT FAILED: {result.product_name}"

        fields = [
            {"name": "Retailer", "value": result.retailer.title(), "inline": True},
        ]
        if result.order_number:
            fields.append({"name": "Order #", "value": result.order_number, "inline": True})
        if result.error_message:
            fields.append({"name": "Error", "value": result.error_message})

        embed = {
            "title": title,
            "color": color,
            "fields": fields,
            "timestamp": result.timestamp.isoformat(),
        }
        await self._send({"embeds": [embed]})

    async def close(self):
        await self._client.aclose()

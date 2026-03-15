"""Walmart stock monitor."""

from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup

from pmon.models import StockResult, StockStatus
from .base import BaseMonitor

logger = logging.getLogger(__name__)


class WalmartMonitor(BaseMonitor):
    retailer_name = "walmart"

    async def check_stock(self, url: str, product_name: str) -> StockResult:
        client = await self.get_client()
        resp = await client.get(url)
        resp.raise_for_status()

        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        # Walmart embeds product data in __NEXT_DATA__ script tag
        next_data = soup.find("script", id="__NEXT_DATA__")
        if next_data and next_data.string:
            try:
                data = json.loads(next_data.string)
                return self._parse_next_data(url, product_name, data)
            except json.JSONDecodeError:
                logger.debug("Could not parse Walmart __NEXT_DATA__")

        # Fallback: check for stock indicators in HTML
        return self._parse_html(url, product_name, soup)

    def _parse_next_data(self, url: str, product_name: str, data: dict) -> StockResult:
        try:
            # Navigate the Next.js data structure to find availability
            props = data.get("props", {}).get("pageProps", {})
            initial_data = props.get("initialData", {}).get("data", {})
            product = initial_data.get("product", {})

            availability = product.get("availabilityStatus", "")
            price_info = product.get("priceInfo", {})
            price = price_info.get("currentPrice", {}).get("priceString", "")

            if availability == "IN_STOCK":
                return StockResult(
                    url=url,
                    retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.IN_STOCK,
                    price=price,
                )
            elif availability in ("OUT_OF_STOCK", "NOT_AVAILABLE"):
                return StockResult(
                    url=url,
                    retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.OUT_OF_STOCK,
                    price=price,
                )
        except (KeyError, TypeError):
            pass

        return StockResult(
            url=url,
            retailer=self.retailer_name,
            product_name=product_name,
            status=StockStatus.UNKNOWN,
            error_message="Could not parse Walmart product data",
        )

    def _parse_html(self, url: str, product_name: str, soup: BeautifulSoup) -> StockResult:
        # Check for Add to Cart button
        add_btn = soup.find("button", attrs={"data-tl-id": re.compile(r"addToCart", re.I)})
        if not add_btn:
            add_btn = soup.find("button", string=re.compile(r"add to cart", re.I))

        if add_btn and not add_btn.get("disabled"):
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.IN_STOCK,
                price=self._extract_price(soup),
            )

        # Check for out of stock indicators
        oos = soup.find(string=re.compile(r"(out of stock|get in-stock alert)", re.I))
        if oos:
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
            error_message="Could not determine stock status",
        )

    def _extract_price(self, soup: BeautifulSoup) -> str:
        price_el = soup.find(attrs={"itemprop": "price"})
        if price_el:
            return price_el.get("content", "") or price_el.get_text(strip=True)
        return ""

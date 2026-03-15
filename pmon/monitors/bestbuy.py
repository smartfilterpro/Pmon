"""Best Buy stock monitor."""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from pmon.models import StockResult, StockStatus
from .base import BaseMonitor

logger = logging.getLogger(__name__)


class BestBuyMonitor(BaseMonitor):
    retailer_name = "bestbuy"

    def _extract_sku(self, url: str) -> str | None:
        """Extract SKU from Best Buy URL."""
        # URLs look like: /site/product-name/1234567.p
        match = re.search(r"/(\d{7})\.p", url)
        return match.group(1) if match else None

    async def check_stock(self, url: str, product_name: str) -> StockResult:
        sku = self._extract_sku(url)
        client = await self.get_client()

        # Try the API endpoint first
        if sku:
            try:
                api_url = f"https://www.bestbuy.com/api/3.0/priceBlocks"
                params = {"skus": sku}
                resp = await client.get(api_url, params=params)
                if resp.status_code == 200:
                    return self._parse_api_response(url, product_name, resp.json())
            except Exception as e:
                logger.debug(f"Best Buy API failed for {sku}: {e}")

        # Fallback: scrape the product page
        resp = await client.get(url)
        resp.raise_for_status()
        return self._parse_page(url, product_name, resp.text)

    def _parse_api_response(self, url: str, product_name: str, data: dict) -> StockResult:
        try:
            items = data if isinstance(data, list) else [data]
            for item in items:
                button_state = item.get("buttonState", {})
                state = button_state.get("buttonState", "")

                price = item.get("price", {}).get("currentPrice", "")
                if price:
                    price = f"${price}"

                if state == "ADD_TO_CART":
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
            )
        except (KeyError, TypeError, IndexError):
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.UNKNOWN,
                error_message="Could not parse Best Buy API response",
            )

    def _parse_page(self, url: str, product_name: str, html: str) -> StockResult:
        soup = BeautifulSoup(html, "html.parser")

        # Check for Add to Cart button
        add_btn = soup.find("button", class_=re.compile(r"add-to-cart", re.I))
        if add_btn:
            btn_text = add_btn.get_text(strip=True).lower()
            if "add to cart" in btn_text:
                return StockResult(
                    url=url,
                    retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.IN_STOCK,
                    price=self._extract_price(soup),
                )

        # Check for Sold Out button
        sold_out = soup.find("button", string=re.compile(r"sold out", re.I))
        if sold_out:
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.OUT_OF_STOCK,
            )

        # Check for invitation-only (Best Buy's new Pokemon system)
        invite = soup.find(string=re.compile(r"(get your invite|invitation)", re.I))
        if invite:
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.OUT_OF_STOCK,
                error_message="Invitation-only product",
            )

        # Check structured data
        for script in soup.find_all("script", type="application/ld+json"):
            text = script.string or ""
            if '"availability"' in text:
                if '"InStock"' in text:
                    return StockResult(
                        url=url,
                        retailer=self.retailer_name,
                        product_name=product_name,
                        status=StockStatus.IN_STOCK,
                        price=self._extract_price(soup),
                    )

        return StockResult(
            url=url,
            retailer=self.retailer_name,
            product_name=product_name,
            status=StockStatus.UNKNOWN,
            error_message="Could not determine stock status",
        )

    def _extract_price(self, soup: BeautifulSoup) -> str:
        price_el = soup.find(class_=re.compile(r"priceView-customer-price", re.I))
        if price_el:
            span = price_el.find("span")
            if span:
                return span.get_text(strip=True)
        return ""

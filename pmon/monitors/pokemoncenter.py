"""Pokemon Center stock monitor."""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from pmon.models import StockResult, StockStatus
from .base import BaseMonitor

logger = logging.getLogger(__name__)


class PokemonCenterMonitor(BaseMonitor):
    retailer_name = "pokemoncenter"

    async def check_stock(self, url: str, product_name: str) -> StockResult:
        client = await self.get_client()
        resp = await client.get(url)
        resp.raise_for_status()

        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        # Pokemon Center uses various indicators for stock status
        # Check for "Add to Cart" button
        add_to_cart = soup.find("button", string=re.compile(r"add to cart", re.I))
        if add_to_cart and not add_to_cart.get("disabled"):
            price = self._extract_price(soup)
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.IN_STOCK,
                price=price,
            )

        # Check for "Out of Stock" or "Sold Out" text
        out_of_stock = soup.find(string=re.compile(r"(out of stock|sold out|unavailable)", re.I))
        if out_of_stock:
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.OUT_OF_STOCK,
            )

        # Check for product data in script tags (PKC often loads via JS)
        for script in soup.find_all("script"):
            text = script.string or ""
            if '"availability"' in text:
                if '"InStock"' in text or '"instock"' in text.lower():
                    return StockResult(
                        url=url,
                        retailer=self.retailer_name,
                        product_name=product_name,
                        status=StockStatus.IN_STOCK,
                        price=self._extract_price(soup),
                    )
                if '"OutOfStock"' in text:
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
        price_el = soup.find(class_=re.compile(r"price", re.I))
        if price_el:
            text = price_el.get_text(strip=True)
            match = re.search(r"\$[\d,.]+", text)
            if match:
                return match.group()
        return ""

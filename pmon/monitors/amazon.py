"""Amazon stock & price monitor.

Checks product availability and price by scraping the Amazon product page.
Extracts data from the embedded JSON-LD structured data and HTML fallbacks.
Importantly, it checks the *seller* to distinguish Amazon-sold items from
expensive third-party listings.
"""

from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup

from pmon.models import StockResult, StockStatus
from .base import BaseMonitor

logger = logging.getLogger(__name__)


class AmazonMonitor(BaseMonitor):
    retailer_name = "amazon"
    # Amazon rate-limits aggressively — be conservative
    _min_request_interval: float = 5.0
    _cookie_domain: str = ".amazon.com"

    def _extract_asin(self, url: str) -> str | None:
        """Extract ASIN from Amazon URL.

        Formats:
          /dp/B0G78HDPPR
          /gp/product/B0G78HDPPR
          /dp/B0G78HDPPR?tag=...
        """
        match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url, re.I)
        return match.group(1) if match else None

    async def check_stock(self, url: str, product_name: str) -> StockResult:
        await self.throttle()
        client = await self.get_client()

        asin = self._extract_asin(url)
        if not asin:
            return StockResult(
                url=url, retailer=self.retailer_name, product_name=product_name,
                status=StockStatus.ERROR,
                error_message=f"Could not extract ASIN from URL: {url}",
            )

        resp = await client.get(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        )

        if resp.status_code == 503:
            return StockResult(
                url=url, retailer=self.retailer_name, product_name=product_name,
                status=StockStatus.ERROR,
                error_message="Amazon returned 503 — bot detection triggered. Try importing session cookies.",
            )

        if resp.status_code == 429:
            self.record_rate_limit()
            return StockResult(
                url=url, retailer=self.retailer_name, product_name=product_name,
                status=StockStatus.ERROR,
                error_message=f"Rate limited by Amazon (429). Cooling down for {self.rate_limit_remaining():.0f}s.",
            )

        if resp.status_code != 200:
            return StockResult(
                url=url, retailer=self.retailer_name, product_name=product_name,
                status=StockStatus.ERROR,
                error_message=f"Amazon HTTP {resp.status_code}",
            )

        self.record_success()
        html = resp.text

        # Check for CAPTCHA / bot detection page
        if "captcha" in html.lower() or "sorry, we just need to make sure" in html.lower():
            return StockResult(
                url=url, retailer=self.retailer_name, product_name=product_name,
                status=StockStatus.ERROR,
                error_message="Amazon CAPTCHA triggered. Import session cookies via Settings > Session Cookies.",
            )

        soup = BeautifulSoup(html, "html.parser")

        # Extract product name from page if not provided
        if not product_name:
            title_el = soup.find("span", id="productTitle")
            if title_el:
                product_name = title_el.get_text(strip=True)

        price = self._extract_price(soup)
        seller = self._extract_seller(soup)
        image_url = self._extract_image(soup)
        availability = self._extract_availability(soup)

        # Determine stock status
        if availability is None:
            # Try to infer from add-to-cart button
            add_to_cart = soup.find("input", id="add-to-cart-button")
            if add_to_cart:
                status = StockStatus.IN_STOCK
            else:
                buy_now = soup.find("input", id="buy-now-button")
                status = StockStatus.IN_STOCK if buy_now else StockStatus.OUT_OF_STOCK
        elif "in stock" in availability.lower():
            status = StockStatus.IN_STOCK
        elif "unavailable" in availability.lower() or "out of stock" in availability.lower():
            status = StockStatus.OUT_OF_STOCK
        elif "available from" in availability.lower():
            # Third-party only — might be overpriced
            status = StockStatus.IN_STOCK
        else:
            status = StockStatus.UNKNOWN

        # Build price string with seller info for the dashboard
        price_display = price
        if seller and "amazon" not in seller.lower():
            price_display = f"{price} (via {seller})" if price else f"(via {seller})"

        return StockResult(
            url=url,
            retailer=self.retailer_name,
            product_name=product_name,
            status=status,
            price=price_display,
            image_url=image_url,
        )

    def _extract_price(self, soup: BeautifulSoup) -> str:
        """Extract the current price from the Amazon product page."""
        # Try the main price display (whole + fraction)
        whole = soup.find("span", class_="a-price-whole")
        fraction = soup.find("span", class_="a-price-fraction")
        if whole and fraction:
            w = whole.get_text(strip=True).rstrip(".")
            f = fraction.get_text(strip=True)
            return f"${w}.{f}"

        # Try the combined price span
        price_span = soup.find("span", class_="a-price")
        if price_span:
            offscreen = price_span.find("span", class_="a-offscreen")
            if offscreen:
                return offscreen.get_text(strip=True)

        # Try deal price
        deal = soup.find("span", id="priceblock_dealprice")
        if deal:
            return deal.get_text(strip=True)

        # Try regular price
        regular = soup.find("span", id="priceblock_ourprice")
        if regular:
            return regular.get_text(strip=True)

        return ""

    def _extract_seller(self, soup: BeautifulSoup) -> str:
        """Extract who is selling/shipping the item."""
        # "Ships from and sold by" section
        merchant = soup.find("div", id="merchant-info")
        if merchant:
            return merchant.get_text(strip=True)

        # Tabular merchant info
        sold_by = soup.find("a", id="sellerProfileTriggerId")
        if sold_by:
            return sold_by.get_text(strip=True)

        # "Sold by" in the buybox
        for span in soup.find_all("span", class_="tabular-buybox-text"):
            text = span.get_text(strip=True)
            if text and text != "Amazon.com":
                parent = span.find_parent("div", class_="tabular-buybox-container")
                if parent and "sold by" in parent.get_text().lower():
                    return text

        return ""

    def _extract_image(self, soup: BeautifulSoup) -> str:
        """Extract the main product image URL."""
        img = soup.find("img", id="landingImage")
        if img:
            return img.get("src", "")
        # Fallback: look in image block
        img = soup.find("img", id="imgBlkFront")
        if img:
            return img.get("src", "")
        return ""

    def _extract_availability(self, soup: BeautifulSoup) -> str | None:
        """Extract the availability text (e.g., 'In Stock', 'Currently unavailable')."""
        avail_div = soup.find("div", id="availability")
        if avail_div:
            text = avail_div.get_text(strip=True)
            if text:
                return text
        return None

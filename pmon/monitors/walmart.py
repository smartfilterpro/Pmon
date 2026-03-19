"""Walmart stock monitor."""

from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup

from pmon.models import StockResult, StockStatus
from .base import API_HEADERS, BaseMonitor

logger = logging.getLogger(__name__)


def _parse_retry_after(resp) -> float | None:
    """Extract Retry-After header value in seconds, or None if absent."""
    val = resp.headers.get("Retry-After")
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


class WalmartMonitor(BaseMonitor):
    retailer_name = "walmart"
    # Walmart aggressively rate-limits — enforce at least 5s between requests
    _min_request_interval: float = 5.0

    def _extract_product_id(self, url: str) -> str | None:
        """Extract product/item ID from Walmart URL.

        Formats: /ip/product-name/123456789, /ip/123456789
        """
        match = re.search(r"/ip/[^/]*/(\d+)", url) or re.search(r"/ip/(\d+)", url)
        return match.group(1) if match else None

    async def check_stock(self, url: str, product_name: str) -> StockResult:
        client = await self.get_client()

        # Try Walmart's internal product API first (less likely to be blocked than page scrape)
        product_id = self._extract_product_id(url)
        if product_id:
            result = await self._check_via_api(client, url, product_name, product_id)
            if result and result.status != StockStatus.UNKNOWN:
                return result

        # Fallback: scrape the product page
        resp = await client.get(
            url,
            headers={
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            },
        )

        if resp.status_code == 429:
            retry_after = _parse_retry_after(resp)
            self.record_rate_limit(retry_after)
            return StockResult(
                url=url, retailer=self.retailer_name, product_name=product_name,
                status=StockStatus.ERROR,
                error_message=f"Rate limited by Walmart (429). Cooling down for {self.rate_limit_remaining():.0f}s.",
            )

        if resp.status_code == 403:
            logger.warning("Walmart: 403 on product page — PerimeterX blocked")
            return StockResult(
                url=url, retailer=self.retailer_name, product_name=product_name,
                status=StockStatus.ERROR,
                error_message="Blocked by Walmart (403). Import session cookies to improve reliability.",
            )

        resp.raise_for_status()
        self.record_success()

        html = resp.text

        # Check if we got a block page instead of product data
        if "/blocked" in resp.url.path or "press & hold" in html.lower():
            logger.warning("Walmart: redirected to CAPTCHA block page")
            return StockResult(
                url=url, retailer=self.retailer_name, product_name=product_name,
                status=StockStatus.ERROR,
                error_message="Walmart CAPTCHA block. Import session cookies via Settings > Session Cookies.",
            )

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
                price=self._extract_price(soup),
            )

        return StockResult(
            url=url,
            retailer=self.retailer_name,
            product_name=product_name,
            status=StockStatus.UNKNOWN,
            error_message="Could not determine stock status",
        )

    async def _check_via_api(
        self, client, url: str, product_name: str, product_id: str
    ) -> StockResult | None:
        """Try Walmart's internal product API (less blocked than full page scrape).

        Uses the same endpoint that Walmart's frontend calls via fetch() when
        rendering product pages on the client side.
        """
        try:
            resp = await client.get(
                f"https://www.walmart.com/orchestra/home/graphql/GetProductByItemId/"
                f"54e7e0bcfcab0d31a9e67f63ce68b54edc3fa3d0bd67f6f7e1aaaf2e4e564a5c",
                params={"variables": json.dumps({"itemId": product_id})},
                headers={
                    **API_HEADERS,
                    "Referer": url,
                    "Origin": "https://www.walmart.com",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-origin",
                    "x-o-platform": "rweb",
                    "x-o-correlation-id": f"pmon-{product_id}",
                },
            )

            if resp.status_code == 429:
                retry_after = _parse_retry_after(resp)
                self.record_rate_limit(retry_after)
                return StockResult(
                    url=url, retailer=self.retailer_name, product_name=product_name,
                    status=StockStatus.ERROR,
                    error_message=f"Rate limited by Walmart (429). Cooling down for {self.rate_limit_remaining():.0f}s.",
                )

            if resp.status_code != 200:
                logger.debug("Walmart API returned %d for product %s", resp.status_code, product_id)
                return None

            self.record_success()
            data = resp.json()
            # Navigate the GraphQL response structure
            product = (
                data.get("data", {})
                .get("product", {})
            )
            if not product:
                return None

            availability = product.get("availabilityStatus", "")
            price_info = product.get("priceInfo", {})
            price = price_info.get("currentPrice", {}).get("priceString", "")

            if availability == "IN_STOCK":
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.IN_STOCK, price=price,
                )
            elif availability in ("OUT_OF_STOCK", "NOT_AVAILABLE"):
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.OUT_OF_STOCK, price=price,
                )

        except Exception as e:
            logger.debug("Walmart API check failed for %s: %s", product_id, e)

        return None

    def _extract_price(self, soup: BeautifulSoup) -> str:
        price_el = soup.find(attrs={"itemprop": "price"})
        if price_el:
            return price_el.get("content", "") or price_el.get_text(strip=True)
        return ""

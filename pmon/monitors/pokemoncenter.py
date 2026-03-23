"""Pokemon Center stock monitor — API-first with HTML fallback."""

from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup

from pmon.models import StockResult, StockStatus
from .base import BaseMonitor

logger = logging.getLogger(__name__)


class PokemonCenterMonitor(BaseMonitor):
    retailer_name = "pokemoncenter"

    # Pokemon Center aggressively blocks rapid requests — their WAF flags IPs
    # after just a few hits and serves a "unusual activity" block page.
    # Use a higher interval than the 2s default to stay under the radar.
    _min_request_interval: float = 8.0

    async def check_stock(self, url: str, product_name: str) -> StockResult:
        client = await self.get_client()

        # Try API/JSON approach first — this fetches the page and parses
        # embedded JSON data.  If it can't determine status, _last_html is
        # saved so the HTML fallback can reuse it without a second request.
        self._last_html: str | None = None
        result = await self._check_stock_api(client, url, product_name)
        if result and result.status != StockStatus.ERROR:
            return result

        # Fallback to HTML scraping — reuses _last_html to avoid a duplicate
        # request that doubles our fingerprint with Pokemon Center's WAF.
        return await self._check_stock_html(client, url, product_name)

    async def _check_stock_api(self, client, url: str, product_name: str) -> StockResult | None:
        """Check stock via embedded JSON data (avoids full HTML parsing, harder to block)."""
        try:
            resp = await client.get(url)

            # Detect block page — trigger exponential backoff so we stop
            # hammering the blocked endpoint and making the IP ban worse.
            if resp.status_code == 403:
                self.record_rate_limit()
                return StockResult(
                    url=url,
                    retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.ERROR,
                    error_message="Access blocked (403) — IP may be flagged, backing off",
                )

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                self.record_rate_limit(float(retry_after) if retry_after else None)
                return StockResult(
                    url=url,
                    retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.ERROR,
                    error_message="Rate limited (429) — backing off",
                )

            resp.raise_for_status()
            html = resp.text
            # Cache for HTML fallback so we don't make a second request
            self._last_html = html

            # Check for bot block in content (block page served as 200)
            lower = html.lower()
            if "unusual activity" in lower or "access to this page has been denied" in lower:
                self.record_rate_limit()
                return StockResult(
                    url=url,
                    retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.ERROR,
                    error_message="Access blocked — bot detection triggered, backing off",
                )

            # Strategy 1: JSON-LD structured data (schema.org Product)
            for match in re.finditer(
                r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
            ):
                try:
                    ld = json.loads(match.group(1))
                    if isinstance(ld, list):
                        ld = next((x for x in ld if x.get("@type") == "Product"), None)
                    if not isinstance(ld, dict) or ld.get("@type") != "Product":
                        continue

                    availability = ""
                    offers = ld.get("offers", {})
                    if isinstance(offers, dict):
                        availability = offers.get("availability", "")
                    elif isinstance(offers, list) and offers:
                        availability = offers[0].get("availability", "")

                    price = self._extract_price_from_offers(offers)
                    ld_image = self._extract_ld_image(ld)

                    if "InStock" in availability:
                        return StockResult(
                            url=url, retailer=self.retailer_name,
                            product_name=product_name,
                            status=StockStatus.IN_STOCK, price=price,
                            image_url=ld_image,
                        )
                    elif "OutOfStock" in availability or "SoldOut" in availability:
                        return StockResult(
                            url=url, retailer=self.retailer_name,
                            product_name=product_name,
                            status=StockStatus.OUT_OF_STOCK, price=price,
                            image_url=ld_image,
                        )
                except (json.JSONDecodeError, StopIteration):
                    continue

            # Strategy 2: __NEXT_DATA__ (Next.js hydration data)
            next_match = re.search(
                r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
            )
            if next_match:
                try:
                    nd = json.loads(next_match.group(1))
                    product = (
                        nd.get("props", {})
                        .get("pageProps", {})
                        .get("product", {})
                    )
                    if product:
                        avail = product.get("availability", "")
                        in_stock = product.get("inStock", product.get("isAvailable"))
                        price = product.get("price", "")
                        if isinstance(price, (int, float)):
                            price = f"${price:.2f}"
                        nd_image = ""
                        images = product.get("images", product.get("image", []))
                        if isinstance(images, list) and images:
                            nd_image = images[0] if isinstance(images[0], str) else images[0].get("url", "")
                        elif isinstance(images, str):
                            nd_image = images

                        if in_stock is True or "InStock" in str(avail):
                            return StockResult(
                                url=url, retailer=self.retailer_name,
                                product_name=product_name,
                                status=StockStatus.IN_STOCK,
                                price=str(price), image_url=nd_image,
                            )
                        elif in_stock is False or "OutOfStock" in str(avail):
                            return StockResult(
                                url=url, retailer=self.retailer_name,
                                product_name=product_name,
                                status=StockStatus.OUT_OF_STOCK,
                                price=str(price), image_url=nd_image,
                            )
                except (json.JSONDecodeError, AttributeError):
                    pass

            # Strategy 3: Inline JS variables (window.__PRODUCT_DATA__, etc.)
            for pattern in [
                r'"availability"\s*:\s*"([^"]+)"',
                r'"stockStatus"\s*:\s*"([^"]+)"',
                r'"inStock"\s*:\s*(true|false)',
            ]:
                m = re.search(pattern, html, re.I)
                if m:
                    val = m.group(1).lower()
                    if val in ("instock", "in_stock", "true", "available"):
                        return StockResult(
                            url=url, retailer=self.retailer_name,
                            product_name=product_name,
                            status=StockStatus.IN_STOCK,
                            price=self._extract_price_from_html(html),
                        )
                    elif val in ("outofstock", "out_of_stock", "false", "soldout", "unavailable"):
                        return StockResult(
                            url=url, retailer=self.retailer_name,
                            product_name=product_name,
                            status=StockStatus.OUT_OF_STOCK,
                            price=self._extract_price_from_html(html),
                        )

            # Request succeeded (no block), reset backoff counter
            self.record_success()
            return None  # Couldn't determine from API data, try HTML fallback

        except Exception as exc:
            logger.debug("Pokemon Center API stock check failed: %s", exc)
            return None

    async def _check_stock_html(self, client, url: str, product_name: str) -> StockResult:
        """Fallback: check stock via HTML content parsing.

        Reuses HTML from the API check when available to avoid a second request.
        """
        try:
            html = self._last_html
            if not html:
                resp = await client.get(url)
                resp.raise_for_status()
                html = resp.text

            soup = BeautifulSoup(html, "html.parser")

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
            out_of_stock = soup.find(
                string=re.compile(r"(out of stock|sold out|unavailable)", re.I)
            )
            if out_of_stock:
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

        except Exception as exc:
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.ERROR,
                error_message=str(exc),
            )

    @staticmethod
    def _extract_ld_image(ld: dict) -> str:
        """Extract image URL from a JSON-LD Product object."""
        img = ld.get("image", "")
        if isinstance(img, list) and img:
            return img[0] if isinstance(img[0], str) else img[0].get("url", "")
        return img if isinstance(img, str) else ""

    def _extract_price_from_offers(self, offers) -> str:
        """Extract price from JSON-LD offers."""
        if isinstance(offers, dict):
            price = offers.get("price", "")
            currency = offers.get("priceCurrency", "USD")
            if price:
                return f"${price}" if currency == "USD" else f"{price} {currency}"
        elif isinstance(offers, list) and offers:
            return self._extract_price_from_offers(offers[0])
        return ""

    def _extract_price_from_html(self, html: str) -> str:
        """Quick regex price extraction from raw HTML."""
        m = re.search(r'"price"\s*:\s*"?(\$?[\d,.]+)"?', html)
        if m:
            price = m.group(1)
            return price if price.startswith("$") else f"${price}"
        return ""

    def _extract_price(self, soup: BeautifulSoup) -> str:
        price_el = soup.find(class_=re.compile(r"price", re.I))
        if price_el:
            text = price_el.get_text(strip=True)
            match = re.search(r"\$[\d,.]+", text)
            if match:
                return match.group()
        return ""

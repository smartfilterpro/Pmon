"""Costco stock monitor.

Uses Costco's batch inventory API (ecom-api.costco.com) to check stock status.
Falls back to HTML scraping of product pages when the API is unavailable.

Costco uses:
- JSP/WCS (WebSphere Commerce) frontend with server-side rendering
- Batch inventory API at ecom-api.costco.com/ebusiness/inventory/v1/
- Embedded product data in JS variables (window.digitalData, var products)
- Akamai bot detection (mPulse boomerang)
- Queue-it for high-traffic events

Key quirks:
- Costco requires membership for most online purchases
- Product URLs contain item numbers (e.g. .product.1234567.html or /p/1234567)
- Stock data is embedded in page JS as ``var products`` and ``window.digitalData``
- Price may be base64-encoded in ``listPrice`` field; use digitalData.priceMin instead
- Session cookies from a logged-in member account are needed for member pricing
"""

from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup

from pmon.models import StockResult, StockStatus
from .base import API_HEADERS, BaseMonitor

logger = logging.getLogger(__name__)


class CostcoMonitor(BaseMonitor):
    retailer_name = "costco"

    # Costco is aggressive with rate limiting — use a conservative interval
    _min_request_interval: float = 5.0

    # Costco's batch inventory API (real endpoint from their frontend)
    INVENTORY_URL = "https://ecom-api.costco.com/ebusiness/inventory/v1/inventorylevels/availability/batch/v2"
    CLIENT_ID = "481b1aec-aa3b-454b-b81b-48187e28f205"

    def __init__(self):
        super().__init__()
        self._warmed_up: bool = False

    def _extract_item_number(self, url: str) -> str | None:
        """Extract Costco item number from URL.

        Supported formats:
        - https://www.costco.com/product-name.product.1234567.html
        - https://www.costco.com/p/1234567
        - https://www.costco.com/.product.1234567.html
        - Query param: ?itemNo=1234567
        """
        # Format: .product.1234567.html
        match = re.search(r"\.product\.(\d+)\.html", url)
        if match:
            return match.group(1)

        # Format: /p/1234567
        match = re.search(r"/p/(\d+)", url)
        if match:
            return match.group(1)

        # Query param: itemNo=1234567
        match = re.search(r"[?&]itemNo=(\d+)", url)
        if match:
            return match.group(1)

        # Fallback: any 6-8 digit number in the URL path
        match = re.search(r"/(\d{6,8})(?:[./]|$)", url)
        if match:
            return match.group(1)

        return None

    def _inventory_headers(self, url: str) -> dict:
        """Build headers matching Costco's real frontend inventory API requests."""
        return {
            **API_HEADERS,
            "Content-Type": "application/json",
            "client-identifier": self.CLIENT_ID,
            "Referer": url,
            "Origin": "https://www.costco.com",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }

    async def _warm_up(self, client):
        """Visit Costco homepage to establish Akamai session cookies."""
        if self._warmed_up:
            return
        try:
            resp = await client.get(
                "https://www.costco.com/",
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                },
            )
            if resp.status_code == 200:
                self._warmed_up = True
                logger.debug("Costco: warm-up visit OK, cookies established")
        except Exception as e:
            logger.debug("Costco: warm-up visit failed: %s", e)

    async def check_stock(self, url: str, product_name: str) -> StockResult:
        item_number = self._extract_item_number(url)
        if not item_number:
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name, status=StockStatus.ERROR,
                error_message="Could not extract item number from Costco URL",
            )

        client = await self.get_client()
        await self._warm_up(client)

        # --- Strategy 1: Batch inventory API ---
        result = await self._check_via_inventory_api(client, url, product_name, item_number)
        if result and result.status != StockStatus.UNKNOWN:
            return result

        # --- Strategy 2: HTML page scrape ---
        logger.debug("Costco: falling back to page scrape for %s", product_name)
        return await self._scrape_page(client, url, product_name, item_number)

    async def _check_via_inventory_api(
        self, client, url: str, product_name: str, item_number: str
    ) -> StockResult | None:
        """Check stock via Costco's batch inventory API.

        This is the real API that costco.com's frontend calls to check
        availability. It requires the client-identifier header.
        """
        try:
            resp = await client.post(
                self.INVENTORY_URL,
                json=[{
                    "productId": item_number,
                    "partNumber": item_number,
                }],
                headers=self._inventory_headers(url),
            )

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                self.record_rate_limit(float(retry_after) if retry_after else None)
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name, status=StockStatus.ERROR,
                    error_message="Rate limited (429)",
                )

            if resp.status_code == 403:
                logger.warning("Costco: Inventory API 403 — Akamai blocked, rotating session")
                self._warmed_up = False
                return None

            if resp.status_code != 200:
                logger.debug("Costco: Inventory API returned %d for item %s", resp.status_code, item_number)
                return None

            data = resp.json()
            return self._parse_inventory_response(url, product_name, data)

        except Exception as e:
            logger.debug("Costco: Inventory API failed for item %s: %s", item_number, e)
            return None

    def _parse_inventory_response(self, url: str, product_name: str, data) -> StockResult:
        """Parse the batch inventory API response."""
        try:
            # Response is a list of inventory items or an object with items
            items = data if isinstance(data, list) else data.get("inventoryItems", [data])
            if not items:
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name, status=StockStatus.UNKNOWN,
                    error_message="No items in inventory API response",
                )

            item = items[0] if isinstance(items, list) else items

            # The API can return various status fields
            inv_status = (
                item.get("inventoryStatus", "")
                or item.get("status", "")
                or item.get("availabilityStatus", "")
            ).upper()

            price = ""
            price_val = item.get("price") or item.get("finalPrice")
            if price_val is not None:
                price = f"${price_val}"

            if inv_status in ("IN_STOCK", "AVAILABLE"):
                self.record_success()
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.IN_STOCK, price=price,
                )
            if inv_status in ("OUT_OF_STOCK", "UNAVAILABLE", "SOLD_OUT", "NOT_AVAILABLE"):
                self.record_success()
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.OUT_OF_STOCK, price=price,
                )

            # Broad string match on the response
            data_str = json.dumps(data)
            if '"IN_STOCK"' in data_str or '"AVAILABLE"' in data_str:
                self.record_success()
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.IN_STOCK, price=price,
                )
            if '"OUT_OF_STOCK"' in data_str or '"SOLD_OUT"' in data_str:
                self.record_success()
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.OUT_OF_STOCK, price=price,
                )

            # Could not determine from API — fall through to page scrape
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name, status=StockStatus.UNKNOWN,
                error_message="Could not determine stock from inventory API",
            )

        except (KeyError, TypeError, IndexError) as e:
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name, status=StockStatus.UNKNOWN,
                error_message=f"Could not parse Costco inventory data: {e}",
            )

    async def _scrape_page(
        self, client, url: str, product_name: str, item_number: str
    ) -> StockResult:
        """Scrape the Costco product page for stock status.

        Costco uses JSP/WCS server-side rendering. Key data sources:
        - ``window.digitalData`` — contains inventoryStatus, priceMin/priceMax
        - ``var products`` — JS array with per-SKU inventory and price
        - ``var product`` — JS array with product-level metadata
        - Add-to-cart button presence/disabled state
        """
        try:
            resp = await client.get(
                url,
                headers={
                    "Referer": "https://www.costco.com/",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-User": "?1",
                },
            )

            if resp.status_code == 403:
                logger.warning("Costco: 403 on page scrape — Akamai blocked")
                self._warmed_up = False
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name, status=StockStatus.ERROR,
                    error_message="Blocked by Akamai (403) — will retry with new session",
                )

            if resp.status_code == 404:
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name, status=StockStatus.OUT_OF_STOCK,
                    error_message="Product page not found (404)",
                )

            resp.raise_for_status()
            html = resp.text

            # Strategy 1: window.digitalData (most reliable on Costco JSP pages)
            # Contains inventoryStatus ('in stock' / 'out of stock') and pricing
            result = self._parse_digital_data(url, product_name, html)
            if result and result.status != StockStatus.UNKNOWN:
                return result

            # Strategy 2: var products JS array
            # Contains per-SKU data: "inventory" : "IN_STOCK", price, etc.
            result = self._parse_products_array(url, product_name, html)
            if result and result.status != StockStatus.UNKNOWN:
                return result

            soup = BeautifulSoup(html, "html.parser")

            # Strategy 3: JSON-LD structured data
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    ld_data = json.loads(script.string or "")
                    items = ld_data if isinstance(ld_data, list) else [ld_data]
                    for item in items:
                        if item.get("@type") != "Product":
                            continue
                        offers = item.get("offers", {})
                        offer_list = offers if isinstance(offers, list) else [offers]
                        for offer in offer_list:
                            avail = offer.get("availability", "")
                            price = offer.get("price", "")
                            if price:
                                price = f"${price}" if not str(price).startswith("$") else str(price)

                            if "InStock" in avail:
                                self.record_success()
                                return StockResult(
                                    url=url, retailer=self.retailer_name,
                                    product_name=product_name,
                                    status=StockStatus.IN_STOCK, price=price,
                                )
                            elif "OutOfStock" in avail:
                                self.record_success()
                                return StockResult(
                                    url=url, retailer=self.retailer_name,
                                    product_name=product_name,
                                    status=StockStatus.OUT_OF_STOCK, price=price,
                                )
                except (json.JSONDecodeError, TypeError, AttributeError):
                    continue

            # Strategy 4: Embedded script data (inventoryAvailable or inventory status)
            for script in soup.find_all("script"):
                text = script.string or ""
                if not text or len(text) < 20:
                    continue

                # Check for inventoryAvailable boolean
                if "inventoryAvailable" in text:
                    price = self._extract_script_price(text)
                    if re.search(r'["\']?inventoryAvailable["\']?\s*[:=]\s*true', text, re.I):
                        self.record_success()
                        return StockResult(
                            url=url, retailer=self.retailer_name,
                            product_name=product_name,
                            status=StockStatus.IN_STOCK, price=price,
                        )
                    if re.search(r'["\']?inventoryAvailable["\']?\s*[:=]\s*false', text, re.I):
                        self.record_success()
                        return StockResult(
                            url=url, retailer=self.retailer_name,
                            product_name=product_name,
                            status=StockStatus.OUT_OF_STOCK, price=price,
                        )

                # Check for inventory status string
                if '"inventory"' in text or "'inventory'" in text:
                    price = self._extract_script_price(text)
                    inv_match = re.search(
                        r"""['"]\s*inventory\s*['"]\s*:\s*['"]([\w_]+)['"]""", text
                    )
                    if inv_match:
                        inv_val = inv_match.group(1).upper()
                        if inv_val in ("IN_STOCK", "AVAILABLE"):
                            self.record_success()
                            return StockResult(
                                url=url, retailer=self.retailer_name,
                                product_name=product_name,
                                status=StockStatus.IN_STOCK, price=price,
                            )
                        if inv_val in ("OUT_OF_STOCK", "UNAVAILABLE", "SOLD_OUT"):
                            self.record_success()
                            return StockResult(
                                url=url, retailer=self.retailer_name,
                                product_name=product_name,
                                status=StockStatus.OUT_OF_STOCK, price=price,
                            )

            # Strategy 5: Text-based detection
            page_price = self._extract_page_price(soup)
            if re.search(r"(out of stock|sold out|temporarily unavailable)", html, re.I):
                self.record_success()
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.OUT_OF_STOCK, price=page_price,
                )

            # Strategy 6: "Add to Cart" button presence
            add_btn = soup.find("input", attrs={"value": re.compile(r"add to cart", re.I)})
            if not add_btn:
                add_btn = soup.find("button", string=re.compile(r"add to cart", re.I))
            if add_btn and not add_btn.get("disabled"):
                self.record_success()
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.IN_STOCK, price=page_price,
                )

            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name, status=StockStatus.UNKNOWN,
                error_message="Could not determine stock status from Costco page",
            )

        except Exception as e:
            logger.error("Costco: page scrape failed for %s: %s", product_name, e)
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name, status=StockStatus.ERROR,
                error_message=str(e),
            )

    def _parse_digital_data(self, url: str, product_name: str, html: str) -> StockResult | None:
        """Parse ``window.digitalData`` embedded in Costco pages.

        This JS object contains::

            window.digitalData = {
                product: {
                    inventoryStatus: 'in stock',
                    priceMin: '89.99',
                    priceMax: '89.99',
                    pid: '4000271399',
                    ...
                },
            }
        """
        # Extract the inventoryStatus value
        inv_match = re.search(
            r"""inventoryStatus\s*:\s*['"]([^'"]+)['"]""", html
        )
        if not inv_match:
            return None

        inv_status = inv_match.group(1).strip().lower()

        # Extract price from priceMin
        price = ""
        price_match = re.search(r"""priceMin\s*:\s*['"]([^'"]+)['"]""", html)
        if price_match:
            price_val = price_match.group(1).strip()
            if price_val and price_val != "0":
                price = f"${price_val}"

        if inv_status in ("in stock", "in_stock", "available"):
            self.record_success()
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.IN_STOCK, price=price,
            )
        if inv_status in ("out of stock", "out_of_stock", "sold out", "unavailable"):
            self.record_success()
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.OUT_OF_STOCK, price=price,
            )

        logger.debug("Costco: unrecognized inventoryStatus in digitalData: %r", inv_status)
        return None

    def _parse_products_array(self, url: str, product_name: str, html: str) -> StockResult | None:
        """Parse the ``var products`` JS array embedded in Costco PDP pages.

        The array contains per-SKU objects with inventory status::

            var products = [[{
                "inventory" : "IN_STOCK",
                "price" : "",
                ...
            }]];
        """
        # Look for "inventory" : "VALUE" pattern in the products array region
        # This is distinct from digitalData — it's in the product JS block
        products_match = re.search(
            r'var\s+products\s*=\s*\[', html
        )
        if not products_match:
            return None

        # Search for inventory field after var products declaration
        remaining = html[products_match.start():]
        inv_match = re.search(
            r"""['"]\s*inventory\s*['"]\s*:\s*['"]([\w_]+)['"]""", remaining
        )
        if not inv_match:
            return None

        inv_status = inv_match.group(1).upper()

        # Try to get price from digitalData since products array often has empty price
        price = ""
        price_match = re.search(r"""priceMin\s*:\s*['"]([^'"]+)['"]""", html)
        if price_match:
            price_val = price_match.group(1).strip()
            if price_val and price_val != "0":
                price = f"${price_val}"

        if inv_status in ("IN_STOCK", "AVAILABLE"):
            self.record_success()
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.IN_STOCK, price=price,
            )
        if inv_status in ("OUT_OF_STOCK", "UNAVAILABLE", "SOLD_OUT"):
            self.record_success()
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.OUT_OF_STOCK, price=price,
            )

        logger.debug("Costco: unrecognized inventory in products array: %r", inv_status)
        return None

    @staticmethod
    def _extract_script_price(text: str) -> str:
        """Extract a price from an inline script block."""
        price_match = re.search(r"""['"]\s*price\s*['"]\s*:\s*['"]\$?([\d.]+)""", text)
        if price_match:
            return f"${price_match.group(1)}"
        return ""

    @staticmethod
    def _extract_page_price(soup: BeautifulSoup) -> str:
        """Extract a price from visible page elements."""
        price_el = soup.find(class_=re.compile(r"price", re.I))
        if price_el:
            price_match = re.search(r"\$[\d,]+\.?\d*", price_el.get_text())
            if price_match:
                return price_match.group()
        return ""

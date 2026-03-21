"""Costco stock monitor.

Uses Costco's GraphQL product API (ecom-api.costco.com) to check stock status.
Falls back to HTML scraping of product pages when the API is unavailable.

Costco uses:
- Next.js frontend (consumer-web) with server-side rendering
- GraphQL product API at ecom-api.costco.com/ebusiness/product/v1/products/graphql
- Akamai bot detection (mPulse boomerang)
- Queue-it for high-traffic events
- OAuth login at /OAuthLogonCmd

Key quirks:
- Costco requires membership for most online purchases
- Product URLs contain item numbers (e.g. .product.1234567.html or /p/1234567)
- The GraphQL API returns inventory, pricing, and fulfillment data
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

    # Costco's GraphQL product API
    GRAPHQL_URL = "https://ecom-api.costco.com/ebusiness/product/v1/products/graphql"

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

    def _graphql_headers(self, url: str) -> dict:
        """Build headers matching Costco's real frontend GraphQL requests."""
        return {
            **API_HEADERS,
            "Content-Type": "application/json",
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

        # --- Strategy 1: GraphQL product API ---
        result = await self._check_via_graphql(client, url, product_name, item_number)
        if result and result.status != StockStatus.UNKNOWN:
            return result

        # --- Strategy 2: HTML page scrape ---
        logger.info("Costco: falling back to page scrape for %s", product_name)
        return await self._scrape_page(client, url, product_name, item_number)

    async def _check_via_graphql(
        self, client, url: str, product_name: str, item_number: str
    ) -> StockResult | None:
        """Check stock via Costco's GraphQL product API."""
        # Costco's GraphQL API accepts product queries with item numbers.
        # The exact query shape is reverse-engineered from network captures.
        query = """
        query ProductQuery($itemNumber: String!) {
            product(itemNumber: $itemNumber) {
                itemNumber
                name
                active
                inventoryAvailable
                isPublished
                maxQty
                minQty
                onlineOnly
                price {
                    finalPrice
                    originalPrice
                    priceDisplay
                }
                inventory {
                    status
                    quantity
                    isBackorderable
                    isPreorderable
                }
                fulfillment {
                    deliveryAvailable
                    shippingAvailable
                }
            }
        }
        """

        try:
            resp = await client.post(
                self.GRAPHQL_URL,
                json={
                    "query": query,
                    "variables": {"itemNumber": item_number},
                },
                headers=self._graphql_headers(url),
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
                logger.warning("Costco: GraphQL API 403 — Akamai blocked, rotating session")
                self._warmed_up = False
                return None

            if resp.status_code != 200:
                logger.debug("Costco: GraphQL API returned %d for item %s", resp.status_code, item_number)
                return None

            data = resp.json()
            return self._parse_graphql_response(url, product_name, data)

        except Exception as e:
            logger.debug("Costco: GraphQL API failed for item %s: %s", item_number, e)
            return None

    def _parse_graphql_response(self, url: str, product_name: str, data: dict) -> StockResult:
        """Parse the GraphQL product response."""
        try:
            product = data.get("data", {}).get("product", {})
            if not product:
                # Check for errors in GraphQL response
                errors = data.get("errors", [])
                if errors:
                    logger.debug("Costco: GraphQL errors: %s", errors[:2])
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name, status=StockStatus.UNKNOWN,
                    error_message="No product data in GraphQL response",
                )

            # Extract price
            price = ""
            price_data = product.get("price", {})
            if isinstance(price_data, dict):
                price = price_data.get("priceDisplay", "")
                if not price:
                    final_price = price_data.get("finalPrice")
                    if final_price is not None:
                        price = f"${final_price}"

            # Check active/published status
            if product.get("active") is False or product.get("isPublished") is False:
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.OUT_OF_STOCK, price=price,
                )

            # Check inventory
            inventory = product.get("inventory", {})
            if isinstance(inventory, dict):
                inv_status = inventory.get("status", "").upper()
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
                # Preorder/backorder count as available
                if inventory.get("isPreorderable") or inventory.get("isBackorderable"):
                    self.record_success()
                    return StockResult(
                        url=url, retailer=self.retailer_name,
                        product_name=product_name,
                        status=StockStatus.IN_STOCK, price=price,
                    )
                # Check quantity
                qty = inventory.get("quantity", 0)
                if isinstance(qty, (int, float)) and qty > 0:
                    self.record_success()
                    return StockResult(
                        url=url, retailer=self.retailer_name,
                        product_name=product_name,
                        status=StockStatus.IN_STOCK, price=price,
                    )

            # Check inventoryAvailable (top-level boolean)
            if product.get("inventoryAvailable") is True:
                self.record_success()
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.IN_STOCK, price=price,
                )
            if product.get("inventoryAvailable") is False:
                self.record_success()
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.OUT_OF_STOCK, price=price,
                )

            # Check fulfillment
            fulfillment = product.get("fulfillment", {})
            if isinstance(fulfillment, dict):
                if fulfillment.get("deliveryAvailable") or fulfillment.get("shippingAvailable"):
                    self.record_success()
                    return StockResult(
                        url=url, retailer=self.retailer_name,
                        product_name=product_name,
                        status=StockStatus.IN_STOCK, price=price,
                    )

            # Broad string search across product JSON
            product_str = json.dumps(product)
            if any(s in product_str for s in ('"IN_STOCK"', '"AVAILABLE"', '"inventoryAvailable":true')):
                self.record_success()
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.IN_STOCK, price=price,
                )

            self.record_success()
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.OUT_OF_STOCK, price=price,
            )

        except (KeyError, TypeError) as e:
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name, status=StockStatus.UNKNOWN,
                error_message=f"Could not parse Costco GraphQL data: {e}",
            )

    async def _scrape_page(
        self, client, url: str, product_name: str, item_number: str
    ) -> StockResult:
        """Scrape the Costco product page for stock status."""
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
            soup = BeautifulSoup(html, "html.parser")

            # Strategy 1: JSON-LD structured data
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

            # Strategy 2: Next.js __NEXT_DATA__ (Costco uses Next.js)
            next_data_tag = soup.find("script", id="__NEXT_DATA__")
            if next_data_tag and next_data_tag.string:
                try:
                    nd = json.loads(next_data_tag.string)
                    props = nd.get("props", {}).get("pageProps", {})
                    product = props.get("product", {}) or props.get("initialData", {}).get("product", {})
                    if product:
                        return self._parse_next_data_product(url, product_name, product)
                except (json.JSONDecodeError, TypeError):
                    pass

            # Strategy 3: Embedded product data in scripts
            for script in soup.find_all("script"):
                text = script.string or ""
                if "inventoryAvailable" in text or "addToCartUrl" in text:
                    price = ""
                    price_match = re.search(r'"price"\s*:\s*"?\$?([\d.]+)', text)
                    if price_match:
                        price = f"${price_match.group(1)}"

                    if re.search(r'"inventoryAvailable"\s*:\s*true', text, re.I):
                        self.record_success()
                        return StockResult(
                            url=url, retailer=self.retailer_name,
                            product_name=product_name,
                            status=StockStatus.IN_STOCK, price=price,
                        )
                    if re.search(r'"inventoryAvailable"\s*:\s*false', text, re.I):
                        self.record_success()
                        return StockResult(
                            url=url, retailer=self.retailer_name,
                            product_name=product_name,
                            status=StockStatus.OUT_OF_STOCK, price=price,
                        )

            # Strategy 4: Text-based detection
            page_price = ""
            price_el = soup.find(class_=re.compile(r"price", re.I))
            if price_el:
                price_match = re.search(r"\$[\d,]+\.?\d*", price_el.get_text())
                if price_match:
                    page_price = price_match.group()

            if re.search(r"(out of stock|sold out|temporarily unavailable)", html, re.I):
                self.record_success()
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.OUT_OF_STOCK, price=page_price,
                )

            # Strategy 5: "Add to Cart" button presence
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

    def _parse_next_data_product(self, url: str, product_name: str, product: dict) -> StockResult:
        """Parse product data from Costco's Next.js __NEXT_DATA__."""
        price = ""
        price_data = product.get("price", product.get("pricing", {}))
        if isinstance(price_data, dict):
            price = price_data.get("priceDisplay", "") or price_data.get("finalPrice", "")
            if price and not str(price).startswith("$"):
                price = f"${price}"
        elif isinstance(price_data, (int, float)):
            price = f"${price_data}"

        # Check inventory fields
        if product.get("inventoryAvailable") is True:
            self.record_success()
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.IN_STOCK, price=price,
            )
        if product.get("inventoryAvailable") is False:
            self.record_success()
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.OUT_OF_STOCK, price=price,
            )

        # Check active status
        if product.get("active") is False:
            self.record_success()
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.OUT_OF_STOCK, price=price,
            )

        # Broad check
        product_str = json.dumps(product)
        if '"IN_STOCK"' in product_str or '"AVAILABLE"' in product_str:
            self.record_success()
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.IN_STOCK, price=price,
            )

        return StockResult(
            url=url, retailer=self.retailer_name,
            product_name=product_name, status=StockStatus.UNKNOWN,
            error_message="Could not determine stock from Next.js data",
        )

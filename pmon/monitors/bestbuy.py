"""Best Buy stock monitor."""

from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup

from pmon.models import StockResult, StockStatus
from .base import BaseMonitor

logger = logging.getLogger(__name__)


class BestBuyMonitor(BaseMonitor):
    retailer_name = "bestbuy"

    def _extract_sku(self, url: str) -> str | None:
        """Extract SKU from Best Buy URL.

        Handles multiple URL formats:
          Old: /site/product-name/1234567.p  or  /site/product-name/12345678.p
          New: /product/product-name/JJG2TLCK6H  (BSIN — no numeric SKU in URL)
        """
        # Old format: 7-8 digit SKU ending in .p
        match = re.search(r"/(\d{7,8})\.p", url)
        if match:
            return match.group(1)
        return None

    def _extract_bsin(self, url: str) -> str | None:
        """Extract BSIN from new Best Buy URL format.

        New URLs: /product/product-name/JJG2TLCK6H
        The BSIN is always the last path segment (alphanumeric, 6-12 chars).
        """
        match = re.search(r"/product/[^/]+/([A-Za-z0-9]{6,12})(?:\?|$|#|/|$)", url)
        if not match:
            # Handle URLs that may end without query/hash
            match = re.search(r"/product/[^/]+/([A-Za-z0-9]{6,12})\s*$", url)
        return match.group(1) if match else None

    async def _resolve_sku_from_page(self, url: str) -> str | None:
        """Fetch the product page and extract the SKU from embedded data.

        Best Buy's Next.js PDP pages embed product data in multiple places:
        - __NEXT_DATA__ script tags with skuId/sku fields
        - Meta tags with SKU content
        - Inline JSON with "skuId" or "sku" keys
        - The page title contains "SKU: XXXXXXXX" in the page metadata
        - GraphQL fulfillment calls reference the SKU

        SKUs are typically 7-8 digits (e.g. 10890190).
        """
        client = await self.get_client()
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text

            # Try __NEXT_DATA__ script tag (Next.js pages)
            match = re.search(r'"skuId"\s*:\s*"(\d{7,8})"', html)
            if match:
                return match.group(1)

            # Try sku in various JSON patterns
            match = re.search(r'"sku"\s*:\s*"(\d{7,8})"', html)
            if match:
                return match.group(1)

            # Try SKU: XXXXXXXX pattern in page content (visible in title/meta)
            match = re.search(r'SKU:\s*(\d{7,8})', html)
            if match:
                return match.group(1)

            # Try meta tags
            match = re.search(r'<meta[^>]*content="(\d{7,8})"[^>]*name="[^"]*sku[^"]*"', html, re.I)
            if match:
                return match.group(1)

            # Try og:url or canonical that might have old-format URL with SKU
            match = re.search(r'/(\d{7,8})\.p', html)
            if match:
                return match.group(1)

            # Try data attributes with SKU
            match = re.search(r'data-sku-id="(\d{7,8})"', html)
            if match:
                return match.group(1)

        except Exception as e:
            logger.debug("Best Buy: failed to resolve SKU from page %s: %s", url, e)
        return None

    async def check_stock(self, url: str, product_name: str) -> StockResult:
        sku = self._extract_sku(url)

        # For new-format URLs, resolve SKU from the page
        if not sku and self._extract_bsin(url):
            sku = await self._resolve_sku_from_page(url)
            if sku:
                logger.debug("Best Buy: resolved SKU %s from BSIN URL %s", sku, url)

        client = await self.get_client()

        # Primary: fulfillment GraphQL endpoint (works with new PDP)
        fulfillment_result = None
        if sku:
            try:
                fulfillment_result = await self._check_fulfillment_api(url, product_name, sku, client)
            except Exception as e:
                logger.debug("Best Buy fulfillment API failed for %s: %s", sku, e)

        # priceBlocks API — used for price even when fulfillment already got status
        price_result = None
        if sku:
            try:
                api_url = "https://www.bestbuy.com/api/3.0/priceBlocks"
                params = {"skus": sku}
                resp = await client.get(api_url, params=params)
                if resp.status_code == 200:
                    price_result = self._parse_api_response(url, product_name, resp.json())
            except Exception as e:
                logger.debug("Best Buy priceBlocks API failed for %s: %s", sku, e)

        # Combine: use fulfillment for status, priceBlocks for price
        if fulfillment_result and fulfillment_result.status != StockStatus.UNKNOWN:
            if not fulfillment_result.price and price_result and price_result.price:
                fulfillment_result.price = price_result.price
            return fulfillment_result
        if price_result and price_result.status != StockStatus.UNKNOWN:
            return price_result

        # Fallback: scrape the product page
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            return self._parse_page(url, product_name, resp.text)
        except Exception as e:
            logger.debug("Best Buy page scrape failed for %s: %s", url, e)

        return StockResult(
            url=url,
            retailer=self.retailer_name,
            product_name=product_name,
            status=StockStatus.UNKNOWN,
            error_message=f"Could not determine stock status (SKU: {sku or 'unknown'})",
        )

    async def _check_fulfillment_api(
        self, url: str, product_name: str, sku: str, client
    ) -> StockResult:
        """Check stock via Best Buy's fulfillment GraphQL endpoint.

        This is the same endpoint the PDP uses to render the Add to Cart button.
        The PDP makes multiple fulfillment calls:
        1. Basic: just sku + context=PDP (initial button state)
        2. With location: sku + zipCode + storeId (full fulfillment options)

        We use the basic call first since it doesn't require location data.
        """
        fulfillment_url = "https://www.bestbuy.com/gateway/graphql/fulfillment"

        # Basic call — just needs SKU and context
        variables = {
            "fulfillmentOptionsInput": {
                "sku": sku,
                "buttonState": {"context": "PDP"},
            }
        }
        params = {"variables": json.dumps(variables, separators=(",", ":"))}
        resp = await client.get(fulfillment_url, params=params)

        if resp.status_code != 200:
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.UNKNOWN,
                error_message=f"Fulfillment API returned {resp.status_code}",
            )

        data = resp.json()
        return self._parse_fulfillment_response(url, product_name, data)

    def _parse_fulfillment_response(self, url: str, product_name: str, data: dict) -> StockResult:
        """Parse the fulfillment GraphQL response for stock status.

        Known button states from Best Buy PDP:
        - ADD_TO_CART: Available for purchase (including third-party sellers)
        - SOLD_OUT: No stock anywhere
        - UNAVAILABLE: Not available in this region/config
        - CHECK_STORES: Might be available in stores only
        - RESERVATION: "High Demand Product" reservation/invite system
        - COMING_SOON: Not yet released
        - PRE_ORDER: Available for pre-order
        - SAVE: Wishlist only (cannot purchase)
        """
        try:
            # Navigate the GraphQL response structure
            ff_data = data.get("data", {}).get("fulfillmentOptions", data.get("data", {}))

            # Button state is the definitive answer
            button_state = ff_data.get("buttonState", {})
            state = button_state.get("buttonState", "")

            if not state:
                # Try alternate paths in the response
                for key in ("fulfillmentOptions", "fulfillment"):
                    nested = data.get("data", {}).get(key, {})
                    if isinstance(nested, dict):
                        state = nested.get("buttonState", {}).get("buttonState", "")
                        if state:
                            button_state = nested.get("buttonState", {})
                            break

            # Also search recursively as a last resort
            if not state:
                found = self._find_in_dict(data, "buttonState")
                if isinstance(found, dict):
                    state = found.get("buttonState", "")
                elif isinstance(found, str):
                    state = found

            # Try to extract price from fulfillment response
            price = ""
            price_val = self._find_in_dict(data, "currentPrice")
            if price_val and isinstance(price_val, (int, float)):
                price = f"${price_val}"
            elif price_val and isinstance(price_val, str) and price_val.replace(".", "").isdigit():
                price = f"${price_val}"
            if not price:
                price_val = self._find_in_dict(data, "customerPrice")
                if isinstance(price_val, str) and "$" in price_val:
                    price = price_val

            if state in ("ADD_TO_CART", "PRE_ORDER"):
                return StockResult(
                    url=url,
                    retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.IN_STOCK,
                    price=price,
                )
            elif state in ("SOLD_OUT", "UNAVAILABLE", "CHECK_STORES", "COMING_SOON", "SAVE"):
                return StockResult(
                    url=url,
                    retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.OUT_OF_STOCK,
                    price=price,
                    error_message=f"Button state: {state}" if state != "SOLD_OUT" else "",
                )
            elif state == "RESERVATION":
                return StockResult(
                    url=url,
                    retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.OUT_OF_STOCK,
                    price=price,
                    error_message="High Demand Product — reservation/invite required",
                )
            elif state:
                logger.debug("Best Buy fulfillment: button state = %s for %s", state, url)
                return StockResult(
                    url=url,
                    retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.OUT_OF_STOCK,
                    price=price,
                    error_message=f"Button state: {state}",
                )

        except (KeyError, TypeError, AttributeError) as e:
            logger.debug("Best Buy fulfillment parse error: %s", e)

        return StockResult(
            url=url,
            retailer=self.retailer_name,
            product_name=product_name,
            status=StockStatus.UNKNOWN,
            error_message="Could not parse fulfillment response",
        )

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
                price=price,
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

        # Check for Add to Cart button (old and new class names)
        add_btn = soup.find("button", class_=re.compile(r"add-to-cart", re.I))
        if not add_btn:
            # New PDP may use data attributes or different classes
            add_btn = soup.find("button", attrs={"data-button-state": "ADD_TO_CART"})
        if not add_btn:
            add_btn = soup.find("button", string=re.compile(r"add to cart", re.I))
        if add_btn:
            btn_text = add_btn.get_text(strip=True).lower()
            if "add to cart" in btn_text:
                return StockResult(
                    url=url,
                    retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.IN_STOCK,
                    price=self._extract_price(soup, html),
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

        # Check for Coming Soon
        coming_soon = soup.find("button", string=re.compile(r"coming soon", re.I))
        if coming_soon:
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.OUT_OF_STOCK,
                error_message="Coming soon",
            )

        # Check for invitation-only or reservation system (Best Buy's Pokemon/high-demand system)
        invite = soup.find(string=re.compile(r"(get your invite|invitation|reservation process)", re.I))
        if invite:
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.OUT_OF_STOCK,
                error_message="Invitation/reservation product",
            )

        # Check for "High Demand Product" notice (visible on PDP for Pokemon items)
        high_demand = soup.find(string=re.compile(r"high demand product", re.I))
        if high_demand:
            # High demand doesn't mean out of stock — it may still have "Add to cart"
            # from third-party sellers. Continue checking for add-to-cart below.
            logger.debug("Best Buy: high demand product detected for %s", url)

        # Check __NEXT_DATA__ for button state (new Next.js PDP)
        next_data_match = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if next_data_match:
            try:
                next_data = json.loads(next_data_match.group(1))
                button_state = self._find_in_dict(next_data, "buttonState")
                if button_state:
                    state = button_state if isinstance(button_state, str) else button_state.get("buttonState", "")
                    if state == "ADD_TO_CART":
                        return StockResult(
                            url=url,
                            retailer=self.retailer_name,
                            product_name=product_name,
                            status=StockStatus.IN_STOCK,
                            price=self._extract_price(soup, html),
                        )
                    elif state in ("SOLD_OUT", "UNAVAILABLE"):
                        return StockResult(
                            url=url,
                            retailer=self.retailer_name,
                            product_name=product_name,
                            status=StockStatus.OUT_OF_STOCK,
                        )
            except (json.JSONDecodeError, TypeError):
                pass

        # Check structured data (JSON-LD)
        for script in soup.find_all("script", type="application/ld+json"):
            text = script.string or ""
            if '"availability"' in text:
                if '"InStock"' in text or '"inStock"' in text.lower():
                    return StockResult(
                        url=url,
                        retailer=self.retailer_name,
                        product_name=product_name,
                        status=StockStatus.IN_STOCK,
                        price=self._extract_price(soup, html),
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

    def _find_in_dict(self, d, key: str, max_depth: int = 10):
        """Recursively search for a key in nested dicts/lists."""
        if max_depth <= 0:
            return None
        if isinstance(d, dict):
            if key in d:
                return d[key]
            for v in d.values():
                result = self._find_in_dict(v, key, max_depth - 1)
                if result is not None:
                    return result
        elif isinstance(d, list):
            for item in d:
                result = self._find_in_dict(item, key, max_depth - 1)
                if result is not None:
                    return result
        return None

    def _extract_price(self, soup: BeautifulSoup, html: str = "") -> str:
        # Old class name
        price_el = soup.find(class_=re.compile(r"priceView-customer-price", re.I))
        if price_el:
            span = price_el.find("span")
            if span:
                return span.get_text(strip=True)

        # New PDP: look for price in data attributes or aria labels
        price_el = soup.find(attrs={"data-testid": re.compile(r"customer-price", re.I)})
        if price_el:
            return price_el.get_text(strip=True)

        # Try extracting from __NEXT_DATA__ or inline JSON
        if html:
            match = re.search(r'"currentPrice"\s*:\s*(\d+\.?\d*)', html)
            if match:
                return f"${match.group(1)}"

        return ""

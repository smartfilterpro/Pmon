"""Target stock monitor."""

from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup

from pmon.models import StockResult, StockStatus
from .base import BaseMonitor

logger = logging.getLogger(__name__)


class TargetMonitor(BaseMonitor):
    retailer_name = "target"

    # Target's Redsky PDP API for stock checking
    # NOTE: pdp_fulfillment_v1 was deprecated (returns 410 Gone as of ~2026-03).
    # pdp_client_v1 is the current endpoint used by the Target web frontend.
    PDP_URL = "https://redsky.target.com/redsky_aggregations/v1/web/pdp_client_v1"

    # Multiple API keys to try (Target rotates these periodically)
    API_KEYS = [
        "9f36aeafbe60771e321a7cc95a78140772ab3e96",
        "ff457966e64d5e877fdbad070f276d18ecec4a01",
    ]

    def _extract_tcin(self, url: str) -> str | None:
        """Extract TCIN (Target product ID) from URL."""
        # Target URLs look like: /p/product-name/-/A-12345678
        match = re.search(r"A-(\d+)", url)
        return match.group(1) if match else None

    async def check_stock(self, url: str, product_name: str) -> StockResult:
        tcin = self._extract_tcin(url)
        if not tcin:
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.ERROR,
                error_message="Could not extract TCIN from URL",
            )

        client = await self.get_client()

        # Try the Redsky PDP API first (faster and more reliable)
        for api_key in self.API_KEYS:
            params = {
                "key": api_key,
                "tcin": tcin,
                "channel": "WEB",
            }

            try:
                resp = await client.get(
                    self.PDP_URL,
                    params=params,
                    headers={
                        "Referer": url,
                        "Origin": "https://www.target.com",
                        "Accept": "application/json",
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    result = self._parse_pdp(url, product_name, data)
                    if result.status != StockStatus.UNKNOWN:
                        return result
                elif resp.status_code == 410:
                    logger.debug(f"Redsky pdp_client_v1 returned 410 for {tcin} with key ...{api_key[-6:]}")
            except Exception as e:
                logger.debug(f"Redsky API failed for {tcin} with key ...{api_key[-6:]}: {e}")

        # Fallback: scrape the product page
        return await self._scrape_page(url, product_name, client)

    def _parse_pdp(self, url: str, product_name: str, data: dict) -> StockResult:
        """Parse pdp_client_v1 response for stock status."""
        try:
            product = data.get("data", {}).get("product", {})
            fulfillment = product.get("fulfillment", {})

            price_info = product.get("price", {})
            price = price_info.get("formatted_current_price", "")

            # pdp_client_v1 uses several availability indicators:
            # 1. shipping_options.availability_status (legacy, still present sometimes)
            shipping = fulfillment.get("shipping_options", {})
            ship_status = shipping.get("availability_status", "")
            if ship_status == "IN_STOCK":
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.IN_STOCK, price=price,
                )

            # 2. Check scheduled_delivery and shipping availability_status_v2
            for method_key in ("shipping_options", "scheduled_delivery"):
                method = fulfillment.get(method_key, {})
                v2 = method.get("availability_status_v2", [])
                if isinstance(v2, list):
                    for entry in v2:
                        if isinstance(entry, dict) and entry.get("is_available"):
                            return StockResult(
                                url=url, retailer=self.retailer_name,
                                product_name=product_name,
                                status=StockStatus.IN_STOCK, price=price,
                            )

            # 3. Check store_options / in_store_only pickup
            store_options = fulfillment.get("store_options", [])
            if isinstance(store_options, list):
                for opt in store_options:
                    if opt.get("order_pickup", {}).get("availability_status") == "IN_STOCK":
                        return StockResult(
                            url=url, retailer=self.retailer_name,
                            product_name=product_name,
                            status=StockStatus.IN_STOCK, price=price,
                        )

            # 4. Broad string search in fulfillment data as last resort
            fulfillment_str = json.dumps(fulfillment)
            if '"IN_STOCK"' in fulfillment_str or '"AVAILABLE"' in fulfillment_str:
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.IN_STOCK, price=price,
                )

            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.OUT_OF_STOCK, price=price,
            )
        except (KeyError, TypeError) as e:
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.UNKNOWN,
                error_message=f"Could not parse PDP data: {e}",
            )

    async def _scrape_page(self, url: str, product_name: str, client) -> StockResult:
        resp = await client.get(
            url,
            headers={
                "Referer": "https://www.target.com/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        resp.raise_for_status()
        html = resp.text

        # Strategy 1: Parse schema.org JSON-LD (Target includes this in initial HTML)
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                ld_data = json.loads(script.string or "")
                # Handle both single objects and arrays
                items = ld_data if isinstance(ld_data, list) else [ld_data]
                for item in items:
                    offers = item.get("offers", {})
                    # offers can be a single dict or a list
                    offer_list = offers if isinstance(offers, list) else [offers]
                    for offer in offer_list:
                        avail = offer.get("availability", "")
                        price = offer.get("price", "")
                        if price:
                            price = f"${price}" if not str(price).startswith("$") else str(price)

                        if "InStock" in avail:
                            return StockResult(
                                url=url,
                                retailer=self.retailer_name,
                                product_name=product_name,
                                status=StockStatus.IN_STOCK,
                                price=price,
                            )
                        elif "OutOfStock" in avail:
                            return StockResult(
                                url=url,
                                retailer=self.retailer_name,
                                product_name=product_name,
                                status=StockStatus.OUT_OF_STOCK,
                                price=price,
                            )
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue

        # Strategy 2: Look for availability_status in embedded JSON/JS data
        if re.search(r'"availability_status"\s*:\s*"IN_STOCK"', html):
            price = ""
            price_match = re.search(r'"formatted_current_price"\s*:\s*"([^"]+)"', html)
            if price_match:
                price = price_match.group(1)
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.IN_STOCK,
                price=price,
            )

        # Strategy 3: Check for preloaded query data (Target's __TGT_DATA__ / window.__PRELOADED_QUERIES__)
        preloaded_match = re.search(r'window\.__PRELOADED_QUERIES__\s*=\s*(\{.+?\});?\s*</script>', html, re.S)
        if preloaded_match:
            try:
                preloaded = json.loads(preloaded_match.group(1))
                # Walk the preloaded data looking for availability
                preloaded_str = json.dumps(preloaded)
                if '"IN_STOCK"' in preloaded_str:
                    price_match = re.search(r'"formatted_current_price"\s*:\s*"([^"]+)"', preloaded_str)
                    price = price_match.group(1) if price_match else ""
                    return StockResult(
                        url=url,
                        retailer=self.retailer_name,
                        product_name=product_name,
                        status=StockStatus.IN_STOCK,
                        price=price,
                    )
            except (json.JSONDecodeError, TypeError):
                pass

        # Strategy 4: Simple text-based checks
        if re.search(r"(out of stock|sold out|temporarily unavailable)", html, re.I):
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.OUT_OF_STOCK,
            )

        # Check for "Add to cart" button in HTML as last resort
        add_btn = soup.find("button", attrs={"data-test": re.compile(r"addToCart|shippingButton", re.I)})
        if not add_btn:
            add_btn = soup.find("button", string=re.compile(r"add to cart", re.I))
        if add_btn and not add_btn.get("disabled"):
            return StockResult(
                url=url,
                retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.IN_STOCK,
                price="",
            )

        return StockResult(
            url=url,
            retailer=self.retailer_name,
            product_name=product_name,
            status=StockStatus.UNKNOWN,
            error_message="Could not determine stock status from page",
        )

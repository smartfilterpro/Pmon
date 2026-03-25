"""Sam's Club stock monitor.

Sam's Club uses a combination of:
- Product API: samsclub.com/api/node/vivaldi/v2/products/{productId}
- Browse API: samsclub.com/api/soa/services/v1/catalog/product/{productId}
- __NEXT_DATA__ embedded JSON on product pages (Next.js SSR)

Membership-aware: Sam's Club is a members-only warehouse club. Some items
are available to Plus members only. The monitor checks general availability
and notes membership restrictions when detected.

Rate limiting: Sam's Club uses Akamai bot protection. Sessions cookies from
a real browser significantly reduce blocking. Without cookies, expect 403s
after a few requests. Minimum interval set to 5s (same as Costco/Walmart).
"""

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


class SamsClubMonitor(BaseMonitor):
    retailer_name = "samsclub"
    # Sam's Club aggressively rate-limits — enforce at least 5s between requests
    _min_request_interval: float = 5.0
    _cookie_domain: str = ".samsclub.com"

    def _extract_product_id(self, url: str) -> str | None:
        """Extract product ID from Sam's Club URL.

        Formats:
          /p/product-name/prod12345678
          /p/prod12345678
          /sams/shopping/details/prod12345678
          ?productId=prod12345678
        """
        # Match prod followed by digits (Sam's Club product ID format)
        match = re.search(r"(prod\d+)", url)
        if match:
            return match.group(1)
        # Also try plain numeric ID at end of path
        match = re.search(r"/(\d{8,})(?:\?|$|#)", url)
        if match:
            return match.group(1)
        return None

    def _extract_item_number(self, url: str) -> str | None:
        """Extract item number from Sam's Club URL.

        Formats:
          /p/product-name/123456
          ?itemNumber=123456
        """
        match = re.search(r"[?&]itemNumber=(\d+)", url)
        if match:
            return match.group(1)
        # Try numeric segment at end of URL path (6-8 digits, after product name)
        match = re.search(r"/p/[^/]+/(\d{4,8})(?:\?|$|#)", url)
        if match:
            return match.group(1)
        return None

    async def check_stock(self, url: str, product_name: str) -> StockResult:
        client = await self.get_client()

        product_id = self._extract_product_id(url)
        item_number = self._extract_item_number(url)

        # Try the product API first (JSON, less likely to be blocked)
        if product_id:
            result = await self._check_via_api(client, url, product_name, product_id)
            if result and result.status != StockStatus.UNKNOWN:
                return result

        # Try the browse/catalog API with item number
        if item_number:
            result = await self._check_via_catalog_api(client, url, product_name, item_number)
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
            hint = "" if self._session_cookies else " Import session cookies to reduce rate limiting."
            return StockResult(
                url=url, retailer=self.retailer_name, product_name=product_name,
                status=StockStatus.ERROR,
                error_message=f"Rate limited by Sam's Club (429). Cooling down for {self.rate_limit_remaining():.0f}s.{hint}",
            )

        if resp.status_code == 403:
            logger.warning("Sam's Club: 403 on product page — Akamai blocked")
            hint = " Re-import fresh cookies." if self._session_cookies else " Import session cookies to improve reliability."
            return StockResult(
                url=url, retailer=self.retailer_name, product_name=product_name,
                status=StockStatus.ERROR,
                error_message=f"Blocked by Sam's Club (403).{hint}",
            )

        resp.raise_for_status()
        self.record_success()

        html = resp.text

        # Check if we got a block/CAPTCHA page
        if "blocked" in resp.url.path.lower() or "press & hold" in html.lower() or "robot" in html.lower():
            logger.warning("Sam's Club: redirected to CAPTCHA/block page")
            return StockResult(
                url=url, retailer=self.retailer_name, product_name=product_name,
                status=StockStatus.ERROR,
                error_message="Sam's Club CAPTCHA block. Import session cookies via Settings > Session Cookies.",
            )

        soup = BeautifulSoup(html, "html.parser")

        # Sam's Club uses __NEXT_DATA__ for SSR (Next.js)
        next_data = soup.find("script", id="__NEXT_DATA__")
        if next_data and next_data.string:
            try:
                data = json.loads(next_data.string)
                result = self._parse_next_data(url, product_name, data)
                if result.status != StockStatus.UNKNOWN:
                    return result
            except json.JSONDecodeError:
                logger.debug("Could not parse Sam's Club __NEXT_DATA__")

        # Also try script tags with product JSON-LD
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                ld = json.loads(script.string)
                result = self._parse_json_ld(url, product_name, ld)
                if result and result.status != StockStatus.UNKNOWN:
                    return result
            except (json.JSONDecodeError, TypeError):
                continue

        # Final fallback: parse HTML for stock indicators
        return self._parse_html(url, product_name, soup)

    def _parse_next_data(self, url: str, product_name: str, data: dict) -> StockResult:
        """Parse Sam's Club __NEXT_DATA__ structure for stock info."""
        try:
            props = data.get("props", {}).get("pageProps", {})

            # Navigate to product data — Sam's Club nests it in various places
            product = (
                props.get("initialData", {}).get("data", {}).get("product", {})
                or props.get("product", {})
                or props.get("productData", {})
            )

            if not product:
                # Try deeper nesting
                content_data = props.get("contentData", {})
                if isinstance(content_data, dict):
                    product = content_data.get("product", {})

            if not product:
                return StockResult(
                    url=url, retailer=self.retailer_name, product_name=product_name,
                    status=StockStatus.UNKNOWN,
                    error_message="Could not find product in __NEXT_DATA__",
                )

            # Extract availability
            online_offer = product.get("onlineOffer", {}) or {}
            availability = (
                online_offer.get("availabilityStatus", "")
                or product.get("availabilityStatus", "")
                or product.get("status", "")
            ).upper()

            # Extract price
            price = ""
            price_info = online_offer.get("finalPrice", {}) or product.get("finalPrice", {})
            if isinstance(price_info, dict):
                amount = price_info.get("currencyAmount", "") or price_info.get("amount", "")
                if amount:
                    price = f"${amount}"
            if not price:
                list_price = product.get("listPrice", "") or online_offer.get("listPrice", "")
                if isinstance(list_price, dict):
                    amount = list_price.get("currencyAmount", "")
                    if amount:
                        price = f"${amount}"
                elif list_price:
                    price = str(list_price)

            # Extract image
            image_url = ""
            images = product.get("images", []) or product.get("imageGroups", [])
            if isinstance(images, list) and images:
                first = images[0]
                if isinstance(first, dict):
                    image_url = first.get("url", "") or first.get("thumbnailUrl", "")
                elif isinstance(first, str):
                    image_url = first

            # Check inventory / online availability
            in_stock_indicators = ("IN_STOCK", "AVAILABLE", "ONLINE")
            oos_indicators = ("OUT_OF_STOCK", "UNAVAILABLE", "NOT_AVAILABLE", "OOS")

            if any(ind in availability for ind in in_stock_indicators):
                return StockResult(
                    url=url, retailer=self.retailer_name, product_name=product_name,
                    status=StockStatus.IN_STOCK,
                    price=price, image_url=image_url,
                )
            elif any(ind in availability for ind in oos_indicators):
                return StockResult(
                    url=url, retailer=self.retailer_name, product_name=product_name,
                    status=StockStatus.OUT_OF_STOCK,
                    price=price, image_url=image_url,
                )

            # Check boolean flags as fallback
            is_available = (
                online_offer.get("available", None)
                or product.get("isAvailableOnline", None)
                or product.get("buyable", None)
            )
            if is_available is True:
                return StockResult(
                    url=url, retailer=self.retailer_name, product_name=product_name,
                    status=StockStatus.IN_STOCK,
                    price=price, image_url=image_url,
                )
            elif is_available is False:
                return StockResult(
                    url=url, retailer=self.retailer_name, product_name=product_name,
                    status=StockStatus.OUT_OF_STOCK,
                    price=price, image_url=image_url,
                )

        except (KeyError, TypeError, AttributeError):
            pass

        return StockResult(
            url=url, retailer=self.retailer_name, product_name=product_name,
            status=StockStatus.UNKNOWN,
            error_message="Could not parse Sam's Club product data",
        )

    def _parse_json_ld(self, url: str, product_name: str, ld_data) -> StockResult | None:
        """Parse JSON-LD structured data for product availability."""
        if isinstance(ld_data, list):
            for item in ld_data:
                result = self._parse_json_ld(url, product_name, item)
                if result:
                    return result
            return None

        if not isinstance(ld_data, dict):
            return None

        ld_type = ld_data.get("@type", "")
        if ld_type != "Product":
            return None

        offers = ld_data.get("offers", {})
        if isinstance(offers, list) and offers:
            offers = offers[0]

        if not isinstance(offers, dict):
            return None

        availability = offers.get("availability", "")
        price = offers.get("price", "")
        if price:
            price = f"${price}"

        image_url = ld_data.get("image", "")
        if isinstance(image_url, list) and image_url:
            image_url = image_url[0]

        if "InStock" in availability:
            return StockResult(
                url=url, retailer=self.retailer_name, product_name=product_name,
                status=StockStatus.IN_STOCK,
                price=price, image_url=image_url if isinstance(image_url, str) else "",
            )
        elif "OutOfStock" in availability:
            return StockResult(
                url=url, retailer=self.retailer_name, product_name=product_name,
                status=StockStatus.OUT_OF_STOCK,
                price=price, image_url=image_url if isinstance(image_url, str) else "",
            )

        return None

    def _parse_html(self, url: str, product_name: str, soup: BeautifulSoup) -> StockResult:
        """Fallback: parse HTML for stock status indicators."""
        # Check for Add to Cart button
        add_btn = soup.find("button", attrs={"data-automation": re.compile(r"add.to.cart", re.I)})
        if not add_btn:
            add_btn = soup.find("button", string=re.compile(r"add to cart", re.I))
        if not add_btn:
            add_btn = soup.find("button", attrs={"id": re.compile(r"addToCart", re.I)})

        if add_btn and not add_btn.get("disabled"):
            return StockResult(
                url=url, retailer=self.retailer_name, product_name=product_name,
                status=StockStatus.IN_STOCK,
                price=self._extract_price(soup),
            )

        # Check for out of stock indicators
        oos = soup.find(string=re.compile(
            r"(out of stock|currently unavailable|not available|sold out|notify me)", re.I
        ))
        if oos:
            return StockResult(
                url=url, retailer=self.retailer_name, product_name=product_name,
                status=StockStatus.OUT_OF_STOCK,
                price=self._extract_price(soup),
            )

        # Check for membership-only indicators
        members_only = soup.find(string=re.compile(r"(plus member|members only)", re.I))
        if members_only:
            return StockResult(
                url=url, retailer=self.retailer_name, product_name=product_name,
                status=StockStatus.IN_STOCK,
                price=self._extract_price(soup),
            )

        return StockResult(
            url=url, retailer=self.retailer_name, product_name=product_name,
            status=StockStatus.UNKNOWN,
            error_message="Could not determine stock status from HTML",
        )

    async def _check_via_api(
        self, client, url: str, product_name: str, product_id: str
    ) -> StockResult | None:
        """Try Sam's Club product API (less blocked than full page scrape).

        Sam's Club exposes product data via internal API endpoints that
        the frontend calls during client-side rendering.
        """
        try:
            resp = await client.get(
                f"https://www.samsclub.com/api/node/vivaldi/v2/products/{product_id}",
                params={
                    "response_group": "LARGE",
                    "clubId": "0",  # 0 = online/ship-to-home
                },
                headers={
                    **API_HEADERS,
                    "Referer": url,
                    "Origin": "https://www.samsclub.com",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-origin",
                    "x-sams-channel": "web",
                },
            )

            if resp.status_code == 429:
                retry_after = _parse_retry_after(resp)
                self.record_rate_limit(retry_after)
                hint = "" if self._session_cookies else " Import session cookies to reduce rate limiting."
                return StockResult(
                    url=url, retailer=self.retailer_name, product_name=product_name,
                    status=StockStatus.ERROR,
                    error_message=f"Rate limited by Sam's Club (429). Cooling down for {self.rate_limit_remaining():.0f}s.{hint}",
                )

            if resp.status_code != 200:
                logger.debug("Sam's Club API returned %d for product %s", resp.status_code, product_id)
                return None

            self.record_success()
            data = resp.json()

            # Navigate response structure
            payload = data.get("payload", data)
            products = payload.get("products", [payload]) if isinstance(payload, dict) else [payload]

            for product in products:
                if not isinstance(product, dict):
                    continue

                online_offer = product.get("onlineOffer", {}) or {}
                availability = (
                    online_offer.get("availabilityStatus", "")
                    or product.get("status", "")
                ).upper()

                # Price extraction
                price = ""
                final_price = online_offer.get("finalPrice", {})
                if isinstance(final_price, dict):
                    amount = final_price.get("currencyAmount", "")
                    if amount:
                        price = f"${amount}"
                if not price:
                    list_price = online_offer.get("listPrice", {})
                    if isinstance(list_price, dict):
                        amount = list_price.get("currencyAmount", "")
                        if amount:
                            price = f"${amount}"

                # Image
                image_url = ""
                images = product.get("images", [])
                if isinstance(images, list) and images:
                    first = images[0]
                    if isinstance(first, dict):
                        image_url = first.get("url", "")

                if "IN_STOCK" in availability or "AVAILABLE" in availability:
                    return StockResult(
                        url=url, retailer=self.retailer_name,
                        product_name=product_name,
                        status=StockStatus.IN_STOCK, price=price,
                        image_url=image_url,
                    )
                elif "OUT_OF_STOCK" in availability or "UNAVAILABLE" in availability:
                    return StockResult(
                        url=url, retailer=self.retailer_name,
                        product_name=product_name,
                        status=StockStatus.OUT_OF_STOCK, price=price,
                        image_url=image_url,
                    )

                # Boolean fallback
                is_available = online_offer.get("available")
                if is_available is True:
                    return StockResult(
                        url=url, retailer=self.retailer_name,
                        product_name=product_name,
                        status=StockStatus.IN_STOCK, price=price,
                        image_url=image_url,
                    )
                elif is_available is False:
                    return StockResult(
                        url=url, retailer=self.retailer_name,
                        product_name=product_name,
                        status=StockStatus.OUT_OF_STOCK, price=price,
                        image_url=image_url,
                    )

        except Exception as e:
            logger.debug("Sam's Club API check failed for %s: %s", product_id, e)

        return None

    async def _check_via_catalog_api(
        self, client, url: str, product_name: str, item_number: str
    ) -> StockResult | None:
        """Try Sam's Club catalog/browse API using item number."""
        try:
            resp = await client.get(
                f"https://www.samsclub.com/api/soa/services/v1/catalog/product/{item_number}",
                params={
                    "response_group": "LARGE",
                    "clubId": "0",
                },
                headers={
                    **API_HEADERS,
                    "Referer": url,
                    "Origin": "https://www.samsclub.com",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-origin",
                },
            )

            if resp.status_code == 429:
                retry_after = _parse_retry_after(resp)
                self.record_rate_limit(retry_after)
                return StockResult(
                    url=url, retailer=self.retailer_name, product_name=product_name,
                    status=StockStatus.ERROR,
                    error_message=f"Rate limited by Sam's Club (429).",
                )

            if resp.status_code != 200:
                logger.debug("Sam's Club catalog API returned %d for item %s", resp.status_code, item_number)
                return None

            self.record_success()
            data = resp.json()

            # Catalog API response structure
            payload = data.get("payload", data)
            if not isinstance(payload, dict):
                return None

            status = payload.get("status", "").upper()
            online = payload.get("onlineInventory", {}) or {}
            online_status = online.get("status", "").upper()

            price = ""
            pricing = payload.get("pricing", {}) or {}
            if isinstance(pricing, dict):
                amount = pricing.get("finalPrice", "") or pricing.get("listPrice", "")
                if amount:
                    price = f"${amount}" if not str(amount).startswith("$") else str(amount)

            if "IN_STOCK" in online_status or "AVAILABLE" in online_status:
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.IN_STOCK, price=price,
                )
            elif "OUT_OF_STOCK" in online_status or "UNAVAILABLE" in online_status:
                return StockResult(
                    url=url, retailer=self.retailer_name,
                    product_name=product_name,
                    status=StockStatus.OUT_OF_STOCK, price=price,
                )

        except Exception as e:
            logger.debug("Sam's Club catalog API check failed for %s: %s", item_number, e)

        return None

    def _extract_price(self, soup: BeautifulSoup) -> str:
        """Extract price from HTML."""
        # Try structured data first
        price_el = soup.find(attrs={"itemprop": "price"})
        if price_el:
            return price_el.get("content", "") or price_el.get_text(strip=True)

        # Try common Sam's Club price selectors
        for selector in [
            "[data-automation='price']",
            ".sc-price",
            ".Price-group",
            "[class*='price']",
        ]:
            el = soup.select_one(selector)
            if el:
                text = el.get_text(strip=True)
                if "$" in text:
                    return text

        return ""

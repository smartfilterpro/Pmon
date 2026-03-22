"""Target stock monitor.

Uses the Redsky API (same as Target's web frontend) to check stock status.
Two endpoints are tried:
1. product_fulfillment_and_variation_hierarchy_v1 — returns location-aware
   fulfillment data (shipping, pickup, delivery availability)
2. pdp_client_v1 — returns full product data including price and fulfillment

Both endpoints require matching the exact query parameters that Target's
real frontend sends, including is_bot=false, store_id, and location data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid

from bs4 import BeautifulSoup

from pmon.models import StockResult, StockStatus
from .base import API_HEADERS, BaseMonitor

logger = logging.getLogger(__name__)


class TargetMonitor(BaseMonitor):
    retailer_name = "target"

    # Target's Redsky API endpoints (same base, different aggregation paths)
    REDSKY_BASE = "https://redsky.target.com/redsky_aggregations/v1/web"
    # Current endpoint (as of 2026-03) — Target renamed from pdp_client_v1
    FULFILLMENT_URL = f"{REDSKY_BASE}/product_fulfillment_v1"
    # Fallback: older endpoint names that may still work on some products
    FULFILLMENT_URL_LEGACY = f"{REDSKY_BASE}/product_fulfillment_and_variation_hierarchy_v1"
    PDP_URL = f"{REDSKY_BASE}/pdp_client_v1"

    # API keys observed from Target's real frontend (2026-03-17)
    API_KEYS = [
        "9f36aeafbe60771e321a7cc95a78140772ab3e96",
        "e59ce3b531b2c39afb2e2b8a71ff10113aac2a14",
        "ff457966e64d5e877fdbad070f276d18ecec4a01",
    ]

    # Default store / location — used when user hasn't configured their own.
    # These values come from the network capture (store 2845, Baltimore MD).
    DEFAULT_STORE_ID = "2845"
    DEFAULT_ZIP = "21224"
    DEFAULT_STATE = "MD"
    DEFAULT_LAT = "39.282024"
    DEFAULT_LNG = "-76.569695"

    def __init__(self):
        super().__init__()
        self._visitor_id: str = uuid.uuid4().hex
        self._warmed_up: bool = False
        self._refreshed_keys: list[str] | None = None  # keys discovered at runtime
        self._key_refresh_attempted: float = 0  # timestamp of last browser key refresh
        self._KEY_REFRESH_COOLDOWN = 3600  # don't retry browser refresh more than once per hour

    def _extract_tcin(self, url: str) -> str | None:
        """Extract TCIN (Target product ID) from URL."""
        match = re.search(r"A-(\d+)", url)
        return match.group(1) if match else None

    @property
    def _active_keys(self) -> list[str]:
        """Return refreshed keys if available, otherwise hardcoded defaults."""
        return self._refreshed_keys if self._refreshed_keys else self.API_KEYS

    async def _warm_up(self, client):
        """Visit Target homepage to establish PerimeterX session cookies."""
        if self._warmed_up:
            return
        try:
            resp = await client.get(
                "https://www.target.com/",
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
                logger.debug("Target: warm-up visit OK, cookies established")
                # Try to extract API keys from inline HTML/JS
                self._extract_api_keys_from_html(resp.text)
        except Exception as e:
            logger.debug("Target: warm-up visit failed: %s", e)

    def _extract_api_keys_from_html(self, html: str):
        """Extract Redsky API keys from Target page HTML (best-effort)."""
        url_keys = re.findall(
            r'redsky\.target\.com/[^"\']*[?&]key=([a-f0-9]{30,50})', html
        )
        js_keys = re.findall(
            r'["\']?apiKey["\']?\s*[:=]\s*["\']([a-f0-9]{30,50})["\']', html
        )
        all_keys = list(dict.fromkeys(url_keys + js_keys))
        if all_keys:
            logger.info("Target: extracted %d API key(s) from HTML", len(all_keys))
            self._refreshed_keys = all_keys

    async def _refresh_api_keys_via_browser(self):
        """Use Playwright to visit Target and intercept Redsky API keys.

        Opens a real browser (with stealth), navigates to a product page,
        waits for the frontend to make Redsky API calls, and captures the
        key= parameter from the request URLs.

        Respects a cooldown to avoid hammering Target with browser sessions.
        """
        now = time.monotonic()
        if now - self._key_refresh_attempted < self._KEY_REFRESH_COOLDOWN:
            logger.debug("Target: key refresh on cooldown, skipping")
            return
        self._key_refresh_attempted = now

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.debug("Target: playwright not installed — cannot refresh API keys via browser")
            return

        logger.info("Target: refreshing API keys via browser (intercepting Redsky requests)...")
        captured_keys: list[str] = []

        try:
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
            )
            page = await context.new_page()

            # Remove webdriver flag
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)

            # Intercept requests to redsky.target.com and capture API keys
            def on_request(request):
                url = request.url
                if "redsky.target.com" in url:
                    match = re.search(r'[?&]key=([a-f0-9]{30,50})', url)
                    if match and match.group(1) not in captured_keys:
                        captured_keys.append(match.group(1))

            page.on("request", on_request)

            # Visit a known product page to trigger Redsky calls
            await page.goto(
                "https://www.target.com/p/-/A-89315228",
                wait_until="domcontentloaded",
                timeout=30000,
            )

            # Wait for Redsky requests to fire (they happen after DOM load)
            for _ in range(10):
                if captured_keys:
                    break
                await asyncio.sleep(1)

            await browser.close()
            await pw.stop()

            if captured_keys:
                logger.info("Target: captured %d fresh API key(s) via browser: ...%s",
                            len(captured_keys), captured_keys[0][-8:])
                self._refreshed_keys = captured_keys
            else:
                logger.warning("Target: browser key refresh found no Redsky requests")

        except Exception as exc:
            logger.warning("Target: browser key refresh failed: %s", exc)

    def _redsky_headers(self, url: str) -> dict:
        """Build headers matching Target's real frontend Redsky requests."""
        return {
            **API_HEADERS,
            "Referer": url,
            "Origin": "https://www.target.com",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "x-application-name": "web",
        }

    async def check_stock(self, url: str, product_name: str) -> StockResult:
        tcin = self._extract_tcin(url)
        if not tcin:
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name, status=StockStatus.ERROR,
                error_message="Could not extract TCIN from URL",
            )

        client = await self.get_client()
        await self._warm_up(client)

        store_id = self.DEFAULT_STORE_ID

        # --- Strategy 1: product_fulfillment_v1 (current endpoint) ---
        # Target renamed their fulfillment endpoint ~2026-03. This is the
        # primary endpoint that the frontend now calls for fulfillment data.
        fulfillment_result: StockResult | None = None
        for api_key in self._active_keys:
            fulfillment_params = {
                "key": api_key,
                "tcin": tcin,
                "is_bot": "false",
                "store_id": store_id,
                "store_positions_store_id": store_id,
                "pricing_store_id": store_id,
                "has_pricing_store_id": "true",
                "has_store_positions_store_id": "true",
                "latitude": self.DEFAULT_LAT,
                "longitude": self.DEFAULT_LNG,
                "state": self.DEFAULT_STATE,
                "zip": self.DEFAULT_ZIP,
                "visitor_id": self._visitor_id,
                "channel": "WEB",
                "page": f"/p/A-{tcin}",
            }
            try:
                resp = await client.get(
                    self.FULFILLMENT_URL,
                    params=fulfillment_params,
                    headers=self._redsky_headers(url),
                )
                if resp.status_code == 200:
                    data = resp.json()
                    result = self._parse_fulfillment(url, product_name, data)
                    if result.status != StockStatus.UNKNOWN:
                        logger.debug(
                            "Target stock for %s: %s (via product_fulfillment_v1)",
                            product_name, result.status.value,
                        )
                        if result.price:
                            return result
                        fulfillment_result = result
                    else:
                        logger.debug("Target: product_fulfillment_v1 returned UNKNOWN for %s", tcin)
                    break
                elif resp.status_code in (404, 410):
                    # 404 = endpoint not found, 410 = gone/deprecated key
                    # Either way, try legacy endpoint as fallback
                    logger.debug("Target: product_fulfillment_v1 returned %d, trying legacy endpoint for %s", resp.status_code, tcin)
                    legacy_resp = await client.get(
                        self.FULFILLMENT_URL_LEGACY,
                        params={**fulfillment_params,
                                "required_store_id": store_id,
                                "scheduled_delivery_store_id": store_id,
                                "paid_membership": "false",
                                "base_membership": "true",
                                "card_membership": "false"},
                        headers=self._redsky_headers(url),
                    )
                    if legacy_resp.status_code == 200:
                        data = legacy_resp.json()
                        result = self._parse_fulfillment(url, product_name, data)
                        if result.status != StockStatus.UNKNOWN:
                            logger.debug("Target stock for %s: %s (via legacy fulfillment)", product_name, result.status.value)
                            if result.price:
                                return result
                            fulfillment_result = result
                    break
                elif resp.status_code == 403:
                    logger.warning("Target: fulfillment API 403 for %s — rotating visitor_id", tcin)
                    self._visitor_id = uuid.uuid4().hex
                    self._warmed_up = False
                else:
                    logger.debug("Target: fulfillment API returned %d for %s", resp.status_code, tcin)
                    break
            except Exception as e:
                logger.debug("Target: fulfillment API failed for %s: %s", tcin, e)
                break

        # --- Strategy 2: pdp_client_v1 (full product data, may be deprecated) ---
        api_attempted = False
        api_all_blocked = True
        for api_key in self._active_keys:
            pdp_params = {
                "key": api_key,
                "tcin": tcin,
                "is_bot": "false",
                "store_id": store_id,
                "pricing_store_id": store_id,
                "has_pricing_store_id": "true",
                "has_financing_options": "true",
                "include_obsolete": "true",
                "skip_personalized": "true",
                "skip_variation_hierarchy": "true",
                "visitor_id": self._visitor_id,
                "channel": "WEB",
                "page": f"/p/A-{tcin}",
            }

            try:
                api_attempted = True
                resp = await client.get(
                    self.PDP_URL,
                    params=pdp_params,
                    headers=self._redsky_headers(url),
                )
                if resp.status_code == 200:
                    api_all_blocked = False
                    data = resp.json()
                    result = self._parse_pdp(url, product_name, data)
                    if result.status != StockStatus.UNKNOWN:
                        logger.debug("Target stock for %s: %s (via pdp_client_v1)", product_name, result.status.value)
                        # If we had a fulfillment result with status but no price,
                        # use the fulfillment status with the PDP price
                        if fulfillment_result and not fulfillment_result.price and result.price:
                            fulfillment_result.price = result.price
                            return fulfillment_result
                        return result
                    elif fulfillment_result and result.price:
                        # PDP couldn't determine status but got price — merge into fulfillment result
                        fulfillment_result.price = result.price
                        return fulfillment_result
                    else:
                        logger.debug("Target: pdp_client_v1 returned 200 but parse returned UNKNOWN for %s", tcin)
                elif resp.status_code == 403:
                    logger.warning("Target: pdp_client_v1 403 for %s with key ...%s", tcin, api_key[-6:])
                    self._visitor_id = uuid.uuid4().hex
                    self._warmed_up = False
                elif resp.status_code == 410:
                    api_all_blocked = False
                    logger.debug("Target: pdp_client_v1 returned 410 for %s", tcin)
                else:
                    api_all_blocked = False
                    logger.debug("Target: pdp_client_v1 returned %d for %s", resp.status_code, tcin)
            except Exception as e:
                logger.debug("Target: pdp_client_v1 failed for %s: %s", tcin, e)

        if api_attempted and api_all_blocked:
            logger.warning("Target: ALL API keys blocked for %s — falling back to scrape", tcin)

        # If fulfillment got a definitive status but PDP couldn't add price, return it anyway
        if fulfillment_result:
            return fulfillment_result

        # --- Strategy 3: Browser key refresh ---
        # All API calls failed. If we haven't recently tried, use Playwright
        # to visit Target and intercept fresh API keys from network requests.
        if not self._refreshed_keys:
            await self._refresh_api_keys_via_browser()
            if self._refreshed_keys:
                # Got fresh keys — retry the primary endpoint once
                api_key = self._refreshed_keys[0]
                try:
                    resp = await client.get(
                        self.FULFILLMENT_URL,
                        params={
                            "key": api_key,
                            "tcin": tcin,
                            "is_bot": "false",
                            "store_id": store_id,
                            "store_positions_store_id": store_id,
                            "pricing_store_id": store_id,
                            "has_pricing_store_id": "true",
                            "has_store_positions_store_id": "true",
                            "latitude": self.DEFAULT_LAT,
                            "longitude": self.DEFAULT_LNG,
                            "state": self.DEFAULT_STATE,
                            "zip": self.DEFAULT_ZIP,
                            "visitor_id": self._visitor_id,
                            "channel": "WEB",
                            "page": f"/p/A-{tcin}",
                        },
                        headers=self._redsky_headers(url),
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        result = self._parse_fulfillment(url, product_name, data)
                        if result.status != StockStatus.UNKNOWN:
                            logger.info("Target stock for %s: %s (via refreshed key)", product_name, result.status.value)
                            return result
                except Exception as e:
                    logger.debug("Target: retry with refreshed key failed: %s", e)

        # --- Strategy 4: HTML scrape ---
        logger.debug("Target stock for %s: falling back to page scrape", product_name)
        return await self._scrape_page(url, product_name, client)

    def _parse_fulfillment(self, url: str, product_name: str, data: dict) -> StockResult:
        """Parse product_fulfillment_and_variation_hierarchy_v1 response."""
        try:
            product = data.get("data", {}).get("product", {})
            fulfillment = product.get("fulfillment", {})
            price = self._extract_price_from_product(product)

            logger.debug("Target fulfillment response keys: %s", list(fulfillment.keys()) if isinstance(fulfillment, dict) else "N/A")

            # This endpoint returns detailed fulfillment with availability per method
            return self._check_fulfillment_availability(url, product_name, product, fulfillment, price)
        except (KeyError, TypeError) as e:
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name, status=StockStatus.UNKNOWN,
                error_message=f"Could not parse fulfillment data: {e}",
            )

    @staticmethod
    def _extract_price_from_product(product: dict) -> str:
        """Extract price from a Target product dict, trying multiple paths."""
        price_info = product.get("price", {})
        if isinstance(price_info, dict):
            # formatted_current_price is the most common field
            price = price_info.get("formatted_current_price", "")
            if price:
                return price
            # Fallback: current_retail / current_retail_min
            for key in ("current_retail", "current_retail_min"):
                val = price_info.get(key)
                if val is not None:
                    return f"${val}" if not str(val).startswith("$") else str(val)
        # Search entire product JSON for formatted_current_price as last resort
        product_str = json.dumps(product)
        match = re.search(r'"formatted_current_price"\s*:\s*"([^"]+)"', product_str)
        if match:
            return match.group(1)
        return ""

    def _parse_pdp(self, url: str, product_name: str, data: dict) -> StockResult:
        """Parse pdp_client_v1 response for stock status."""
        try:
            product = data.get("data", {}).get("product", {})
            fulfillment = product.get("fulfillment", {})
            price = self._extract_price_from_product(product)

            logger.debug("Target pdp_client_v1 fulfillment data: %s", json.dumps(fulfillment, indent=2)[:2000])

            return self._check_fulfillment_availability(url, product_name, product, fulfillment, price)
        except (KeyError, TypeError) as e:
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name, status=StockStatus.UNKNOWN,
                error_message=f"Could not parse PDP data: {e}",
            )

    def _check_fulfillment_availability(
        self, url: str, product_name: str, product: dict, fulfillment: dict, price: str,
    ) -> StockResult:
        """Shared logic to check availability from fulfillment data.

        Checks multiple fields and structures that Target uses to indicate
        stock status, in order of reliability.
        """
        def _in_stock(reason: str = "") -> StockResult:
            if reason:
                logger.debug("Target stock: %s → IN_STOCK", reason)
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.IN_STOCK, price=price,
            )

        # 1. is_out_of_stock_in_all_store_locations — explicit flag
        if fulfillment.get("is_out_of_stock_in_all_store_locations") is False:
            return _in_stock("is_out_of_stock_in_all_store_locations=false")

        # 2. shipping_options.availability_status
        shipping = fulfillment.get("shipping_options", {})
        if shipping.get("availability_status") == "IN_STOCK":
            return _in_stock("shipping_options.availability_status=IN_STOCK")

        # 3. availability_status_v2 across all fulfillment methods
        for method_key in ("shipping_options", "scheduled_delivery"):
            method = fulfillment.get(method_key, {})
            v2 = method.get("availability_status_v2", [])
            if isinstance(v2, list):
                for entry in v2:
                    if isinstance(entry, dict) and entry.get("is_available"):
                        return _in_stock(f"{method_key}.availability_status_v2.is_available=true")

        # 4. store_options — pickup availability
        store_options = fulfillment.get("store_options", [])
        if isinstance(store_options, list):
            for opt in store_options:
                pickup = opt.get("order_pickup", {})
                if pickup.get("availability_status") == "IN_STOCK":
                    return _in_stock("store_options.order_pickup=IN_STOCK")
                # Also check ship_to_store, in_store_only
                for sub_key in ("ship_to_store", "in_store_only"):
                    sub = opt.get(sub_key, {})
                    if sub.get("availability_status") == "IN_STOCK":
                        return _in_stock(f"store_options.{sub_key}=IN_STOCK")

        # NOTE: Previously checks 5-8 used catalog-level availability_status,
        # shipping reason_code (SHIP_ELIGIBLE), and broad JSON string searches.
        # These caused false IN_STOCK results because:
        #   - product.availability.availability_status is catalog-level, not inventory
        #   - SHIP_ELIGIBLE means the product type is shippable, not in stock
        #   - Broad string searches matched unrelated fields
        # Only the fulfillment-specific checks above (1-4) are reliable.

        # Nothing found → OUT_OF_STOCK
        logger.debug(
            "Target stock: no availability signals found for %s — returning OUT_OF_STOCK. "
            "Fulfillment keys: %s",
            product_name,
            list(fulfillment.keys()) if isinstance(fulfillment, dict) else "N/A",
        )
        return StockResult(
            url=url, retailer=self.retailer_name,
            product_name=product_name,
            status=StockStatus.OUT_OF_STOCK, price=price,
        )

    async def _scrape_page(self, url: str, product_name: str, client) -> StockResult:
        resp = await client.get(
            url,
            headers={
                "Referer": "https://www.target.com/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
            },
        )
        if resp.status_code == 403:
            logger.warning("Target: 403 on page scrape — PerimeterX blocked, rotating session")
            self._visitor_id = uuid.uuid4().hex
            self._warmed_up = False
            return StockResult(
                url=url, retailer=self.retailer_name, product_name=product_name,
                status=StockStatus.ERROR,
                error_message="Blocked by PerimeterX (403) — will retry with new session",
            )
        resp.raise_for_status()
        html = resp.text

        # Strategy 1: Parse schema.org JSON-LD
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                ld_data = json.loads(script.string or "")
                items = ld_data if isinstance(ld_data, list) else [ld_data]
                for item in items:
                    offers = item.get("offers", {})
                    offer_list = offers if isinstance(offers, list) else [offers]
                    for offer in offer_list:
                        avail = offer.get("availability", "")
                        price = offer.get("price", "")
                        if price:
                            price = f"${price}" if not str(price).startswith("$") else str(price)

                        if "InStock" in avail:
                            return StockResult(
                                url=url, retailer=self.retailer_name,
                                product_name=product_name,
                                status=StockStatus.IN_STOCK, price=price,
                            )
                        elif "OutOfStock" in avail:
                            return StockResult(
                                url=url, retailer=self.retailer_name,
                                product_name=product_name,
                                status=StockStatus.OUT_OF_STOCK, price=price,
                            )
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue

        # Strategy 2: Regex for availability_status in embedded JS data
        if re.search(r'"availability_status"\s*:\s*"IN_STOCK"', html):
            price = ""
            price_match = re.search(r'"formatted_current_price"\s*:\s*"([^"]+)"', html)
            if price_match:
                price = price_match.group(1)
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.IN_STOCK, price=price,
            )

        # Strategy 3: __PRELOADED_QUERIES__ data
        preloaded_match = re.search(r'window\.__PRELOADED_QUERIES__\s*=\s*(\{.+?\});?\s*</script>', html, re.S)
        if preloaded_match:
            try:
                preloaded = json.loads(preloaded_match.group(1))
                preloaded_str = json.dumps(preloaded)
                if '"IN_STOCK"' in preloaded_str:
                    price_match = re.search(r'"formatted_current_price"\s*:\s*"([^"]+)"', preloaded_str)
                    price = price_match.group(1) if price_match else ""
                    return StockResult(
                        url=url, retailer=self.retailer_name,
                        product_name=product_name,
                        status=StockStatus.IN_STOCK, price=price,
                    )
            except (json.JSONDecodeError, TypeError):
                pass

        # Try to get price from embedded data for remaining strategies
        page_price = ""
        price_match = re.search(r'"formatted_current_price"\s*:\s*"([^"]+)"', html)
        if price_match:
            page_price = price_match.group(1)

        # Strategy 4: Text-based out-of-stock detection
        if re.search(r"(out of stock|sold out|temporarily unavailable)", html, re.I):
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.OUT_OF_STOCK, price=page_price,
            )

        # Strategy 5: "Add to cart" button presence
        add_btn = soup.find("button", attrs={"data-test": re.compile(r"addToCart|shippingButton", re.I)})
        if not add_btn:
            add_btn = soup.find("button", string=re.compile(r"add to cart", re.I))
        if add_btn and not add_btn.get("disabled"):
            return StockResult(
                url=url, retailer=self.retailer_name,
                product_name=product_name,
                status=StockStatus.IN_STOCK, price=page_price,
            )

        return StockResult(
            url=url, retailer=self.retailer_name,
            product_name=product_name,
            status=StockStatus.UNKNOWN,
            error_message="Could not determine stock status from page",
        )

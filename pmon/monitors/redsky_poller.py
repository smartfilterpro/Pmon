"""Dedicated RedSky API poller and keyword search for Target products.

RedSkyPoller — tight polling loop for a single TCIN:

    poller = RedSkyPoller(tcin="12345678", interval_ms=5000)
    poller.on("available", my_async_handler)
    await poller.start()

RedSkySearch — keyword → TCIN discovery + optional auto-poll:

    search = RedSkySearch(store_id="2845")
    results = await search.find("PS5 console")        # list of SearchResult
    pollers = await search.find_and_poll("PS5 console", on_available=handler)

Design notes:
- Uses httpx (consistent with the rest of Pmon — NOT axios/Node).
- Reuses API_HEADERS and rate-limit patterns from base.py.
- Exponential backoff on 429 / network errors (caps at 5 min).
- Logs every state transition with timestamps.
- stop() cleanly cancels the polling loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from pmon.monitors.base import API_HEADERS

logger = logging.getLogger(__name__)


@dataclass
class RedSkyProductData:
    """Snapshot of product data at the moment availability was detected."""

    tcin: str
    title: str = ""
    price: str = ""
    availability_status: str = ""
    is_purchasable: bool = False
    fulfillment: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# Type alias for event handlers: async callables that receive RedSkyProductData.
_Handler = Callable[[RedSkyProductData], Coroutine[Any, Any, Any]]


class RedSkyPoller:
    """Polls Target's RedSky pdp_client_v1 endpoint for a single TCIN.

    Parameters
    ----------
    tcin : str
        Target product ID (the numeric part after ``A-`` in URLs).
    interval_ms : int
        Polling interval in milliseconds (default 5 000 = 5 s).
    store_id : str
        Target store ID for location-aware fulfillment data.
    api_key : str | None
        Override the default RedSky API key.
    """

    REDSKY_FULFILLMENT = "https://redsky.target.com/redsky_aggregations/v1/web/product_fulfillment_v1"
    REDSKY_PDP_LEGACY = "https://redsky.target.com/redsky_aggregations/v1/web/pdp_client_v1"

    # Observed from Target's real frontend — rotated on 403.
    _API_KEYS = [
        "9f36aeafbe60771e321a7cc95a78140772ab3e96",
        "e59ce3b531b2c39afb2e2b8a71ff10113aac2a14",
        "ff457966e64d5e877fdbad070f276d18ecec4a01",
    ]

    def __init__(
        self,
        tcin: str,
        interval_ms: int = 5_000,
        store_id: str = "2845",
        api_key: str | None = None,
    ) -> None:
        self.tcin = tcin
        self.interval_s = interval_ms / 1000.0
        self.store_id = store_id

        self._api_keys = [api_key] if api_key else list(self._API_KEYS)
        self._key_index = 0
        self._visitor_id = uuid.uuid4().hex

        # Event handlers: event_name → list of async callables.
        self._handlers: dict[str, list[_Handler]] = {}

        # Polling state
        self._task: asyncio.Task | None = None
        self._running = False
        self._client: httpx.AsyncClient | None = None

        # Backoff state
        self._consecutive_errors = 0
        self._backoff_until = 0.0  # monotonic timestamp

        # Track the last known status so we only log/emit on *transitions*.
        self._last_status: str | None = None

    # ------------------------------------------------------------------
    # EventEmitter-style API
    # ------------------------------------------------------------------

    def on(self, event: str, handler: _Handler) -> RedSkyPoller:
        """Register an async handler for *event*.  Returns self for chaining."""
        self._handlers.setdefault(event, []).append(handler)
        return self

    def off(self, event: str, handler: _Handler) -> RedSkyPoller:
        """Remove a previously registered handler."""
        handlers = self._handlers.get(event, [])
        try:
            handlers.remove(handler)
        except ValueError:
            pass
        return self

    async def _emit(self, event: str, data: RedSkyProductData) -> None:
        """Fire all handlers registered for *event*."""
        for handler in self._handlers.get(event, []):
            try:
                await handler(data)
            except Exception:
                logger.exception("Handler for '%s' raised an exception", event)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the polling loop as a background asyncio task."""
        if self._running:
            logger.warning("RedSkyPoller for TCIN %s is already running", self.tcin)
            return

        self._running = True
        self._client = httpx.AsyncClient(
            headers=API_HEADERS,
            follow_redirects=True,
            timeout=httpx.Timeout(15.0),
            http2=True,
        )
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "RedSkyPoller started — TCIN=%s interval=%.1fs store=%s",
            self.tcin, self.interval_s, self.store_id,
        )

    async def stop(self) -> None:
        """Stop the polling loop and close the HTTP client."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._task = None
        self._client = None
        logger.info("RedSkyPoller stopped — TCIN=%s", self.tcin)

    # ------------------------------------------------------------------
    # Core polling loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while self._running:
            # Respect backoff
            now = time.monotonic()
            if now < self._backoff_until:
                wait = self._backoff_until - now
                logger.debug("RedSkyPoller backing off for %.1fs", wait)
                await asyncio.sleep(wait)
                continue

            try:
                product_data = await self._fetch()
                if product_data is None:
                    # Non-fatal parse issue — wait and retry.
                    await asyncio.sleep(self.interval_s)
                    continue

                self._consecutive_errors = 0  # reset on success

                current_status = product_data.availability_status
                is_available = (
                    current_status == "IN_STOCK"
                    or product_data.is_purchasable
                )

                # Log state transitions
                if current_status != self._last_status:
                    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    logger.info(
                        "[%s] TCIN %s status changed: %s → %s (purchasable=%s, price=%s)",
                        ts, self.tcin, self._last_status or "INIT",
                        current_status, product_data.is_purchasable,
                        product_data.price or "N/A",
                    )
                    self._last_status = current_status
                    await self._emit("status_change", product_data)

                if is_available:
                    logger.info(
                        "AVAILABLE — TCIN %s: status=%s purchasable=%s price=%s",
                        self.tcin, current_status,
                        product_data.is_purchasable, product_data.price,
                    )
                    await self._emit("available", product_data)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error(str(exc))

            await asyncio.sleep(self.interval_s)

    # ------------------------------------------------------------------
    # HTTP fetch + parse
    # ------------------------------------------------------------------

    async def _fetch(self) -> RedSkyProductData | None:
        """Hit the RedSky product_fulfillment_v1 endpoint and parse the response.

        Falls back to legacy pdp_client_v1 on 404/410.
        """
        assert self._client is not None

        api_key = self._api_keys[self._key_index % len(self._api_keys)]
        params = {
            "key": api_key,
            "tcin": self.tcin,
            "is_bot": "false",
            "store_id": self.store_id,
            "store_positions_store_id": self.store_id,
            "pricing_store_id": self.store_id,
            "has_pricing_store_id": "true",
            "has_store_positions_store_id": "true",
            "latitude": "39.282024",
            "longitude": "-76.569695",
            "state": "MD",
            "zip": "21224",
            "visitor_id": self._visitor_id,
            "channel": "WEB",
            "page": f"/p/A-{self.tcin}",
        }

        headers = {
            **API_HEADERS,
            "Referer": f"https://www.target.com/p/A-{self.tcin}",
            "Origin": "https://www.target.com",
            "x-application-name": "web",
        }

        resp = await self._client.get(self.REDSKY_FULFILLMENT, params=params, headers=headers)

        # Fall back to legacy endpoint on 404/410
        if resp.status_code in (404, 410):
            logger.debug("RedSkyPoller: product_fulfillment_v1 returned %d, trying legacy pdp_client_v1", resp.status_code)
            legacy_params = {
                "key": api_key,
                "tcin": self.tcin,
                "is_bot": "false",
                "store_id": self.store_id,
                "pricing_store_id": self.store_id,
                "has_pricing_store_id": "true",
                "has_financing_options": "true",
                "include_obsolete": "true",
                "skip_personalized": "true",
                "skip_variation_hierarchy": "true",
                "visitor_id": self._visitor_id,
                "channel": "WEB",
                "page": f"/p/A-{self.tcin}",
            }
            resp = await self._client.get(self.REDSKY_PDP_LEGACY, params=legacy_params, headers=headers)

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            self._record_rate_limit(float(retry_after) if retry_after else None)
            return None

        if resp.status_code == 403:
            logger.warning(
                "RedSkyPoller: 403 for TCIN %s — rotating key & visitor_id", self.tcin,
            )
            self._key_index += 1
            self._visitor_id = uuid.uuid4().hex
            self._record_error("403 Forbidden — rotated credentials")
            return None

        if resp.status_code != 200:
            self._record_error(f"HTTP {resp.status_code}")
            return None

        return self._parse(resp.json())

    def _parse(self, data: dict) -> RedSkyProductData | None:
        """Extract availability fields from the pdp_client_v1 response."""
        try:
            product = data["data"]["product"]
        except (KeyError, TypeError):
            logger.warning("RedSkyPoller: malformed response — missing data.product")
            return None

        # Title
        title = ""
        item = product.get("item", {})
        if isinstance(item, dict):
            desc = item.get("product_description", {})
            if isinstance(desc, dict):
                title = desc.get("title", "")

        # Price
        price = ""
        price_info = product.get("price", {})
        if isinstance(price_info, dict):
            price = price_info.get("formatted_current_price", "")

        # Availability status — check multiple paths
        fulfillment = product.get("fulfillment", {})
        avail_status = "UNKNOWN"
        is_purchasable = False

        # 1. shipping_options.availability_status
        shipping = fulfillment.get("shipping_options", {})
        if isinstance(shipping, dict):
            s = shipping.get("availability_status", "")
            if s:
                avail_status = s

        # 2. product-level availability
        product_avail = product.get("availability", {})
        if isinstance(product_avail, dict):
            pa = product_avail.get("availability_status", "")
            if pa:
                avail_status = pa
            is_purchasable = bool(product_avail.get("is_purchasable", False))

        # 3. Broad check: any IN_STOCK in fulfillment JSON
        if avail_status not in ("IN_STOCK", "LIMITED_STOCK"):
            fulfillment_str = json.dumps(fulfillment)
            if '"IN_STOCK"' in fulfillment_str or '"AVAILABLE"' in fulfillment_str:
                avail_status = "IN_STOCK"

        return RedSkyProductData(
            tcin=self.tcin,
            title=title,
            price=price,
            availability_status=avail_status,
            is_purchasable=is_purchasable,
            fulfillment=fulfillment,
            raw=product,
        )

    # ------------------------------------------------------------------
    # Backoff helpers
    # ------------------------------------------------------------------

    def _record_error(self, reason: str) -> None:
        """Apply exponential backoff: 2s, 4s, 8s, 16s … capped at 300s."""
        self._consecutive_errors += 1
        backoff = min(2 ** self._consecutive_errors, 300)
        self._backoff_until = time.monotonic() + backoff
        logger.warning(
            "RedSkyPoller error for TCIN %s: %s — backing off %.0fs (attempt %d)",
            self.tcin, reason, backoff, self._consecutive_errors,
        )

    def _record_rate_limit(self, retry_after: float | None) -> None:
        """Handle 429 rate-limit with exponential backoff (floor 60s)."""
        self._consecutive_errors += 1
        backoff = min(60 * (2 ** (self._consecutive_errors - 1)), 300)
        if retry_after is not None:
            backoff = max(retry_after, 60)
        self._backoff_until = time.monotonic() + backoff
        logger.warning(
            "RedSkyPoller rate-limited (429) for TCIN %s — backing off %.0fs",
            self.tcin, backoff,
        )


# ======================================================================
# RedSkySearch — keyword → TCIN discovery
# ======================================================================


@dataclass
class SearchResult:
    """A single product returned from a retailer search API."""

    tcin: str  # product ID (TCIN for Target, SKU for Best Buy, etc.)
    title: str = ""
    price: str = ""
    url: str = ""
    image_url: str = ""
    availability_status: str = ""
    is_purchasable: bool = False
    sold_by: str = ""  # e.g. "Target" or marketplace seller name
    street_date: str = ""  # release/launch date if upcoming (YYYY-MM-DD)
    release_label: str = ""  # e.g. "Pre-order", "Coming soon", "Launches Apr 25"
    retailer: str = "target"  # which retailer this result came from


def _extract_release_info(product: dict) -> tuple[str, str]:
    """Extract street/release date and a human-readable label from a product.

    Returns (street_date, release_label) where street_date is ISO format
    (e.g. "2026-04-25") and release_label is a UI string like "Pre-order"
    or "Launches Apr 25".

    Target uses several fields for this:
    - item.street_date / item.release_date (ISO date string)
    - product.availability.availability_status = "PRE_ORDER" / "COMING_SOON"
    - fulfillment.shipping_options.availability_status = "PRE_ORDER"
    - Various date fields in scheduled_delivery
    """
    product_str = json.dumps(product)
    street_date = ""
    label = ""

    # 1. Explicit date fields in item data
    item = product.get("item", {})
    if isinstance(item, dict):
        for date_key in ("street_date", "release_date", "launch_date",
                         "expected_availability_date"):
            val = item.get(date_key, "")
            if val and isinstance(val, str) and len(val) >= 10:
                street_date = val[:10]  # take YYYY-MM-DD part
                break

    # 2. Dates in fulfillment data
    if not street_date:
        fulfillment = product.get("fulfillment", {})
        if isinstance(fulfillment, dict):
            for method_key in ("shipping_options", "scheduled_delivery"):
                method = fulfillment.get(method_key, {})
                if isinstance(method, dict):
                    for date_key in ("available_date", "expected_delivery_date",
                                     "street_date"):
                        val = method.get(date_key, "")
                        if val and isinstance(val, str) and len(val) >= 10:
                            street_date = val[:10]
                            break
                if street_date:
                    break

    # 3. Regex scan for any date field we missed
    if not street_date:
        date_match = re.search(
            r'"(?:street_date|release_date|launch_date|available_date)"'
            r'\s*:\s*"(\d{4}-\d{2}-\d{2})',
            product_str,
        )
        if date_match:
            street_date = date_match.group(1)

    # 4. Determine status label from availability signals
    avail = product.get("availability", {})
    if isinstance(avail, dict):
        status = avail.get("availability_status", "")
        if status == "PRE_ORDER":
            label = "Pre-order"
        elif status == "COMING_SOON":
            label = "Coming soon"

    # Check fulfillment for pre-order status
    if not label:
        for status_str in ('"PRE_ORDER"', '"COMING_SOON"'):
            if status_str in product_str:
                label = "Pre-order" if "PRE_ORDER" in status_str else "Coming soon"
                break

    # Build a descriptive label with date if available
    if street_date and label:
        try:
            dt = datetime.strptime(street_date, "%Y-%m-%d")
            label = f"{label} \u2014 {dt.strftime('%b %d')}"
        except ValueError:
            pass
    elif street_date and not label:
        try:
            dt = datetime.strptime(street_date, "%Y-%m-%d")
            label = f"Launches {dt.strftime('%b %d')}"
        except ValueError:
            label = f"Launches {street_date}"

    return street_date, label


def _extract_seller(product: dict) -> str:
    """Determine who sells a Target product by scanning the full product dict.

    Returns "Target" for first-party items, or the seller/vendor name for
    marketplace items.  Uses a multi-path approach because the search API
    nests seller info inconsistently across product types.
    """
    product_str = json.dumps(product)

    # 1. Explicit seller_name in marketplace data (strongest signal)
    for key in ("marketplace", "marketplace_attributes"):
        mp = product.get(key, {})
        if isinstance(mp, dict):
            seller = mp.get("seller_name", "") or mp.get("seller_display_name", "")
            if seller and seller.lower() != "target":
                return seller
        # Sometimes it's nested under item
        item = product.get("item", {})
        if isinstance(item, dict):
            mp2 = item.get(key, {})
            if isinstance(mp2, dict):
                seller = mp2.get("seller_name", "") or mp2.get("seller_display_name", "")
                if seller and seller.lower() != "target":
                    return seller

    # 2. relationship_type in item data
    item = product.get("item", {})
    if isinstance(item, dict):
        rel = item.get("relationship_type", "")
        if rel in ("SA", "TAF"):
            return "Third-party seller"

    # 3. product_vendors — array of vendor objects
    vendors = item.get("product_vendors", []) if isinstance(item, dict) else []
    if isinstance(vendors, list):
        for v in vendors:
            if isinstance(v, dict):
                vname = v.get("vendor_name", "")
                if vname and vname.upper() != "TARGET":
                    return vname

    # 4. Broad string scan for marketplace / third-party signals
    # Check for "Target Plus" partner program or explicit marketplace flags
    if '"Target Plus"' in product_str or '"target plus"' in product_str.lower():
        return "Target Plus partner"
    # Look for seller_name deeper in nested structures
    match = re.search(r'"seller_name"\s*:\s*"([^"]+)"', product_str)
    if match:
        seller = match.group(1)
        if seller.lower() != "target" and seller.lower() != "target corporation":
            return seller

    return "Target"


class RedSkySearch:
    """Search Target's RedSky API by keyword and resolve to TCINs.

    Parameters
    ----------
    store_id : str
        Target store ID for location-aware results.
    max_results : int
        Cap on how many search results to return (default 10).
    api_key : str | None
        Override the default RedSky API key.
    """

    SEARCH_URL = "https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v2"

    _API_KEYS = RedSkyPoller._API_KEYS

    def __init__(
        self,
        store_id: str = "2845",
        max_results: int = 10,
        api_key: str | None = None,
    ) -> None:
        self.store_id = store_id
        self.max_results = max_results
        self._api_keys = [api_key] if api_key else list(self._API_KEYS)
        self._visitor_id = uuid.uuid4().hex

    @staticmethod
    def _extract_tcin(text: str) -> str | None:
        """Try to extract a TCIN from a Target URL or raw number."""
        # Target URL: .../A-12345678 or .../A-12345678?...
        match = re.search(r"A-(\d{6,10})", text)
        if match:
            return match.group(1)
        # Raw TCIN (just digits, 6-10 chars)
        stripped = text.strip()
        if re.fullmatch(r"\d{6,10}", stripped):
            return stripped
        return None

    async def lookup_tcin(self, tcin: str) -> SearchResult | None:
        """Look up a single TCIN via the pdp_client_v1 endpoint.

        This works for products that are delisted from search but still
        have a product page.
        """
        async with httpx.AsyncClient(
            headers=API_HEADERS,
            follow_redirects=True,
            timeout=httpx.Timeout(15.0),
            http2=True,
        ) as client:
            for api_key in self._api_keys:
                params = {
                    "key": api_key,
                    "tcin": tcin,
                    "is_bot": "false",
                    "store_id": self.store_id,
                    "pricing_store_id": self.store_id,
                    "has_pricing_store_id": "true",
                    "has_financing_options": "true",
                    "include_obsolete": "true",
                    "skip_personalized": "true",
                    "skip_variation_hierarchy": "true",
                    "visitor_id": self._visitor_id,
                    "channel": "WEB",
                    "page": f"/p/A-{tcin}",
                }
                headers = {
                    **API_HEADERS,
                    "Referer": f"https://www.target.com/p/A-{tcin}",
                    "Origin": "https://www.target.com",
                    "x-application-name": "web",
                }
                try:
                    resp = await client.get(
                        RedSkyPoller.REDSKY_PDP, params=params, headers=headers,
                    )
                except httpx.HTTPError as exc:
                    logger.warning("RedSkySearch lookup: network error: %s", exc)
                    continue

                if resp.status_code == 403:
                    self._visitor_id = uuid.uuid4().hex
                    continue
                if resp.status_code != 200:
                    logger.debug("RedSkySearch lookup: HTTP %d for TCIN %s", resp.status_code, tcin)
                    continue

                try:
                    product = resp.json().get("data", {}).get("product", {})
                except Exception:
                    continue

                if not product:
                    continue

                # Parse the same fields as search results
                item_data = product.get("item", {})
                title = ""
                if isinstance(item_data, dict):
                    desc = item_data.get("product_description", {})
                    if isinstance(desc, dict):
                        title = desc.get("title", "")

                price = ""
                price_info = product.get("price", {})
                if isinstance(price_info, dict):
                    price = price_info.get("formatted_current_price", "")

                image_url = ""
                if isinstance(item_data, dict):
                    enrichment = item_data.get("enrichment", {})
                    if isinstance(enrichment, dict):
                        images = enrichment.get("images", {})
                        if isinstance(images, dict):
                            image_url = images.get("primary_image_url", "")

                avail_status = ""
                is_purchasable = False
                fulfillment = product.get("fulfillment", {})
                if isinstance(fulfillment, dict):
                    shipping = fulfillment.get("shipping_options", {})
                    if isinstance(shipping, dict):
                        avail_status = shipping.get("availability_status", "")
                product_avail = product.get("availability", {})
                if isinstance(product_avail, dict):
                    pa = product_avail.get("availability_status", "")
                    if pa:
                        avail_status = pa
                    is_purchasable = bool(product_avail.get("is_purchasable", False))

                # Broad check
                if avail_status not in ("IN_STOCK", "LIMITED_STOCK", "PRE_ORDER", "COMING_SOON"):
                    ful_str = json.dumps(fulfillment)
                    if '"IN_STOCK"' in ful_str or '"AVAILABLE"' in ful_str:
                        avail_status = "IN_STOCK"

                sold_by = _extract_seller(product)
                street_date, release_label = _extract_release_info(product)

                logger.debug("RedSkySearch: direct lookup found TCIN %s — %s", tcin, title)
                return SearchResult(
                    tcin=tcin,
                    title=title,
                    price=price,
                    url=f"https://www.target.com/p/-/A-{tcin}",
                    image_url=image_url,
                    availability_status=avail_status,
                    is_purchasable=is_purchasable,
                    sold_by=sold_by,
                    street_date=street_date,
                    release_label=release_label,
                )

        logger.warning("RedSkySearch: direct lookup failed for TCIN %s", tcin)
        return None

    async def find(
        self,
        keyword: str,
        *,
        sold_by_target_only: bool = False,
        include_out_of_stock: bool = False,
        offset: int = 0,
    ) -> list[SearchResult]:
        """Search Target for *keyword* and return matching products.

        If *keyword* is a TCIN or Target URL, does a direct PDP lookup
        instead of searching (works for delisted/unlisted products).

        If *sold_by_target_only* is True, results are filtered to items
        sold and shipped by Target (excludes marketplace / 3P sellers).

        If *include_out_of_stock* is True, disables Target's default
        purchasability filter so unlisted / OOS products can appear.
        """
        # Direct TCIN / URL lookup — bypass search index entirely
        tcin = self._extract_tcin(keyword)
        if tcin:
            result = await self.lookup_tcin(tcin)
            if result:
                return [result]
            return []

        async with httpx.AsyncClient(
            headers=API_HEADERS,
            follow_redirects=True,
            timeout=httpx.Timeout(15.0),
            http2=True,
        ) as client:
            for i, api_key in enumerate(self._api_keys):
                params = {
                    "key": api_key,
                    "keyword": keyword,
                    "channel": "WEB",
                    "count": str(self.max_results),
                    "default_purchasability_filter": "false" if include_out_of_stock else "true",
                    "is_bot": "false",
                    "offset": str(offset),
                    "page": f"/s/{keyword}",
                    "pricing_store_id": self.store_id,
                    "store_ids": self.store_id,
                    "visitor_id": self._visitor_id,
                }
                headers = {
                    **API_HEADERS,
                    "Referer": f"https://www.target.com/s?searchTerm={keyword}",
                    "Origin": "https://www.target.com",
                    "x-application-name": "web",
                }

                try:
                    resp = await client.get(
                        self.SEARCH_URL, params=params, headers=headers,
                    )
                except httpx.HTTPError as exc:
                    logger.warning("RedSkySearch network error: %s", exc)
                    continue

                if resp.status_code == 403:
                    logger.warning(
                        "RedSkySearch: 403 with key ...%s — trying next",
                        api_key[-6:],
                    )
                    self._visitor_id = uuid.uuid4().hex
                    continue

                if resp.status_code == 429:
                    logger.warning("RedSkySearch: rate-limited (429)")
                    return []

                if resp.status_code != 200:
                    logger.warning(
                        "RedSkySearch: HTTP %d for '%s'", resp.status_code, keyword,
                    )
                    continue

                results = self._parse_search(resp.json())
                if sold_by_target_only:
                    results = [
                        r for r in results
                        if r.sold_by.lower() in ("target", "target corporation")
                    ]
                return results

        logger.error("RedSkySearch: all API keys exhausted for '%s'", keyword)
        return []

    def _parse_search(self, data: dict) -> list[SearchResult]:
        """Extract products from the plp_search_v2 response."""
        results: list[SearchResult] = []
        try:
            products = (
                data.get("data", {})
                .get("search", {})
                .get("products", [])
            )
        except (AttributeError, TypeError):
            logger.warning("RedSkySearch: unexpected response structure")
            return results

        for item in products[: self.max_results]:
            try:
                tcin = item.get("tcin", "")
                if not tcin:
                    continue

                # Title
                title = ""
                item_data = item.get("item", {})
                if isinstance(item_data, dict):
                    desc = item_data.get("product_description", {})
                    if isinstance(desc, dict):
                        title = desc.get("title", "")

                # Price
                price = ""
                price_info = item.get("price", {})
                if isinstance(price_info, dict):
                    price = price_info.get("formatted_current_price", "")

                # URL
                url = f"https://www.target.com/p/-/A-{tcin}"

                # Image
                image_url = ""
                enrichment = item.get("item", {}).get("enrichment", {})
                if isinstance(enrichment, dict):
                    images = enrichment.get("images", {})
                    if isinstance(images, dict):
                        image_url = images.get("primary_image_url", "")

                # Availability
                avail_status = ""
                is_purchasable = False
                fulfillment = item.get("fulfillment", {})
                if isinstance(fulfillment, dict):
                    shipping = fulfillment.get("shipping_options", {})
                    if isinstance(shipping, dict):
                        avail_status = shipping.get("availability_status", "")
                product_avail = item.get("availability", {})
                if isinstance(product_avail, dict):
                    pa = product_avail.get("availability_status", "")
                    if pa:
                        avail_status = pa
                    is_purchasable = bool(
                        product_avail.get("is_purchasable", False)
                    )

                # Seller / sold-by info.
                # Target's search API uses multiple paths to indicate the seller:
                #   - item.relationship_type: "TAC" (1P Target), "TAF" (fulfilled
                #     by Target but 3P seller), "SA" (3P seller-fulfilled)
                #   - marketplace / marketplace_attributes with seller_name
                #   - product_vendors with vendor_name
                #   - fulfillment.vendor_id or partner fields
                # Since the exact nesting varies, we do a broad scan of the
                # product JSON for known third-party signals.
                sold_by = _extract_seller(item)

                # Release / launch date for upcoming products
                street_date, release_label = _extract_release_info(item)

                results.append(SearchResult(
                    tcin=tcin,
                    title=title,
                    price=price,
                    url=url,
                    image_url=image_url,
                    availability_status=avail_status,
                    is_purchasable=is_purchasable,
                    sold_by=sold_by,
                    street_date=street_date,
                    release_label=release_label,
                ))
            except Exception:
                logger.debug("RedSkySearch: skipping unparseable item", exc_info=True)
                continue

        logger.debug(
            "RedSkySearch: found %d products for keyword query", len(results),
        )
        return results

    async def find_and_poll(
        self,
        keyword: str,
        on_available: _Handler,
        interval_ms: int = 5_000,
    ) -> list[RedSkyPoller]:
        """Search for *keyword*, spawn a RedSkyPoller for each result.

        Returns the list of started pollers (caller is responsible for
        stopping them via ``poller.stop()``).
        """
        results = await self.find(keyword)
        if not results:
            logger.warning(
                "RedSkySearch: no results for '%s' — nothing to poll", keyword,
            )
            return []

        pollers: list[RedSkyPoller] = []
        for sr in results:
            poller = RedSkyPoller(
                tcin=sr.tcin,
                interval_ms=interval_ms,
                store_id=self.store_id,
            )
            poller.on("available", on_available)
            await poller.start()
            pollers.append(poller)
            logger.debug(
                "RedSkySearch: polling TCIN %s — %s (%s)",
                sr.tcin, sr.title, sr.price,
            )

        return pollers

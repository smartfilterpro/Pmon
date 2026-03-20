"""Dedicated RedSky API poller for a single Target TCIN.

Unlike TargetMonitor (which checks stock once per engine cycle across many
products), RedSkyPoller runs its own tight polling loop for a single product
and fires an async callback the moment availability is detected.  This lets
the checkout orchestrator subscribe and trigger immediately:

    poller = RedSkyPoller(tcin="12345678", interval_ms=5000)
    poller.on("available", my_async_handler)
    await poller.start()

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

    REDSKY_PDP = "https://redsky.target.com/redsky_aggregations/v1/web/pdp_client_v1"

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
        """Hit the RedSky pdp_client_v1 endpoint and parse the response."""
        assert self._client is not None

        api_key = self._api_keys[self._key_index % len(self._api_keys)]
        params = {
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
            "page": f"/p/{self.tcin}",
        }

        headers = {
            **API_HEADERS,
            "Referer": f"https://www.target.com/p/A-{self.tcin}",
            "Origin": "https://www.target.com",
            "x-application-name": "web",
        }

        resp = await self._client.get(self.REDSKY_PDP, params=params, headers=headers)

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

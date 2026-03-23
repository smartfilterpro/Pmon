"""Best Buy product search via the official Best Buy Products API.

Uses the public API at api.bestbuy.com/v1/products for keyword search.
Requires a free API key from https://developer.bestbuy.com/.

Set the BESTBUY_API_KEY environment variable to enable search.
"""

from __future__ import annotations

import logging
import os
import re

import httpx

from pmon.monitors.base import API_HEADERS
from pmon.monitors.redsky_poller import SearchResult

logger = logging.getLogger(__name__)

# Official Best Buy Products API
_API_BASE = "https://api.bestbuy.com/v1/products"

# Fields we request from the API
_SHOW_FIELDS = ",".join([
    "sku", "name", "salePrice", "regularPrice",
    "image", "url", "onlineAvailability",
    "inStoreAvailability", "orderable",
    "thumbnailImage", "largeFrontImage",
])

# Headers for direct SKU lookup (internal endpoint, no API key needed)
_BB_HEADERS = {
    **API_HEADERS,
    "Referer": "https://www.bestbuy.com/",
    "Origin": "https://www.bestbuy.com",
    "Sec-Fetch-Site": "same-origin",
}


class BestBuySearch:
    """Search Best Buy's product catalog by keyword.

    Parameters
    ----------
    max_results : int
        Cap on how many search results to return (default 10).
    """

    def __init__(self, max_results: int = 10) -> None:
        self.max_results = max_results
        self._api_key = os.environ.get("BESTBUY_API_KEY", "")

    @staticmethod
    def _extract_sku(text: str) -> str | None:
        """Try to extract a Best Buy SKU from a URL or raw number.

        Best Buy URLs:
          Old: /site/product-name/1234567.p
          New: /product/product-name/JJG2TLCK6H  (BSIN)
        """
        match = re.search(r"/(\d{7,8})\.p", text)
        if match:
            return match.group(1)
        stripped = text.strip()
        if re.fullmatch(r"\d{7,8}", stripped):
            return stripped
        return None

    @staticmethod
    def _extract_bsin(text: str) -> str | None:
        """Extract BSIN from new Best Buy URL format."""
        match = re.search(r"/product/[^/]+/([A-Za-z0-9]{6,12})(?:\?|$|#|/)", text)
        if not match:
            match = re.search(r"/product/[^/]+/([A-Za-z0-9]{6,12})\s*$", text)
        return match.group(1) if match else None

    async def lookup_sku(self, sku: str) -> SearchResult | None:
        """Look up a single product by SKU.

        Uses the official API if key is available, otherwise falls back
        to the internal priceBlocks endpoint.
        """
        if self._api_key:
            return await self._lookup_sku_api(sku)
        return await self._lookup_sku_internal(sku)

    async def _lookup_sku_api(self, sku: str) -> SearchResult | None:
        """Look up a SKU via the official Best Buy API."""
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            try:
                resp = await client.get(
                    f"{_API_BASE}/{sku}.json",
                    params={
                        "apiKey": self._api_key,
                        "show": _SHOW_FIELDS,
                    },
                )
                if resp.status_code != 200:
                    logger.debug("BestBuySearch: API lookup HTTP %d for SKU %s", resp.status_code, sku)
                    return None

                item = resp.json()
                return self._item_to_result(item)
            except Exception as exc:
                logger.warning("BestBuySearch: API lookup failed for SKU %s: %s", sku, exc)
                return None

    async def _lookup_sku_internal(self, sku: str) -> SearchResult | None:
        """Look up a single product by SKU via the internal priceBlocks API."""
        async with httpx.AsyncClient(
            headers=_BB_HEADERS,
            follow_redirects=True,
            timeout=httpx.Timeout(15.0),
            http2=True,
        ) as client:
            try:
                resp = await client.get(
                    "https://www.bestbuy.com/api/3.0/priceBlocks",
                    params={"skus": sku},
                )
                if resp.status_code != 200:
                    logger.debug("BestBuySearch: priceBlocks HTTP %d for SKU %s", resp.status_code, sku)
                    return None

                data = resp.json()
                items = data if isinstance(data, list) else [data]
                if not items:
                    return None

                item = items[0]
                title = item.get("sku", {}).get("names", {}).get("short", "")
                if not title:
                    title = item.get("sku", {}).get("names", {}).get("long", "")

                price = ""
                price_info = item.get("price", {})
                current = price_info.get("currentPrice")
                if current:
                    price = f"${current}"

                image_url = item.get("sku", {}).get("image", "")

                button_state = item.get("buttonState", {}).get("buttonState", "")
                avail_status = "IN_STOCK" if button_state == "ADD_TO_CART" else "OUT_OF_STOCK"
                is_purchasable = button_state == "ADD_TO_CART"

                if button_state == "PRE_ORDER":
                    avail_status = "PRE_ORDER"
                    is_purchasable = True
                elif button_state == "COMING_SOON":
                    avail_status = "COMING_SOON"

                url = f"https://www.bestbuy.com/site/-/{sku}.p"

                return SearchResult(
                    tcin=sku,
                    title=title,
                    price=price,
                    url=url,
                    image_url=image_url,
                    availability_status=avail_status,
                    is_purchasable=is_purchasable,
                    sold_by="Best Buy",
                    retailer="bestbuy",
                )
            except Exception as exc:
                logger.warning("BestBuySearch: lookup failed for SKU %s: %s", sku, exc)
                return None

    async def find(
        self,
        keyword: str,
        *,
        include_out_of_stock: bool = False,
        offset: int = 0,
    ) -> list[SearchResult]:
        """Search Best Buy for *keyword* and return matching products.

        If *keyword* is a SKU or Best Buy URL, does a direct lookup.
        Requires BESTBUY_API_KEY env var for keyword search.
        """
        # Direct SKU lookup
        sku = self._extract_sku(keyword)
        if sku:
            result = await self.lookup_sku(sku)
            if result:
                return [result]
            return []

        # BSIN URL lookup — try using BSIN as SKU identifier
        bsin = self._extract_bsin(keyword)
        if bsin:
            result = await self.lookup_sku(bsin)
            if result:
                return [result]
            return []

        if not self._api_key:
            logger.warning(
                "BestBuySearch: BESTBUY_API_KEY not set — keyword search unavailable. "
                "Get a free key at https://developer.bestbuy.com/"
            )
            return []

        page = (offset // self.max_results) + 1

        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            try:
                resp = await client.get(
                    f"{_API_BASE}(search={keyword})",
                    params={
                        "apiKey": self._api_key,
                        "show": _SHOW_FIELDS,
                        "pageSize": str(self.max_results),
                        "page": str(page),
                        "format": "json",
                    },
                )
            except httpx.HTTPError as exc:
                logger.warning("BestBuySearch: API network error: %s", exc)
                return []

            if resp.status_code == 403:
                logger.warning("BestBuySearch: API 403 — check your BESTBUY_API_KEY")
                return []

            if resp.status_code != 200:
                logger.warning("BestBuySearch: API HTTP %d for '%s'", resp.status_code, keyword)
                return []

            data = resp.json()
            products = data.get("products", [])

            results: list[SearchResult] = []
            for item in products:
                result = self._item_to_result(item)
                if not result:
                    continue
                if not include_out_of_stock and result.availability_status == "OUT_OF_STOCK":
                    continue
                results.append(result)

            logger.debug("BestBuySearch: API returned %d products for '%s'", len(results), keyword)
            return results

    def _item_to_result(self, item: dict) -> SearchResult | None:
        """Convert a Best Buy API product item to a SearchResult."""
        try:
            sku = str(item.get("sku", ""))
            if not sku:
                return None

            title = item.get("name", "")
            sale_price = item.get("salePrice")
            regular_price = item.get("regularPrice")
            price_val = sale_price or regular_price
            price = f"${price_val}" if price_val else ""

            image_url = item.get("image") or item.get("thumbnailImage") or item.get("largeFrontImage") or ""

            url = item.get("url", "")
            if not url:
                url = f"https://www.bestbuy.com/site/-/{sku}.p"

            online = item.get("onlineAvailability", False)
            in_store = item.get("inStoreAvailability", False)
            orderable = item.get("orderable", "")

            if orderable == "PreOrder":
                avail_status = "PRE_ORDER"
                is_purchasable = True
            elif orderable == "ComingSoon":
                avail_status = "COMING_SOON"
                is_purchasable = False
            elif online or in_store:
                avail_status = "IN_STOCK"
                is_purchasable = True
            else:
                avail_status = "OUT_OF_STOCK"
                is_purchasable = False

            return SearchResult(
                tcin=sku,
                title=title,
                price=price,
                url=url,
                image_url=image_url,
                availability_status=avail_status,
                is_purchasable=is_purchasable,
                sold_by="Best Buy",
                retailer="bestbuy",
            )
        except Exception:
            logger.debug("BestBuySearch: failed to parse product item", exc_info=True)
            return None

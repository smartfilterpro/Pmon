"""Pokemon Center product search — scrapes search results page.

Pokemon Center doesn't expose a public search API. Instead we hit
their Next.js search page and parse the embedded __NEXT_DATA__ JSON
for structured product results.  Falls back to HTML scraping if the
JSON approach fails.

Uses the same HTTP client/cookies as the PokemonCenterMonitor to
share WAF session state and avoid double-fingerprinting.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote_plus, urljoin

import httpx

from pmon.monitors.base import DEFAULT_HEADERS
from pmon.monitors.redsky_poller import SearchResult

logger = logging.getLogger(__name__)

_BASE = "https://www.pokemoncenter.com"
_SEARCH_URL = f"{_BASE}/search"

# Headers that mimic a browser navigating to the search page.
_SEARCH_HEADERS = {
    **DEFAULT_HEADERS,
    "Referer": f"{_BASE}/",
    "Sec-Fetch-Site": "same-origin",
}

# Headers for internal API calls (XHR from the page).
_API_HEADERS = {
    **DEFAULT_HEADERS,
    "Accept": "application/json",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Referer": f"{_BASE}/",
    "x-application-name": "pokemon-center",
}


class PokemonCenterSearch:
    """Search the Pokemon Center product catalog by keyword.

    Parameters
    ----------
    max_results : int
        Cap on results returned (default 20).
    client : httpx.AsyncClient | None
        Reuse an existing client (e.g. from the monitor) to share
        WAF cookies and TLS session state.
    """

    def __init__(
        self,
        max_results: int = 20,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.max_results = max_results
        self._external_client = client

    async def find(
        self,
        keyword: str,
        *,
        include_out_of_stock: bool = False,
        offset: int = 0,
    ) -> list[SearchResult]:
        """Search Pokemon Center for *keyword* and return matching products.

        If *keyword* looks like a product URL, does a direct product
        page lookup instead.
        """
        # Direct product page lookup
        if "pokemoncenter.com" in keyword.lower():
            result = await self._lookup_product_url(keyword.strip())
            if result:
                return [result]
            return []

        # Keyword search — try API first, then page scrape
        results = await self._search_api(keyword, offset=offset)
        if results is None:
            results = await self._search_page(keyword, offset=offset)

        if not include_out_of_stock:
            results = [r for r in results if r.availability_status != "OUT_OF_STOCK"]

        return results[: self.max_results]

    # ------------------------------------------------------------------
    # Strategy 1: Internal search API (tpci-ecommweb-api)
    # ------------------------------------------------------------------

    async def _search_api(
        self, keyword: str, *, offset: int = 0
    ) -> list[SearchResult] | None:
        """Try the internal tpci-ecommweb-api product search endpoint.

        Returns None if the endpoint is unavailable or blocked so the
        caller can fall back to page scraping.
        """
        client = self._get_client()
        close_after = client is not self._external_client

        try:
            # Pokemon Center's internal API for search suggestions/results
            resp = await client.get(
                f"{_BASE}/tpci-ecommweb-api/product-search",
                params={
                    "q": keyword,
                    "count": str(self.max_results),
                    "offset": str(offset),
                    "format": "nodatalinks",
                },
                headers=_API_HEADERS,
            )

            if resp.status_code in (403, 404, 429):
                logger.debug(
                    "PokemonCenterSearch: API returned %d, falling back to page scrape",
                    resp.status_code,
                )
                return None

            resp.raise_for_status()
            data = resp.json()
            return self._parse_api_results(data)

        except Exception as exc:
            logger.debug("PokemonCenterSearch: API search failed: %s", exc)
            return None
        finally:
            if close_after:
                await client.aclose()

    def _parse_api_results(self, data: dict) -> list[SearchResult]:
        """Parse results from the internal API response."""
        results: list[SearchResult] = []

        # The API may return results under various keys
        products = (
            data.get("products")
            or data.get("results")
            or data.get("data", {}).get("products")
            or []
        )
        if isinstance(products, dict):
            products = products.get("items", products.get("edges", []))

        for item in products:
            try:
                result = self._product_to_result(item)
                if result:
                    results.append(result)
            except Exception:
                continue

        return results

    # ------------------------------------------------------------------
    # Strategy 2: Search page scrape with __NEXT_DATA__ parsing
    # ------------------------------------------------------------------

    async def _search_page(
        self, keyword: str, *, offset: int = 0
    ) -> list[SearchResult]:
        """Fetch the search results page and parse __NEXT_DATA__."""
        client = self._get_client()
        close_after = client is not self._external_client

        try:
            page = (offset // self.max_results) + 1
            params = {"q": keyword}
            if page > 1:
                params["page"] = str(page)

            resp = await client.get(
                _SEARCH_URL,
                params=params,
                headers=_SEARCH_HEADERS,
            )

            if resp.status_code in (403, 429):
                logger.warning(
                    "PokemonCenterSearch: search page returned %d — WAF may be blocking",
                    resp.status_code,
                )
                return []

            resp.raise_for_status()
            html = resp.text

            # Check for bot block page
            lower = html.lower()
            if "unusual activity" in lower or "access to this page has been denied" in lower:
                logger.warning("PokemonCenterSearch: bot detection triggered on search page")
                return []

            # Try __NEXT_DATA__ first
            results = self._parse_next_data_search(html)
            if results:
                return results

            # Try JSON-LD
            results = self._parse_jsonld_search(html)
            if results:
                return results

            # Fallback to HTML scraping
            return self._parse_html_search(html)

        except Exception as exc:
            logger.warning("PokemonCenterSearch: page search failed: %s", exc)
            return []
        finally:
            if close_after:
                await client.aclose()

    def _parse_next_data_search(self, html: str) -> list[SearchResult]:
        """Extract search results from Next.js __NEXT_DATA__ script tag."""
        match = re.search(
            r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
        )
        if not match:
            return []

        try:
            nd = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []

        results: list[SearchResult] = []

        # Navigate the Next.js data tree — structure varies but common patterns:
        page_props = nd.get("props", {}).get("pageProps", {})

        # Try various keys where search results might live
        products = (
            page_props.get("products")
            or page_props.get("searchResults")
            or page_props.get("results")
            or page_props.get("initialState", {}).get("products")
            or page_props.get("initialState", {}).get("searchResults")
            or page_props.get("data", {}).get("products")
        )

        # Handle nested structures
        if isinstance(products, dict):
            products = (
                products.get("items")
                or products.get("edges")
                or products.get("results")
                or products.get("products")
                or []
            )
            # GraphQL edges pattern: [{node: {...}}, ...]
            if products and isinstance(products[0], dict) and "node" in products[0]:
                products = [p["node"] for p in products]

        if not products or not isinstance(products, list):
            # Try to find product data anywhere in the tree
            products = self._find_products_in_data(nd)

        for item in products or []:
            try:
                result = self._product_to_result(item)
                if result:
                    results.append(result)
            except Exception:
                continue

        return results

    def _find_products_in_data(self, data, depth: int = 0) -> list | None:
        """Recursively search the data tree for an array of product objects."""
        if depth > 6 or not isinstance(data, dict):
            return None

        for key, value in data.items():
            if isinstance(value, list) and len(value) > 0:
                # Check if this looks like a product array
                first = value[0]
                if isinstance(first, dict) and any(
                    k in first for k in ("name", "title", "productName", "slug", "url", "price")
                ):
                    return value
            elif isinstance(value, dict):
                found = self._find_products_in_data(value, depth + 1)
                if found:
                    return found
        return None

    def _parse_jsonld_search(self, html: str) -> list[SearchResult]:
        """Extract products from JSON-LD ItemList or Product entries."""
        results: list[SearchResult] = []

        for match in re.finditer(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
        ):
            try:
                ld = json.loads(match.group(1))
                if isinstance(ld, list):
                    for item in ld:
                        if item.get("@type") == "Product":
                            result = self._ld_product_to_result(item)
                            if result:
                                results.append(result)
                elif isinstance(ld, dict):
                    if ld.get("@type") == "ItemList":
                        for elem in ld.get("itemListElement", []):
                            item = elem.get("item", elem)
                            if item.get("@type") == "Product":
                                result = self._ld_product_to_result(item)
                                if result:
                                    results.append(result)
                    elif ld.get("@type") == "Product":
                        result = self._ld_product_to_result(ld)
                        if result:
                            results.append(result)
            except json.JSONDecodeError:
                continue

        return results

    def _parse_html_search(self, html: str) -> list[SearchResult]:
        """Fallback: parse search results from raw HTML using BeautifulSoup."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return []

        soup = BeautifulSoup(html, "html.parser")
        results: list[SearchResult] = []

        # Look for product cards/links — Pokemon Center uses various class patterns
        product_links = soup.find_all("a", href=re.compile(r"/product/"))

        seen_urls: set[str] = set()
        for link in product_links:
            href = link.get("href", "")
            full_url = urljoin(_BASE, href)
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            # Try to get the product name
            title = ""
            title_el = link.find(class_=re.compile(r"(name|title)", re.I))
            if title_el:
                title = title_el.get_text(strip=True)
            elif link.get_text(strip=True):
                title = link.get_text(strip=True)

            # Try to get price
            price = ""
            card = link.find_parent(class_=re.compile(r"(card|product|item)", re.I))
            if card:
                price_el = card.find(class_=re.compile(r"price", re.I))
                if price_el:
                    price_text = price_el.get_text(strip=True)
                    m = re.search(r"\$[\d,.]+", price_text)
                    if m:
                        price = m.group()
                if not title:
                    title_el = card.find(class_=re.compile(r"(name|title)", re.I))
                    if title_el:
                        title = title_el.get_text(strip=True)

            # Try to get image
            image_url = ""
            img = (card or link).find("img")
            if img:
                image_url = img.get("src") or img.get("data-src") or ""
                if image_url and not image_url.startswith("http"):
                    image_url = urljoin(_BASE, image_url)

            # Extract product ID from URL
            slug = href.rstrip("/").split("/")[-1] if href else ""

            if title or slug:
                results.append(SearchResult(
                    tcin=slug,
                    title=title or slug,
                    price=price,
                    url=full_url,
                    image_url=image_url,
                    availability_status="UNKNOWN",
                    is_purchasable=False,
                    sold_by="Pokemon Center",
                    retailer="pokemoncenter",
                ))

        return results

    # ------------------------------------------------------------------
    # Direct product URL lookup
    # ------------------------------------------------------------------

    async def _lookup_product_url(self, url: str) -> SearchResult | None:
        """Fetch a single product page and extract its details."""
        client = self._get_client()
        close_after = client is not self._external_client

        try:
            resp = await client.get(url, headers=_SEARCH_HEADERS)
            if resp.status_code in (403, 429):
                logger.warning("PokemonCenterSearch: product page returned %d", resp.status_code)
                return None

            resp.raise_for_status()
            html = resp.text

            # Check for bot block
            lower = html.lower()
            if "unusual activity" in lower or "access to this page has been denied" in lower:
                return None

            # Try __NEXT_DATA__
            match = re.search(
                r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
            )
            if match:
                try:
                    nd = json.loads(match.group(1))
                    product = (
                        nd.get("props", {})
                        .get("pageProps", {})
                        .get("product", {})
                    )
                    if product:
                        result = self._product_to_result(product)
                        if result:
                            result.url = url
                            return result
                except json.JSONDecodeError:
                    pass

            # Try JSON-LD
            for m in re.finditer(
                r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
            ):
                try:
                    ld = json.loads(m.group(1))
                    if isinstance(ld, list):
                        ld = next((x for x in ld if x.get("@type") == "Product"), None)
                    if isinstance(ld, dict) and ld.get("@type") == "Product":
                        result = self._ld_product_to_result(ld)
                        if result:
                            result.url = url
                            return result
                except (json.JSONDecodeError, StopIteration):
                    continue

            return None

        except Exception as exc:
            logger.warning("PokemonCenterSearch: product lookup failed: %s", exc)
            return None
        finally:
            if close_after:
                await client.aclose()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        """Return the shared client or create a new one."""
        if self._external_client:
            return self._external_client
        return httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
            timeout=httpx.Timeout(20.0),
            http2=True,
        )

    @staticmethod
    def _product_to_result(item: dict) -> SearchResult | None:
        """Convert a product dict (from API or __NEXT_DATA__) to SearchResult."""
        if not isinstance(item, dict):
            return None

        # Title — try various field names
        title = (
            item.get("name")
            or item.get("title")
            or item.get("productName")
            or item.get("displayName")
            or ""
        )

        # Product ID / slug
        tcin = (
            item.get("id")
            or item.get("productId")
            or item.get("sku")
            or item.get("slug")
            or item.get("productSlug")
            or ""
        )
        tcin = str(tcin)

        # URL
        url = item.get("url") or item.get("href") or item.get("productUrl") or ""
        if not url and item.get("slug"):
            url = f"{_BASE}/product/{item['slug']}"
        if url and not url.startswith("http"):
            url = urljoin(_BASE, url)

        # Price
        price = ""
        price_val = item.get("price") or item.get("salePrice") or item.get("currentPrice")
        if isinstance(price_val, dict):
            price_val = price_val.get("value") or price_val.get("amount") or price_val.get("current")
        if isinstance(price_val, (int, float)):
            price = f"${price_val:.2f}"
        elif isinstance(price_val, str) and price_val:
            price = price_val if price_val.startswith("$") else f"${price_val}"

        # If price is still empty, check nested prices
        if not price:
            prices = item.get("prices") or item.get("priceRange") or {}
            if isinstance(prices, dict):
                for k in ("sale", "current", "regular", "min", "list"):
                    v = prices.get(k)
                    if isinstance(v, (int, float)):
                        price = f"${v:.2f}"
                        break
                    elif isinstance(v, dict):
                        amt = v.get("amount") or v.get("value")
                        if isinstance(amt, (int, float)):
                            price = f"${amt:.2f}"
                            break

        # Image
        image_url = ""
        img = item.get("image") or item.get("imageUrl") or item.get("thumbnailUrl") or item.get("thumbnail")
        if isinstance(img, list) and img:
            img = img[0]
        if isinstance(img, dict):
            image_url = img.get("url") or img.get("src") or ""
        elif isinstance(img, str):
            image_url = img
        # Check images array
        if not image_url:
            images = item.get("images") or []
            if isinstance(images, list) and images:
                first = images[0]
                if isinstance(first, str):
                    image_url = first
                elif isinstance(first, dict):
                    image_url = first.get("url") or first.get("src") or ""

        if image_url and not image_url.startswith("http"):
            image_url = urljoin(_BASE, image_url)

        # Availability
        avail = item.get("availability") or item.get("availabilityStatus") or item.get("stockStatus") or ""
        in_stock = item.get("inStock", item.get("isAvailable", item.get("purchasable")))

        if isinstance(avail, dict):
            avail = avail.get("status") or avail.get("availability") or ""

        avail_str = str(avail).upper()
        if in_stock is True or "INSTOCK" in avail_str.replace("_", "") or "IN_STOCK" in avail_str:
            availability_status = "IN_STOCK"
            is_purchasable = True
        elif in_stock is False or "OUTOFSTOCK" in avail_str.replace("_", "") or "OUT_OF_STOCK" in avail_str or "SOLDOUT" in avail_str:
            availability_status = "OUT_OF_STOCK"
            is_purchasable = False
        elif "PREORDER" in avail_str.replace("_", "") or "PRE_ORDER" in avail_str:
            availability_status = "PRE_ORDER"
            is_purchasable = True
        else:
            availability_status = "UNKNOWN"
            is_purchasable = False

        if not title and not tcin:
            return None

        return SearchResult(
            tcin=tcin,
            title=title,
            price=price,
            url=url,
            image_url=image_url,
            availability_status=availability_status,
            is_purchasable=is_purchasable,
            sold_by="Pokemon Center",
            retailer="pokemoncenter",
        )

    @staticmethod
    def _ld_product_to_result(ld: dict) -> SearchResult | None:
        """Convert a JSON-LD Product to SearchResult."""
        title = ld.get("name", "")
        url = ld.get("url", "")
        if url and not url.startswith("http"):
            url = urljoin(_BASE, url)

        # ID from URL slug
        tcin = url.rstrip("/").split("/")[-1] if url else ld.get("sku", "")

        # Price from offers
        price = ""
        offers = ld.get("offers", {})
        if isinstance(offers, list) and offers:
            offers = offers[0]
        if isinstance(offers, dict):
            p = offers.get("price", "")
            currency = offers.get("priceCurrency", "USD")
            if p:
                price = f"${p}" if currency == "USD" else f"{p} {currency}"

        # Image
        image_url = ""
        img = ld.get("image", "")
        if isinstance(img, list) and img:
            img = img[0]
        if isinstance(img, dict):
            image_url = img.get("url", "")
        elif isinstance(img, str):
            image_url = img

        # Availability
        availability = ""
        if isinstance(offers, dict):
            availability = offers.get("availability", "")

        if "InStock" in availability:
            avail_status = "IN_STOCK"
            is_purchasable = True
        elif "OutOfStock" in availability or "SoldOut" in availability:
            avail_status = "OUT_OF_STOCK"
            is_purchasable = False
        elif "PreOrder" in availability:
            avail_status = "PRE_ORDER"
            is_purchasable = True
        else:
            avail_status = "UNKNOWN"
            is_purchasable = False

        if not title:
            return None

        return SearchResult(
            tcin=tcin,
            title=title,
            price=price,
            url=url,
            image_url=image_url,
            availability_status=avail_status,
            is_purchasable=is_purchasable,
            sold_by="Pokemon Center",
            retailer="pokemoncenter",
        )

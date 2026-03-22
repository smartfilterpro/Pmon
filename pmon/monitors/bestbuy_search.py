"""Best Buy product search via their internal search API.

Uses Best Buy's web search endpoint to find products by keyword,
returning results with SKU, price, availability, and image data.
"""

from __future__ import annotations

import logging
import re

import httpx

from pmon.monitors.base import API_HEADERS
from pmon.monitors.redsky_poller import SearchResult

logger = logging.getLogger(__name__)

# Best Buy's internal search API — same endpoint the website uses.
_SEARCH_URL = "https://www.bestbuy.com/api/tcfb/model.json"

# Headers that mimic a browser navigating Best Buy's search page.
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

    @staticmethod
    def _extract_sku(text: str) -> str | None:
        """Try to extract a Best Buy SKU from a URL or raw number.

        Best Buy URLs:
          /site/product-name/1234567.p
          /product/product-name/JJG2TLCK6H  (BSIN — not a numeric SKU)
        """
        # Old format: 7-8 digit SKU
        match = re.search(r"/(\d{7,8})\.p", text)
        if match:
            return match.group(1)
        # Raw SKU (just digits, 7-8 chars)
        stripped = text.strip()
        if re.fullmatch(r"\d{7,8}", stripped):
            return stripped
        return None

    async def lookup_sku(self, sku: str) -> SearchResult | None:
        """Look up a single product by SKU via the priceBlocks API."""
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
        Uses the Falcor model.json API first, then falls back to the
        typeahead/suggest API if no results are found.
        """
        # Direct SKU lookup
        sku = self._extract_sku(keyword)
        if sku:
            result = await self.lookup_sku(sku)
            if result:
                return [result]
            return []

        async with httpx.AsyncClient(
            headers=_BB_HEADERS,
            follow_redirects=True,
            timeout=httpx.Timeout(15.0),
            http2=True,
        ) as client:
            page = (offset // self.max_results) + 1

            # Try primary search (Falcor model.json)
            results = await self._search_model_json(client, keyword, include_out_of_stock, page)

            # Fallback to typeahead/suggest API (no pagination support)
            if not results and page == 1:
                results = await self._search_typeahead(client, keyword, include_out_of_stock)

            return results

    async def _search_model_json(
        self, client: httpx.AsyncClient, keyword: str, include_out_of_stock: bool, page: int = 1
    ) -> list[SearchResult]:
        """Primary search via Best Buy's Falcor model.json API."""
        params = {
            "paths": f'[["search","query","{keyword}","1",{{"page":"{page}","pageSize":"{self.max_results}"}},[["sku","names","short"],["sku","names","long"],["sku","skuId"],["sku","image"],["sku","url"],["sku","buttonState","buttonState"],["sku","price","currentPrice"],["sku","price","regularPrice"],["sku","condition"],["sku","salePrice"]]]]',
            "method": "get",
        }

        try:
            resp = await client.get(_SEARCH_URL, params=params)
        except httpx.HTTPError as exc:
            logger.warning("BestBuySearch: model.json network error: %s", exc)
            return []

        if resp.status_code == 403:
            logger.warning("BestBuySearch: model.json 403 — blocked")
            return []

        if resp.status_code != 200:
            logger.warning("BestBuySearch: model.json HTTP %d for '%s'", resp.status_code, keyword)
            return []

        return self._parse_search(resp.json(), include_out_of_stock)

    async def _search_typeahead(
        self, client: httpx.AsyncClient, keyword: str, include_out_of_stock: bool
    ) -> list[SearchResult]:
        """Fallback search via Best Buy's typeahead/suggest API.

        This endpoint is lighter-weight and less likely to be blocked.
        It returns product suggestions that we then enrich via priceBlocks.
        """
        try:
            resp = await client.get(
                "https://www.bestbuy.com/api/tcfb/model.json",
                params={
                    "paths": f'[["search","typeAhead","{keyword}",["products"]]]',
                    "method": "get",
                },
            )
            if resp.status_code != 200:
                logger.debug("BestBuySearch: typeahead HTTP %d", resp.status_code)
                return []

            data = resp.json()
            return self._parse_typeahead(data, include_out_of_stock)
        except Exception as exc:
            logger.debug("BestBuySearch: typeahead failed: %s", exc)
            return []

    def _parse_typeahead(self, data: dict, include_out_of_stock: bool) -> list[SearchResult]:
        """Parse typeahead/suggestion response from Best Buy."""
        results: list[SearchResult] = []
        try:
            json_graph = data.get("jsonGraph", {})
            search_data = json_graph.get("search", {}).get("typeAhead", {})

            for kw_key, kw_data in search_data.items():
                products = kw_data.get("products", {})
                if not isinstance(products, dict):
                    continue

                # Products may be indexed numerically
                for idx in sorted(products.keys(), key=lambda x: int(x) if x.isdigit() else 999):
                    if len(results) >= self.max_results:
                        break

                    item = products[idx]
                    if isinstance(item, dict) and "value" in item:
                        item = item["value"]
                    if not isinstance(item, dict):
                        continue

                    sku = str(item.get("sku", item.get("skuId", "")))
                    if not sku:
                        continue

                    title = item.get("name", item.get("title", ""))
                    image_url = item.get("image", item.get("thumbnailImage", ""))
                    url = item.get("url", "")
                    if isinstance(url, str) and url.startswith("/"):
                        url = f"https://www.bestbuy.com{url}"
                    elif not url:
                        url = f"https://www.bestbuy.com/site/-/{sku}.p"

                    price_val = item.get("salePrice", item.get("currentPrice", item.get("regularPrice", "")))
                    price = f"${price_val}" if price_val else ""

                    avail_status = "IN_STOCK"
                    is_purchasable = True

                    if not include_out_of_stock:
                        online = item.get("onlineAvailability")
                        if online is False:
                            continue

                    results.append(SearchResult(
                        tcin=sku,
                        title=title,
                        price=price,
                        url=url,
                        image_url=image_url,
                        availability_status=avail_status,
                        is_purchasable=is_purchasable,
                        sold_by="Best Buy",
                        retailer="bestbuy",
                    ))

                break  # first keyword only
        except (AttributeError, TypeError, KeyError) as e:
            logger.debug("BestBuySearch: typeahead parse error: %s", e)

        if results:
            logger.debug("BestBuySearch: typeahead found %d products", len(results))
        return results

    def _parse_search(self, data: dict, include_out_of_stock: bool) -> list[SearchResult]:
        """Extract products from Best Buy's JSONP/model response."""
        results: list[SearchResult] = []

        try:
            # Best Buy's model.json returns a Falcor-style JSON graph.
            # Navigate: jsonGraph.search.query.<keyword>.<index>.skus.<n>.value
            json_graph = data.get("jsonGraph", {})
            search_data = json_graph.get("search", {}).get("query", {})

            # Find the first keyword entry
            for keyword_key, keyword_data in search_data.items():
                index_data = keyword_data.get("1", {})
                if not isinstance(index_data, dict):
                    continue

                skus_ref = index_data.get("skus", {})
                if not isinstance(skus_ref, dict):
                    continue

                # Get the SKU data from the graph
                sku_graph = json_graph.get("sku", {})

                for idx in sorted(skus_ref.keys(), key=lambda x: int(x) if x.isdigit() else 999):
                    if len(results) >= self.max_results:
                        break

                    ref = skus_ref.get(idx, {})
                    if not isinstance(ref, dict):
                        continue

                    # Follow the $ref path or get value directly
                    sku_ref = ref.get("value")
                    if isinstance(sku_ref, dict) and "$ref" in sku_ref:
                        # Falcor reference — resolve from graph
                        ref_path = sku_ref["$ref"]
                        sku_id = ref_path[-1] if isinstance(ref_path, list) else str(sku_ref)
                    elif isinstance(sku_ref, list):
                        sku_id = str(sku_ref[-1])
                    else:
                        sku_id = str(idx)

                    sku_entry = sku_graph.get(sku_id, sku_graph.get(str(sku_id), {}))
                    if not sku_entry:
                        continue

                    result = self._parse_sku_entry(sku_id, sku_entry)
                    if result:
                        if not include_out_of_stock and result.availability_status == "OUT_OF_STOCK":
                            continue
                        results.append(result)

                break  # only process first keyword match

        except (AttributeError, TypeError, KeyError) as e:
            logger.debug("BestBuySearch: parse error: %s", e)

        # Fallback: try flat list format if Falcor graph didn't work
        if not results:
            results = self._parse_flat_response(data, include_out_of_stock)

        logger.debug("BestBuySearch: found %d products for keyword query", len(results))
        return results

    def _parse_sku_entry(self, sku_id: str, entry: dict) -> SearchResult | None:
        """Parse a single SKU entry from the JSON graph."""
        try:
            def _val(d):
                """Extract .value from Falcor atom or return dict."""
                if isinstance(d, dict) and "value" in d:
                    return d["value"]
                return d

            names = entry.get("names", {})
            title = _val(names.get("short", {})) or _val(names.get("long", {})) or ""
            if not isinstance(title, str):
                title = ""

            sku_id_val = _val(entry.get("skuId", {})) or sku_id
            if not isinstance(sku_id_val, str):
                sku_id_val = str(sku_id_val)

            image_url = _val(entry.get("image", {})) or ""
            if not isinstance(image_url, str):
                image_url = ""

            url_path = _val(entry.get("url", {})) or ""
            if isinstance(url_path, str) and url_path:
                url = f"https://www.bestbuy.com{url_path}" if url_path.startswith("/") else url_path
            else:
                url = f"https://www.bestbuy.com/site/-/{sku_id_val}.p"

            price_info = entry.get("price", {})
            current_price = _val(price_info.get("currentPrice", {}))
            price = f"${current_price}" if current_price else ""

            button_state_data = entry.get("buttonState", {})
            button_state = _val(button_state_data.get("buttonState", {})) or ""

            if button_state == "ADD_TO_CART":
                avail_status = "IN_STOCK"
                is_purchasable = True
            elif button_state == "PRE_ORDER":
                avail_status = "PRE_ORDER"
                is_purchasable = True
            elif button_state == "COMING_SOON":
                avail_status = "COMING_SOON"
                is_purchasable = False
            else:
                avail_status = "OUT_OF_STOCK"
                is_purchasable = False

            return SearchResult(
                tcin=sku_id_val,
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
            logger.debug("BestBuySearch: failed to parse SKU entry %s", sku_id, exc_info=True)
            return None

    def _parse_flat_response(self, data: dict, include_out_of_stock: bool) -> list[SearchResult]:
        """Fallback parser for simpler Best Buy response formats.

        Best Buy sometimes returns a simpler list-based format instead of
        the Falcor JSON graph, especially for autocomplete/typeahead results.
        """
        results: list[SearchResult] = []

        # Try to find product list in common response locations
        products = []
        if isinstance(data, dict):
            for key in ("products", "results", "items", "skus"):
                if key in data and isinstance(data[key], list):
                    products = data[key]
                    break
            # Nested under data or searchResult
            if not products:
                for outer in ("data", "searchResult", "resultsData"):
                    inner = data.get(outer, {})
                    if isinstance(inner, dict):
                        for key in ("products", "results", "items", "skus"):
                            if key in inner and isinstance(inner[key], list):
                                products = inner[key]
                                break
                    if products:
                        break

        for item in products[:self.max_results]:
            try:
                sku = str(item.get("sku", item.get("skuId", item.get("sku_id", ""))))
                if not sku:
                    continue

                title = item.get("name", item.get("title", item.get("shortName", "")))
                price_val = item.get("salePrice", item.get("currentPrice", item.get("regularPrice", "")))
                price = f"${price_val}" if price_val else ""

                image_url = item.get("image", item.get("thumbnailImage", item.get("largeFrontImage", "")))
                url = item.get("url", item.get("productUrl", f"https://www.bestbuy.com/site/-/{sku}.p"))
                if isinstance(url, str) and url.startswith("/"):
                    url = f"https://www.bestbuy.com{url}"

                in_stock = item.get("inStoreAvailability", False) or item.get("onlineAvailability", False)
                avail_status = "IN_STOCK" if in_stock else "OUT_OF_STOCK"
                is_purchasable = in_stock

                if not include_out_of_stock and avail_status == "OUT_OF_STOCK":
                    continue

                results.append(SearchResult(
                    tcin=sku,
                    title=title,
                    price=price,
                    url=url,
                    image_url=image_url,
                    availability_status=avail_status,
                    is_purchasable=is_purchasable,
                    sold_by="Best Buy",
                    retailer="bestbuy",
                ))
            except Exception:
                logger.debug("BestBuySearch: skipping unparseable item", exc_info=True)
                continue

        return results

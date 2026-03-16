"""API-based checkout - uses direct HTTP calls instead of browser automation."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import urljoin, urlparse

import httpx

from pmon.config import AccountCredentials, Profile
from pmon.models import CheckoutResult, CheckoutStatus

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


class ApiCheckout:
    """Attempts checkout via direct API calls. Faster and works headlessly."""

    def __init__(self):
        self._clients: dict[str, httpx.AsyncClient] = {}
        # Stored session cookies loaded from database (set by engine before calling)
        self._session_cookies: dict[str, dict] = {}  # retailer -> {name: value}

    def load_session_cookies(self, retailer: str, cookies: dict):
        """Load pre-authenticated session cookies for a retailer."""
        self._session_cookies[retailer] = cookies

    def _get_client(self, retailer: str) -> httpx.AsyncClient:
        if retailer not in self._clients or self._clients[retailer].is_closed:
            client = httpx.AsyncClient(
                headers=HEADERS,
                follow_redirects=True,
                timeout=httpx.Timeout(20.0),
            )
            # Apply stored session cookies if available
            if retailer in self._session_cookies:
                for name, value in self._session_cookies[retailer].items():
                    client.cookies.set(name, value)
            self._clients[retailer] = client
        return self._clients[retailer]

    def reset_client(self, retailer: str):
        """Force re-creation of client (e.g. after loading new cookies)."""
        if retailer in self._clients and not self._clients[retailer].is_closed:
            asyncio.get_event_loop().create_task(self._clients[retailer].aclose())
        self._clients.pop(retailer, None)

    async def attempt(
        self,
        url: str,
        retailer: str,
        product_name: str,
        profile: Profile,
        creds: AccountCredentials,
    ) -> CheckoutResult:
        handler = getattr(self, f"_checkout_{retailer}", None)
        if not handler:
            return CheckoutResult(
                url=url,
                retailer=retailer,
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=f"No API checkout for {retailer} — needs browser fallback",
            )
        try:
            return await handler(url, product_name, profile, creds)
        except Exception as e:
            logger.error(f"API checkout failed for {product_name}: {e}")
            return CheckoutResult(
                url=url,
                retailer=retailer,
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=str(e),
            )

    async def _checkout_target(
        self, url: str, product_name: str, profile: Profile, creds: AccountCredentials
    ) -> CheckoutResult:
        """Target API checkout flow."""
        client = self._get_client("target")

        # Extract TCIN from URL
        match = re.search(r"A-(\d+)", url)
        if not match:
            return CheckoutResult(
                url=url, retailer="target", product_name=product_name,
                status=CheckoutStatus.FAILED, error_message="Could not extract TCIN",
            )
        tcin = match.group(1)

        # Step 1: Load login page for session cookies
        await client.get("https://www.target.com/login")

        # Step 2: Authenticate
        auth_resp = await client.post(
            "https://login.target.com/gsp/static/v1/login/token",
            json={
                "username": creds.email,
                "password": creds.password,
                "device_info": {"type": "WEB"},
            },
            headers={
                **HEADERS,
                "Content-Type": "application/json",
                "Origin": "https://www.target.com",
                "Referer": "https://www.target.com/login",
            },
        )

        if auth_resp.status_code not in (200, 201):
            return CheckoutResult(
                url=url, retailer="target", product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=f"Auth failed (HTTP {auth_resp.status_code}): {auth_resp.text[:200]}",
            )

        # Step 2: Add to cart
        cart_resp = await client.post(
            "https://carts.target.com/web_checkouts/v1/cart_items",
            json={
                "cart_type": "REGULAR",
                "channel_id": 10,
                "shopping_context": "DIGITAL",
                "cart_item": {
                    "tcin": tcin,
                    "quantity": 1,
                    "item_channel_id": 10,
                },
            },
            headers={**HEADERS, "Content-Type": "application/json"},
        )

        if cart_resp.status_code not in (200, 201):
            return CheckoutResult(
                url=url, retailer="target", product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=f"Add to cart failed (HTTP {cart_resp.status_code})",
            )

        # Step 3: Initiate checkout
        checkout_resp = await client.post(
            "https://carts.target.com/web_checkouts/v1/checkout",
            json={"cart_type": "REGULAR"},
            headers={**HEADERS, "Content-Type": "application/json"},
        )

        if checkout_resp.status_code in (200, 201):
            return CheckoutResult(
                url=url, retailer="target", product_name=product_name,
                status=CheckoutStatus.SUCCESS,
                error_message="Checkout initiated — check your Target account for order confirmation",
            )

        return CheckoutResult(
            url=url, retailer="target", product_name=product_name,
            status=CheckoutStatus.FAILED,
            error_message=f"Checkout API returned HTTP {checkout_resp.status_code}",
        )

    async def _checkout_walmart(
        self, url: str, product_name: str, profile: Profile, creds: AccountCredentials
    ) -> CheckoutResult:
        """Walmart API checkout flow."""
        client = self._get_client("walmart")

        # Extract product ID from URL
        match = re.search(r"/ip/[^/]*/(\d+)", url) or re.search(r"/ip/(\d+)", url)
        if not match:
            return CheckoutResult(
                url=url, retailer="walmart", product_name=product_name,
                status=CheckoutStatus.FAILED, error_message="Could not extract product ID from URL",
            )
        product_id = match.group(1)

        # Step 1: Load login page for session cookies + CSRF
        page = await client.get("https://www.walmart.com/account/login")
        csrf = ""
        for cookie_name, cookie_val in client.cookies.items():
            if "csrf" in cookie_name.lower() or cookie_name == "CSRF-TOKEN":
                csrf = cookie_val
                break
        if not csrf:
            csrf_match = re.search(r'"csrfToken"\s*:\s*"([^"]+)"', page.text)
            if csrf_match:
                csrf = csrf_match.group(1)

        login_headers = {
            **HEADERS,
            "Content-Type": "application/json",
            "Origin": "https://www.walmart.com",
            "Referer": "https://www.walmart.com/account/login",
        }
        if csrf:
            login_headers["x-csrf-token"] = csrf
            login_headers["WM_SEC.AUTH_TOKEN"] = csrf

        # Step 2: Sign in
        login_resp = await client.post(
            "https://www.walmart.com/account/electrode/api/signin",
            json={"username": creds.email, "password": creds.password},
            headers=login_headers,
        )

        if login_resp.status_code != 200:
            return CheckoutResult(
                url=url, retailer="walmart", product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=f"Login failed (HTTP {login_resp.status_code}): {login_resp.text[:200]}",
            )

        # Step 2: Add to cart
        cart_resp = await client.post(
            "https://www.walmart.com/api/v1/cart/items",
            json={
                "items": [{
                    "offerId": product_id,
                    "quantity": 1,
                }],
            },
            headers={**HEADERS, "Content-Type": "application/json"},
        )

        if cart_resp.status_code not in (200, 201):
            return CheckoutResult(
                url=url, retailer="walmart", product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=f"Add to cart failed (HTTP {cart_resp.status_code})",
            )

        # Step 3: Proceed to checkout
        checkout_resp = await client.post(
            "https://www.walmart.com/api/checkout/v1/contract",
            json={"cart_type": "REGULAR"},
            headers={**HEADERS, "Content-Type": "application/json"},
        )

        if checkout_resp.status_code in (200, 201):
            return CheckoutResult(
                url=url, retailer="walmart", product_name=product_name,
                status=CheckoutStatus.SUCCESS,
                error_message="Checkout initiated — check Walmart account for confirmation",
            )

        return CheckoutResult(
            url=url, retailer="walmart", product_name=product_name,
            status=CheckoutStatus.FAILED,
            error_message=f"Checkout API returned HTTP {checkout_resp.status_code}",
        )

    async def _checkout_pokemoncenter(
        self, url: str, product_name: str, profile: Profile, creds: AccountCredentials
    ) -> CheckoutResult:
        """Pokemon Center API checkout flow.

        Uses pre-authenticated session cookies (imported by user via dashboard).
        Falls back to SSO login if no session is available.

        PKC API pattern:
        - Product data: embedded JSON-LD in product pages, or /api/products/{slug}
        - Cart: POST to cart API with product variant ID
        - Checkout: standard Shopify-like checkout flow
        """
        client = self._get_client("pokemoncenter")
        retailer = "pokemoncenter"

        # Check if we have session cookies loaded
        has_session = bool(self._session_cookies.get("pokemoncenter"))
        if not has_session:
            logger.warning("Pokemon Center: no session cookies — attempting SSO login")
            login_ok = await self._pkc_sso_login(client, creds)
            if not login_ok:
                return CheckoutResult(
                    url=url, retailer=retailer, product_name=product_name,
                    status=CheckoutStatus.FAILED,
                    error_message=(
                        "Pokemon Center login failed. Import session cookies via "
                        "Dashboard > Accounts > Pokemon Center > Import Cookies"
                    ),
                )

        # Step 1: Load product page and extract variant/product ID
        product_id = await self._pkc_extract_product_id(client, url)
        if not product_id:
            return CheckoutResult(
                url=url, retailer=retailer, product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message="Could not extract product ID from Pokemon Center page",
            )

        # Step 2: Add to cart
        cart_ok = await self._pkc_add_to_cart(client, product_id)
        if not cart_ok:
            return CheckoutResult(
                url=url, retailer=retailer, product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message="Failed to add item to Pokemon Center cart",
            )

        # Step 3: Initiate checkout
        checkout_ok = await self._pkc_checkout(client)
        if checkout_ok:
            return CheckoutResult(
                url=url, retailer=retailer, product_name=product_name,
                status=CheckoutStatus.SUCCESS,
                error_message="Checkout initiated — check your Pokemon Center account for confirmation",
            )

        return CheckoutResult(
            url=url, retailer=retailer, product_name=product_name,
            status=CheckoutStatus.FAILED,
            error_message="Pokemon Center checkout API call failed",
        )

    async def _pkc_sso_login(self, client: httpx.AsyncClient, creds: AccountCredentials) -> bool:
        """Attempt Pokemon SSO login via access.pokemon.com."""
        if not creds.email or not creds.password:
            return False

        try:
            # Step 1: Initialize SSO flow
            resp = await client.get(
                "https://www.pokemoncenter.com/account/login",
                headers={**HEADERS, "Accept": "text/html,*/*"},
            )

            # Check for block page
            if resp.status_code == 403 or "unusual activity" in resp.text.lower():
                logger.warning("Pokemon Center: IP blocked during login attempt")
                return False

            # Step 2: Follow SSO redirect to access.pokemon.com
            # The login page typically redirects to Pokemon's SSO
            sso_url = None
            if "access.pokemon.com" in resp.text or "sso.pokemon.com" in resp.text:
                # Extract SSO URL from page
                sso_match = re.search(r'(https://(?:access|sso)\.pokemon\.com[^"\'>\s]+)', resp.text)
                if sso_match:
                    sso_url = sso_match.group(1)

            if sso_url:
                sso_resp = await client.get(sso_url)
                # Extract CSRF/auth tokens from SSO page
                csrf_match = re.search(r'name="csrf[_-]?token"[^>]*value="([^"]+)"', sso_resp.text, re.I)
                csrf = csrf_match.group(1) if csrf_match else ""

                # Submit credentials to SSO
                login_data = {
                    "email": creds.email,
                    "password": creds.password,
                }
                if csrf:
                    login_data["csrf_token"] = csrf

                login_resp = await client.post(
                    sso_url,
                    data=login_data,
                    headers={
                        **HEADERS,
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Origin": "https://access.pokemon.com",
                        "Referer": sso_url,
                    },
                )

                # Check for successful redirect back to pokemoncenter.com
                if login_resp.status_code in (200, 302) and "error" not in login_resp.text.lower()[:500]:
                    logger.info("Pokemon Center: SSO login appears successful")
                    return True

            logger.warning("Pokemon Center: SSO login flow did not complete")
            return False

        except Exception as exc:
            logger.error("Pokemon Center SSO login error: %s", exc)
            return False

    async def _pkc_extract_product_id(self, client: httpx.AsyncClient, url: str) -> str | None:
        """Extract product/variant ID from a Pokemon Center product page."""
        try:
            resp = await client.get(url, headers={**HEADERS, "Accept": "text/html,*/*"})
            if resp.status_code == 403:
                logger.warning("Pokemon Center: blocked when loading product page")
                return None

            html = resp.text

            # Strategy 1: Look for product ID in JSON-LD structured data
            ld_match = re.search(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)
            if ld_match:
                try:
                    ld_data = json.loads(ld_match.group(1))
                    if isinstance(ld_data, dict):
                        sku = ld_data.get("sku") or ld_data.get("productID")
                        if sku:
                            return str(sku)
                        # Check offers for SKU
                        offers = ld_data.get("offers", {})
                        if isinstance(offers, dict):
                            sku = offers.get("sku")
                            if sku:
                                return str(sku)
                        elif isinstance(offers, list) and offers:
                            sku = offers[0].get("sku")
                            if sku:
                                return str(sku)
                except json.JSONDecodeError:
                    pass

            # Strategy 2: Look for product data in embedded JS state
            # PKC often embeds product data in __NEXT_DATA__ or similar
            next_data = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
            if next_data:
                try:
                    nd = json.loads(next_data.group(1))
                    # Navigate common Next.js data paths
                    props = nd.get("props", {}).get("pageProps", {})
                    product = props.get("product", {})
                    # Try various ID fields
                    for field in ("id", "productId", "sku", "variantId", "selectedVariant"):
                        val = product.get(field)
                        if val:
                            return str(val)
                    # Check variants array
                    variants = product.get("variants", [])
                    if variants and isinstance(variants, list):
                        return str(variants[0].get("id") or variants[0].get("sku", ""))
                except (json.JSONDecodeError, AttributeError):
                    pass

            # Strategy 3: Look for add-to-cart form data
            atc_match = re.search(r'data-product-id="([^"]+)"', html)
            if atc_match:
                return atc_match.group(1)

            # Strategy 4: data attributes on buttons
            variant_match = re.search(r'data-variant-id="([^"]+)"', html)
            if variant_match:
                return variant_match.group(1)

            # Strategy 5: Extract from URL slug and try API
            slug = urlparse(url).path.rstrip("/").split("/")[-1]
            if slug:
                # Try fetching product data via common API patterns
                for api_path in [
                    f"https://www.pokemoncenter.com/api/products/{slug}",
                    f"https://www.pokemoncenter.com/api/product/{slug}",
                ]:
                    try:
                        api_resp = await client.get(api_path, headers={**HEADERS, "Accept": "application/json"})
                        if api_resp.status_code == 200:
                            data = api_resp.json()
                            pid = data.get("id") or data.get("productId") or data.get("sku")
                            if pid:
                                return str(pid)
                    except Exception:
                        continue

            logger.warning("Pokemon Center: could not extract product ID from %s", url)
            return None

        except Exception as exc:
            logger.error("Pokemon Center product ID extraction error: %s", exc)
            return None

    async def _pkc_add_to_cart(self, client: httpx.AsyncClient, product_id: str) -> bool:
        """Add a product to the Pokemon Center cart via API."""
        # Try multiple known API patterns for PKC cart
        cart_endpoints = [
            ("https://www.pokemoncenter.com/api/cart/add", {
                "id": product_id,
                "quantity": 1,
            }),
            ("https://www.pokemoncenter.com/api/cart", {
                "items": [{"id": product_id, "quantity": 1}],
            }),
            ("https://www.pokemoncenter.com/cart/add.js", {
                "id": product_id,
                "quantity": 1,
            }),
        ]

        for endpoint, payload in cart_endpoints:
            try:
                resp = await client.post(
                    endpoint,
                    json=payload,
                    headers={
                        **HEADERS,
                        "Content-Type": "application/json",
                        "Origin": "https://www.pokemoncenter.com",
                        "Referer": "https://www.pokemoncenter.com/",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                )
                if resp.status_code in (200, 201):
                    logger.info("Pokemon Center: added to cart via %s", endpoint)
                    return True
                elif resp.status_code == 403:
                    logger.warning("Pokemon Center: blocked (403) on cart add — session may be expired")
                    return False
            except Exception as exc:
                logger.debug("Pokemon Center cart endpoint %s failed: %s", endpoint, exc)
                continue

        logger.warning("Pokemon Center: all cart add endpoints failed for product %s", product_id)
        return False

    async def _pkc_checkout(self, client: httpx.AsyncClient) -> bool:
        """Initiate checkout on Pokemon Center."""
        checkout_endpoints = [
            "https://www.pokemoncenter.com/api/checkout",
            "https://www.pokemoncenter.com/checkout",
        ]

        for endpoint in checkout_endpoints:
            try:
                resp = await client.post(
                    endpoint,
                    json={},
                    headers={
                        **HEADERS,
                        "Content-Type": "application/json",
                        "Origin": "https://www.pokemoncenter.com",
                        "Referer": "https://www.pokemoncenter.com/cart",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                )
                if resp.status_code in (200, 201, 302):
                    logger.info("Pokemon Center: checkout initiated via %s", endpoint)
                    return True
            except Exception as exc:
                logger.debug("Pokemon Center checkout endpoint %s failed: %s", endpoint, exc)
                continue

        # Try GET-based checkout (some e-commerce platforms use this)
        try:
            resp = await client.get(
                "https://www.pokemoncenter.com/checkout",
                headers={**HEADERS, "Accept": "text/html,*/*"},
            )
            if resp.status_code == 200 and "checkout" in resp.text.lower():
                logger.info("Pokemon Center: checkout page loaded successfully")
                return True
        except Exception:
            pass

        return False

    async def close(self):
        for client in self._clients.values():
            if not client.is_closed:
                await client.aclose()

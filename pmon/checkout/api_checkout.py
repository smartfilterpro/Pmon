"""API-based checkout - uses direct HTTP calls instead of browser automation.

AUDIT FINDINGS (2026-03-17):
=============================================================================
This file implements direct HTTP API-based checkout for Target and Walmart.
The Target API checkout (_checkout_target) is the "fast path" — if it works,
no browser is needed. Key issues:

1. GSP LOGIN UNRELIABLE: _tgt_gsp_login() attempts Target's OAuth flow via
   direct HTTP calls. This almost always fails because PerimeterX blocks
   API-level login attempts. The code correctly falls back to session cookie
   import, but the login code is effectively dead weight.

2. UNDOCUMENTED API ENDPOINTS: The Target checkout endpoints
   (carts.target.com/web_checkouts/v1/*) are undocumented and reverse-
   engineered. They may have changed since implementation. The code tries
   multiple endpoint variants (PUT /checkout, POST /checkout, POST
   /checkout/place_order, POST /place_order) as a brute-force approach.
   This is fragile but reasonable given no official API exists.

3. FULFILLMENT TYPE GUESSING: _tgt_add_to_cart() tries "DIGITAL" then "STS"
   fulfillment types. "DIGITAL" maps to "SHIPT" (shipping) and "STS" maps
   to "PICKUP". The fulfillment type naming is confusing and may not match
   current Target API expectations.

4. ADDRESS ENDPOINT SHOTGUNNING: _tgt_set_shipping_address() tries 4
   different endpoint patterns to set the shipping address. This logs
   heavily but may mask the real failure. If ALL 4 fail, it returns True
   anyway hoping Target auto-applies the default address.

5. NO PRICE GUARD: Same as browser flow — no max_price check before
   placing the order.

6. SESSION VALIDATION IS GOOD: _tgt_validate_session() checks both profile
   and cart endpoints, which is a solid approach.

7. SESSION COOKIE IMPORT IS THE REAL AUTH: The most reliable auth method is
   importing cookies from a real browser session via the dashboard. This
   pattern should be preserved and emphasized in the rewrite.
=============================================================================
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import urljoin, urlparse

import httpx

from pmon.config import AccountCredentials, Profile
from pmon.models import CheckoutResult, CheckoutStatus
from pmon.monitors.base import _CHROME_FULL, _CHROME_MAJOR

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{_CHROME_FULL} Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Sec-Ch-Ua": f'"Chromium";v="{_CHROME_MAJOR}", "Google Chrome";v="{_CHROME_MAJOR}", "Not-A.Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
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
                http2=True,
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

    # Target API constants (from network analysis)
    _TGT_API_KEY = "e59ce3b531b2c39afb2e2b8a71ff10113aac2a14"
    _TGT_CLIENT_ID = "ecom-web-1.0.0"

    async def _checkout_target(
        self, url: str, product_name: str, profile: Profile, creds: AccountCredentials
    ) -> CheckoutResult:
        """Target API checkout flow.

        Uses real Target API endpoints discovered from network analysis:
        - Auth: OAuth code grant via gsp.target.com (multi-step: email → method → password)
        - Product: redsky.target.com/redsky_aggregations/v1/web/
        - Cart: carts.target.com/web_checkouts/v1/cart
        - Profile: api.target.com/guest_profile_details/v1/

        Session cookies are the most reliable auth method since Target uses
        PerimeterX bot protection on login pages. Import cookies via Dashboard.
        """
        client = self._get_client("target")
        retailer = "target"

        # Extract TCIN from URL (Target product ID)
        # Formats: /A-12345678, /-/A-12345678, ?preselect=12345678
        match = re.search(r"A-(\d+)", url)
        if not match:
            match = re.search(r"preselect=(\d+)", url)
        if not match:
            return CheckoutResult(
                url=url, retailer=retailer, product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message="Could not extract TCIN from Target URL",
            )
        tcin = match.group(1)

        # Check for session cookies — most reliable auth method
        has_session = bool(self._session_cookies.get("target"))
        if not has_session:
            logger.warning("Target: no session cookies — attempting GSP OAuth login")
            login_ok = await self._tgt_gsp_login(client, creds)
            if not login_ok:
                return CheckoutResult(
                    url=url, retailer=retailer, product_name=product_name,
                    status=CheckoutStatus.FAILED,
                    error_message=(
                        "Target login failed (PerimeterX may be blocking). "
                        "Import session cookies via Dashboard > Accounts > Target > Import Cookies"
                    ),
                )

        # Step 1: Validate session by checking cart or profile
        session_valid = await self._tgt_validate_session(client)
        if not session_valid:
            return CheckoutResult(
                url=url, retailer=retailer, product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message="Target session expired — re-import cookies via Dashboard",
            )

        # Step 2: Look up product via Redsky API to verify availability
        product_info = await self._tgt_lookup_product(client, tcin)
        if product_info and not product_info.get("available", True):
            return CheckoutResult(
                url=url, retailer=retailer, product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message="Product is out of stock on Target",
            )

        # Step 3: Add to cart via carts.target.com
        cart_ok = await self._tgt_add_to_cart(client, tcin)
        if not cart_ok:
            return CheckoutResult(
                url=url, retailer=retailer, product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message="Failed to add item to Target cart",
            )

        # Step 4: Try to set shipping address on the cart
        # (may fail if endpoints are wrong — we continue anyway since Target
        # may auto-apply the account's default address during checkout)
        await self._tgt_set_shipping_address(client, profile)

        # Step 5: Initiate checkout (pass CVV for payment verification)
        checkout_ok = await self._tgt_checkout(client, creds)
        if checkout_ok:
            return CheckoutResult(
                url=url, retailer=retailer, product_name=product_name,
                status=CheckoutStatus.SUCCESS,
                error_message="Checkout initiated — check your Target account for order confirmation",
            )

        return CheckoutResult(
            url=url, retailer=retailer, product_name=product_name,
            status=CheckoutStatus.FAILED,
            error_message="Target checkout API call failed",
        )

    async def _tgt_gsp_login(self, client: httpx.AsyncClient, creds: AccountCredentials) -> bool:
        """Attempt Target OAuth login via GSP.

        Target login is a multi-step OAuth code grant:
        1. Load login page (sets PerimeterX cookies, session cookies)
        2. POST email to get auth methods
        3. POST password to authenticate
        4. Receive OAuth code redirect → validate token

        This often fails due to PerimeterX. Session cookie import is preferred.
        """
        if not creds.email or not creds.password:
            return False

        try:
            # Step 1: Load login page to get initial cookies
            login_url = (
                f"https://www.target.com/login?"
                f"client_id={self._TGT_CLIENT_ID}"
                f"&ui_namespace=ui-default"
                f"&back_button_action=browser"
                f"&keep_me_signed_in=true"
                f"&actions=create_session_signin"
            )
            resp = await client.get(login_url, headers={**HEADERS, "Accept": "text/html,*/*"})

            if resp.status_code == 403:
                logger.warning("Target: PerimeterX blocked login page")
                return False

            # Step 2: Submit email via GSP auth endpoint
            auth_resp = await client.post(
                f"https://gsp.target.com/gsp/authentications/v1/auth_codes"
                f"?client_id={self._TGT_CLIENT_ID}",
                json={
                    "username": creds.email,
                    "credential_type_code": "email",
                    "keep_me_signed_in": True,
                },
                headers={
                    **HEADERS,
                    "Content-Type": "application/json",
                    "Origin": "https://www.target.com",
                    "Referer": login_url,
                },
            )

            if auth_resp.status_code not in (200, 201, 202):
                logger.warning("Target: GSP email step failed (HTTP %d)", auth_resp.status_code)
                return False

            # Step 3: Submit password
            pwd_resp = await client.post(
                f"https://gsp.target.com/gsp/authentications/v1/auth_codes"
                f"?client_id={self._TGT_CLIENT_ID}",
                json={
                    "username": creds.email,
                    "password": creds.password,
                    "credential_type_code": "password",
                    "keep_me_signed_in": True,
                },
                headers={
                    **HEADERS,
                    "Content-Type": "application/json",
                    "Origin": "https://www.target.com",
                    "Referer": login_url,
                },
            )

            if pwd_resp.status_code not in (200, 201):
                logger.warning("Target: GSP password step failed (HTTP %d)", pwd_resp.status_code)
                return False

            # Extract auth code from response
            try:
                auth_data = pwd_resp.json()
                code = auth_data.get("code") or auth_data.get("auth_code")
            except Exception:
                code = None

            if not code:
                # Check if redirect happened with code in URL
                logger.warning("Target: no auth code in GSP response")
                return False

            # Step 4: Validate token
            token_resp = await client.post(
                "https://gsp.target.com/gsp/oauth_validations/v3/token_validations",
                json={"code": code, "client_id": self._TGT_CLIENT_ID},
                headers={
                    **HEADERS,
                    "Content-Type": "application/json",
                },
            )

            if token_resp.status_code == 200:
                logger.info("Target: GSP OAuth login successful")
                return True

            logger.warning("Target: token validation failed (HTTP %d)", token_resp.status_code)
            return False

        except Exception as exc:
            logger.error("Target GSP login error: %s", exc)
            return False

    async def _tgt_validate_session(self, client: httpx.AsyncClient) -> bool:
        """Check if the current Target session is valid."""
        try:
            resp = await client.get(
                f"https://api.target.com/guest_profile_details/v1/profile_details/profiles"
                f"?fields=address,affiliation,loyalty,paid",
                headers={**HEADERS, "Accept": "application/json"},
            )
            # 200 = authenticated, 401/403 = session expired
            if resp.status_code == 200:
                logger.info("Target: session is valid")
                return True

            # Also try cart endpoint as fallback validation
            cart_resp = await client.get(
                f"https://carts.target.com/web_checkouts/v1/cart"
                f"?cart_type=REGULAR"
                f"&field_groups=CART_ITEMS,SUMMARY"
                f"&key={self._TGT_API_KEY}",
                headers={**HEADERS, "Accept": "application/json"},
            )
            if cart_resp.status_code == 200:
                logger.info("Target: session valid (via cart)")
                return True

            return False
        except Exception as exc:
            logger.debug("Target session validation error: %s", exc)
            return False

    async def _tgt_lookup_product(self, client: httpx.AsyncClient, tcin: str) -> dict | None:
        """Look up product details via Redsky API."""
        try:
            resp = await client.get(
                f"https://redsky.target.com/redsky_aggregations/v1/web/pdp_client_v1"
                f"?key={self._TGT_API_KEY}"
                f"&tcin={tcin}"
                f"&channel=WEB",
                headers={**HEADERS, "Accept": "application/json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                product = data.get("data", {}).get("product", {})
                avail = product.get("fulfillment", {})
                is_available = any(
                    method.get("is_available", False)
                    for method in avail.get("shipping_options", {}).get("availability_status_v2", [])
                ) if avail else True  # default to True if we can't determine
                return {"available": is_available, "product": product}
        except Exception as exc:
            logger.debug("Target Redsky lookup failed: %s", exc)
        return None

    async def _tgt_add_to_cart(self, client: httpx.AsyncClient, tcin: str) -> bool:
        """Add item to Target cart via carts.target.com API.

        Target requires a fulfillment method (STS = Ship To Store, DIGITAL = Ship
        to address).  Without it the cart shows "Choose delivery method" errors.
        """
        # Try shipping first, then pickup as fallback
        for fulfillment_type in ("DIGITAL", "STS"):
            try:
                resp = await client.post(
                    f"https://carts.target.com/web_checkouts/v1/cart_items"
                    f"?key={self._TGT_API_KEY}",
                    json={
                        "cart_type": "REGULAR",
                        "channel_id": 10,
                        "shopping_context": fulfillment_type,
                        "cart_item": {
                            "tcin": tcin,
                            "quantity": 1,
                            "item_channel_id": 10,
                            "fulfillment": {
                                "type": "SHIPT" if fulfillment_type == "DIGITAL" else "PICKUP",
                                "shipping_method": "STANDARD",
                            },
                        },
                    },
                    headers={
                        **HEADERS,
                        "Content-Type": "application/json",
                        "Origin": "https://www.target.com",
                        "Referer": "https://www.target.com/",
                        "Sec-Fetch-Dest": "empty",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Site": "same-site",
                    },
                )
                if resp.status_code in (200, 201):
                    logger.info("Target: added TCIN %s to cart (fulfillment=%s)", tcin, fulfillment_type)
                    return True
                elif resp.status_code == 401:
                    logger.warning("Target: session expired during cart add")
                    return False
                elif resp.status_code == 403:
                    logger.warning("Target: blocked (403) on cart add — PerimeterX")
                    return False
                else:
                    body = resp.text[:300]
                    logger.debug("Target: cart add failed for %s (fulfillment=%s, HTTP %d): %s",
                                 tcin, fulfillment_type, resp.status_code, body)
                    # If this fulfillment type isn't available, try the next one
                    continue
            except Exception as exc:
                logger.error("Target cart add error: %s", exc)
                return False

        logger.warning("Target: all fulfillment methods failed for TCIN %s", tcin)
        return False

    async def _tgt_set_shipping_address(self, client: httpx.AsyncClient, profile: Profile) -> bool:
        """Fetch the user's saved Target address and apply it to the checkout.

        Strategy:
        1. Fetch saved addresses from Target's guest profile API
        2. If no saved address, use Profile from config
        3. Apply via multiple endpoint attempts (Target's API is undocumented)
        """
        api_headers = {
            **HEADERS,
            "Content-Type": "application/json",
            "Origin": "https://www.target.com",
            "Referer": "https://www.target.com/checkout",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }

        address = None

        # Strategy 1: Get saved address from Target profile
        try:
            resp = await client.get(
                "https://api.target.com/guest_profile_details/v1/profile_details/profiles"
                "?fields=address",
                headers={**HEADERS, "Accept": "application/json"},
            )
            logger.info("Target address: profile API returned HTTP %d", resp.status_code)
            if resp.status_code == 200:
                profile_data = resp.json()
                logger.info("Target address: profile response keys: %s", list(profile_data.keys()))
                # Target may nest addresses under different keys
                addresses = (
                    profile_data.get("addresses", [])
                    or profile_data.get("shipping_addresses", [])
                    or profile_data.get("profile", {}).get("addresses", [])
                )
                if not addresses and isinstance(profile_data, dict):
                    # Try to find any key containing address data
                    for key, val in profile_data.items():
                        if isinstance(val, list) and val and isinstance(val[0], dict) and ("zip" in str(val[0]).lower() or "city" in str(val[0]).lower()):
                            addresses = val
                            logger.info("Target address: found addresses under key '%s'", key)
                            break

                for addr in addresses:
                    if addr.get("is_default") or addr.get("default"):
                        address = addr
                        break
                if not address and addresses:
                    address = addresses[0]

                if address:
                    logger.info("Target address: using saved address: %s",
                                {k: v for k, v in address.items() if k in ("city", "state", "zip_code", "zip", "address_type")})
                else:
                    logger.info("Target address: profile returned but no addresses found. Full response: %s",
                                str(profile_data)[:500])
            else:
                logger.info("Target address: profile response body: %s", resp.text[:300])
        except Exception as exc:
            logger.warning("Target address: failed to fetch saved addresses: %s", exc)

        # Strategy 2: Fall back to Profile from config
        if not address and profile.address_line1 and profile.zip_code:
            address = {
                "first_name": profile.first_name,
                "last_name": profile.last_name,
                "address_line1": profile.address_line1,
                "address_line2": profile.address_line2 or "",
                "city": profile.city,
                "state": profile.state,
                "zip_code": profile.zip_code,
                "phone": profile.phone or "",
            }
            logger.info("Target address: using config profile: %s, %s %s",
                        profile.city, profile.state, profile.zip_code)

        if not address:
            logger.warning("Target address: NONE AVAILABLE — no saved Target address, no config profile. "
                          "User needs a saved address in their Target account or a profile in config.yaml")
            return False

        # Normalize field names
        shipping_address = {
            "first_name": address.get("first_name", ""),
            "last_name": address.get("last_name", ""),
            "address_line1": address.get("address_line1") or address.get("street1") or address.get("line1", ""),
            "address_line2": address.get("address_line2") or address.get("street2") or address.get("line2", ""),
            "city": address.get("city", ""),
            "state": address.get("state", ""),
            "zip_code": address.get("zip_code") or address.get("zip") or address.get("postal_code", ""),
            "phone": address.get("phone") or address.get("mobile", ""),
            "country": "US",
            "save_as_default": False,
        }

        # Try multiple Target API endpoints to set the address
        # (Target's internal API is undocumented — try all known patterns)
        endpoints = [
            ("PUT", f"https://carts.target.com/web_checkouts/v1/checkout_address?key={self._TGT_API_KEY}",
             {"cart_type": "REGULAR", "address_type": "SHIPPING", "address": shipping_address}),
            ("PUT", f"https://carts.target.com/web_checkouts/v1/checkout?key={self._TGT_API_KEY}",
             {"cart_type": "REGULAR", "channel_id": 10, "shipping_address": shipping_address}),
            ("PUT", f"https://carts.target.com/web_checkouts/v1/cart?cart_type=REGULAR&key={self._TGT_API_KEY}",
             {"cart_type": "REGULAR", "shipping_address": shipping_address}),
            ("POST", f"https://carts.target.com/web_checkouts/v1/checkout_shipping_address?key={self._TGT_API_KEY}",
             {"cart_type": "REGULAR", "address": shipping_address}),
        ]

        for method, url, body in endpoints:
            try:
                if method == "PUT":
                    resp = await client.put(url, json=body, headers=api_headers)
                else:
                    resp = await client.post(url, json=body, headers=api_headers)

                logger.info("Target address: %s %s → HTTP %d: %s",
                           method, url.split("?")[0].split("/")[-1], resp.status_code, resp.text[:300])

                if resp.status_code in (200, 201, 204):
                    logger.info("Target address: SUCCESS via %s %s", method, url.split("?")[0].split("/")[-1])
                    return True
            except Exception as exc:
                logger.info("Target address: %s %s → exception: %s", method, url.split("?")[0].split("/")[-1], exc)

        logger.warning("Target address: ALL endpoint attempts failed — checkout will likely fail with MISSING_ADDRESS")
        # Return True anyway to let checkout attempt proceed — maybe Target's
        # account has a default address that auto-applies during checkout
        return True

    async def _tgt_checkout(self, client: httpx.AsyncClient,
                            creds: "AccountCredentials | None" = None) -> bool:
        """Initiate Target checkout.

        Tries multiple approaches:
        1. PUT to /checkout to create/update checkout state
        2. POST to /checkout to initiate
        3. POST to /checkout/place_order to finalize (with CVV if available)
        All responses are logged in detail for debugging.
        """
        api_headers = {
            **HEADERS,
            "Content-Type": "application/json",
            "Origin": "https://www.target.com",
            "Referer": "https://www.target.com/checkout",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }

        try:
            # Step 1: Load cart to inspect state
            cart_resp = await client.get(
                f"https://carts.target.com/web_checkouts/v1/cart"
                f"?cart_type=REGULAR"
                f"&field_groups=ADDRESSES,CART_ITEMS,SUMMARY,FULFILLMENT,PAYMENT_INSTRUCTIONS"
                f"&key={self._TGT_API_KEY}",
                headers={**HEADERS, "Accept": "application/json"},
            )
            logger.info("Target checkout: cart GET → HTTP %d", cart_resp.status_code)
            if cart_resp.status_code != 200:
                logger.warning("Target checkout: cart load failed: %s", cart_resp.text[:300])
                return False

            cart_data = cart_resp.json()
            logger.info("Target checkout: cart response keys: %s", list(cart_data.keys()))
            logger.info("Target checkout: cart items: %d, addresses: %s, payment: %s",
                        len(cart_data.get("cart_items", [])),
                        "present" if cart_data.get("addresses") else "missing",
                        "present" if cart_data.get("payment_instructions") else "missing")

            # Log cart item details for debugging
            for item in cart_data.get("cart_items", []):
                logger.info("Target checkout: item tcin=%s, fulfillment=%s, cart_item_id=%s",
                           item.get("tcin"), item.get("fulfillment"), item.get("cart_item_id"))

            # Step 2: Ensure fulfillment is set on all items
            for item in cart_data.get("cart_items", []):
                fulfillment = item.get("fulfillment", {})
                if not fulfillment.get("type"):
                    cart_item_id = item.get("cart_item_id")
                    if cart_item_id:
                        try:
                            ful_resp = await client.put(
                                f"https://carts.target.com/web_checkouts/v1/cart_items/{cart_item_id}"
                                f"?key={self._TGT_API_KEY}",
                                json={"cart_type": "REGULAR", "fulfillment": {"type": "SHIPT", "shipping_method": "STANDARD"}},
                                headers=api_headers,
                            )
                            logger.info("Target checkout: set fulfillment for %s → HTTP %d: %s",
                                       cart_item_id, ful_resp.status_code, ful_resp.text[:200])
                        except Exception as exc:
                            logger.warning("Target checkout: fulfillment set failed: %s", exc)

            # Step 3: Try PUT /checkout first (create/update checkout state)
            put_resp = await client.put(
                f"https://carts.target.com/web_checkouts/v1/checkout"
                f"?key={self._TGT_API_KEY}",
                json={"cart_type": "REGULAR", "channel_id": 10},
                headers=api_headers,
            )
            logger.info("Target checkout: PUT /checkout → HTTP %d: %s",
                        put_resp.status_code, put_resp.text[:500])

            if put_resp.status_code in (200, 201):
                checkout_state = put_resp.json()
                logger.info("Target checkout: PUT response keys: %s", list(checkout_state.keys()))

            # Step 4: Try POST /checkout (initiate/finalize)
            post_resp = await client.post(
                f"https://carts.target.com/web_checkouts/v1/checkout"
                f"?key={self._TGT_API_KEY}",
                json={"cart_type": "REGULAR", "channel_id": 10},
                headers=api_headers,
            )
            logger.info("Target checkout: POST /checkout → HTTP %d: %s",
                        post_resp.status_code, post_resp.text[:500])

            if post_resp.status_code in (200, 201):
                return True

            # Build place-order body with CVV if available
            cvv = creds.card_cvv if creds else ""
            place_body = {"cart_type": "REGULAR"}
            if cvv:
                # Target may require CVV in different field names
                place_body["card_security_code"] = cvv
                place_body["payment_verification"] = {"cvv": cvv}
                logger.info("Target checkout: including CVV in place-order request")
            else:
                logger.warning("Target checkout: NO CVV available — order may fail. "
                             "Add CVV via Dashboard > Settings > Accounts > Target > Edit")

            # Step 5: Try POST /checkout/place_order
            place_resp = await client.post(
                f"https://carts.target.com/web_checkouts/v1/checkout/place_order"
                f"?key={self._TGT_API_KEY}",
                json=place_body,
                headers=api_headers,
            )
            logger.info("Target checkout: POST /checkout/place_order → HTTP %d: %s",
                        place_resp.status_code, place_resp.text[:500])

            if place_resp.status_code in (200, 201):
                logger.info("Target checkout: order placed successfully!")
                return True

            # Step 6: Try POST /place_order (alternative path)
            place2_resp = await client.post(
                f"https://carts.target.com/web_checkouts/v1/place_order"
                f"?key={self._TGT_API_KEY}",
                json=place_body,
                headers=api_headers,
            )
            logger.info("Target checkout: POST /place_order → HTTP %d: %s",
                        place2_resp.status_code, place2_resp.text[:500])

            if place2_resp.status_code in (200, 201):
                logger.info("Target checkout: order placed via /place_order!")
                return True

            logger.warning("Target checkout: all checkout endpoints returned non-success")
            return False
        except Exception as exc:
            logger.error("Target checkout error: %s", exc)
            return False

    # Walmart API constants (from network analysis)
    _WMT_CLIENT_ID = "5f3fb121-076a-45f6-9587-249f0bc160ff"

    async def _checkout_walmart(
        self, url: str, product_name: str, profile: Profile, creds: AccountCredentials
    ) -> CheckoutResult:
        """Walmart API checkout flow.

        Uses real Walmart API endpoints discovered from network analysis:
        - Auth: OpenID Connect via /account/verifyToken (phone/email + MFA verification)
        - Cart: GraphQL via /orchestra/cartxo/graphql/MergeAndGetCart
        - Config: /orchestra/api/ccm/v3/bootstrap
        - General: /swag/graphql

        Walmart uses phone-based login with SMS/email MFA verification,
        making programmatic login impractical. Session cookie import is required.
        """
        client = self._get_client("walmart")
        retailer = "walmart"

        # Extract product/offer ID from URL
        # Formats: /ip/product-name/123456789, /ip/123456789
        match = re.search(r"/ip/[^/]*/(\d+)", url) or re.search(r"/ip/(\d+)", url)
        if not match:
            return CheckoutResult(
                url=url, retailer=retailer, product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message="Could not extract product ID from Walmart URL",
            )
        product_id = match.group(1)

        # Session cookies are required — Walmart uses phone + MFA
        has_session = bool(self._session_cookies.get("walmart"))
        if not has_session:
            return CheckoutResult(
                url=url, retailer=retailer, product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=(
                    "Walmart requires session cookies (phone + MFA login cannot be automated). "
                    "Import cookies via Dashboard > Accounts > Walmart > Import Cookies"
                ),
            )

        # Step 1: Validate session via config bootstrap
        session_valid = await self._wmt_validate_session(client)
        if not session_valid:
            return CheckoutResult(
                url=url, retailer=retailer, product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message="Walmart session expired — re-import cookies via Dashboard",
            )

        # Step 2: Add to cart via GraphQL
        cart_ok = await self._wmt_add_to_cart(client, product_id)
        if not cart_ok:
            return CheckoutResult(
                url=url, retailer=retailer, product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message="Failed to add item to Walmart cart",
            )

        # Step 3: Initiate checkout
        checkout_ok = await self._wmt_checkout(client)
        if checkout_ok:
            return CheckoutResult(
                url=url, retailer=retailer, product_name=product_name,
                status=CheckoutStatus.SUCCESS,
                error_message="Checkout initiated — check your Walmart account for order confirmation",
            )

        return CheckoutResult(
            url=url, retailer=retailer, product_name=product_name,
            status=CheckoutStatus.FAILED,
            error_message="Walmart checkout failed",
        )

    async def _wmt_validate_session(self, client: httpx.AsyncClient) -> bool:
        """Validate Walmart session by loading cart via GraphQL."""
        try:
            # Try the config bootstrap endpoint (lightweight)
            resp = await client.get(
                "https://www.walmart.com/orchestra/api/ccm/v3/bootstrap"
                "?configNames=cart,checkout,identity",
                headers={**HEADERS, "Accept": "application/json"},
            )
            if resp.status_code == 200:
                logger.info("Walmart: session is valid (bootstrap OK)")
                return True

            # Fallback: try GraphQL cart query
            cart_resp = await client.post(
                "https://www.walmart.com/swag/graphql",
                json={
                    "query": "query { cart { id itemCount } }",
                    "variables": {},
                },
                headers={
                    **HEADERS,
                    "Content-Type": "application/json",
                    "Origin": "https://www.walmart.com",
                },
            )
            if cart_resp.status_code == 200:
                data = cart_resp.json()
                if "errors" not in data:
                    logger.info("Walmart: session valid (via GraphQL cart)")
                    return True

            return False
        except Exception as exc:
            logger.debug("Walmart session validation error: %s", exc)
            return False

    async def _wmt_add_to_cart(self, client: httpx.AsyncClient, product_id: str) -> bool:
        """Add item to Walmart cart via GraphQL."""
        # Extract CSRF token from cookies
        csrf = ""
        for name, value in client.cookies.items():
            if "csrf" in name.lower() or name == "CSRF-TOKEN":
                csrf = value
                break

        headers = {
            **HEADERS,
            "Content-Type": "application/json",
            "Origin": "https://www.walmart.com",
            "Referer": "https://www.walmart.com/",
        }
        if csrf:
            headers["x-csrf-token"] = csrf
            headers["WM_SEC.AUTH_TOKEN"] = csrf

        # Try GraphQL add-to-cart (primary method)
        try:
            resp = await client.post(
                "https://www.walmart.com/orchestra/cartxo/graphql/MergeAndGetCart/"
                "f2c8033bafbf986df97ef78677ce6172fc6045f08f6221f28b9ac518d17c7005",
                json={
                    "query": """mutation AddToCart($input: AddToCartInput!) {
                        addToCart(input: $input) {
                            cart { id itemCount }
                        }
                    }""",
                    "variables": {
                        "input": {
                            "items": [{
                                "offerId": product_id,
                                "quantity": 1,
                            }],
                        },
                    },
                },
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                if "errors" not in data:
                    logger.info("Walmart: added product %s to cart via GraphQL", product_id)
                    return True
                logger.warning("Walmart: GraphQL add-to-cart errors: %s", data.get("errors", [])[:2])
        except Exception as exc:
            logger.debug("Walmart GraphQL add-to-cart failed: %s", exc)

        # Fallback: REST API add-to-cart
        try:
            resp = await client.post(
                "https://www.walmart.com/api/v1/cart/items",
                json={
                    "items": [{"offerId": product_id, "quantity": 1}],
                },
                headers=headers,
            )
            if resp.status_code in (200, 201):
                logger.info("Walmart: added to cart via REST API")
                return True
            elif resp.status_code == 401:
                logger.warning("Walmart: session expired during cart add")
            elif resp.status_code == 403:
                logger.warning("Walmart: blocked (403) on cart add — PerimeterX")
            else:
                logger.warning("Walmart: cart add failed (HTTP %d)", resp.status_code)
        except Exception as exc:
            logger.debug("Walmart REST add-to-cart failed: %s", exc)

        return False

    async def _wmt_checkout(self, client: httpx.AsyncClient) -> bool:
        """Initiate Walmart checkout."""
        csrf = ""
        for name, value in client.cookies.items():
            if "csrf" in name.lower() or name == "CSRF-TOKEN":
                csrf = value
                break

        headers = {
            **HEADERS,
            "Content-Type": "application/json",
            "Origin": "https://www.walmart.com",
            "Referer": "https://www.walmart.com/cart",
        }
        if csrf:
            headers["x-csrf-token"] = csrf
            headers["WM_SEC.AUTH_TOKEN"] = csrf

        # Try checkout contract endpoint
        try:
            resp = await client.post(
                "https://www.walmart.com/api/checkout/v1/contract",
                json={"cart_type": "REGULAR"},
                headers=headers,
            )
            if resp.status_code in (200, 201):
                logger.info("Walmart: checkout initiated via contract API")
                return True
        except Exception as exc:
            logger.debug("Walmart checkout contract failed: %s", exc)

        # Fallback: navigate to checkout page
        try:
            resp = await client.get(
                "https://www.walmart.com/checkout",
                headers={**HEADERS, "Accept": "text/html,*/*"},
            )
            if resp.status_code == 200 and "checkout" in resp.text.lower():
                logger.info("Walmart: checkout page loaded")
                return True
        except Exception:
            pass

        return False

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

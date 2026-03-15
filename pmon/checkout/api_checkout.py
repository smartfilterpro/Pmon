"""API-based checkout - uses direct HTTP calls instead of browser automation."""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urljoin

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

    def _get_client(self, retailer: str) -> httpx.AsyncClient:
        if retailer not in self._clients or self._clients[retailer].is_closed:
            self._clients[retailer] = httpx.AsyncClient(
                headers=HEADERS,
                follow_redirects=True,
                timeout=httpx.Timeout(20.0),
            )
        return self._clients[retailer]

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

        # Step 1: Authenticate
        auth_resp = await client.post(
            "https://gsp.target.com/gsp/authentications/v1/auth_codes",
            json={
                "username": creds.email,
                "password": creds.password,
                "keep_me_signed_in": True,
            },
            headers={**HEADERS, "Content-Type": "application/json"},
        )

        if auth_resp.status_code not in (200, 201):
            return CheckoutResult(
                url=url, retailer="target", product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=f"Auth failed (HTTP {auth_resp.status_code})",
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

        # Step 1: Sign in
        login_resp = await client.post(
            "https://www.walmart.com/account/electrode/api/signin",
            json={"username": creds.email, "password": creds.password},
            headers={**HEADERS, "Content-Type": "application/json"},
        )

        if login_resp.status_code != 200:
            return CheckoutResult(
                url=url, retailer="walmart", product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=f"Login failed (HTTP {login_resp.status_code})",
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

    async def close(self):
        for client in self._clients.values():
            if not client.is_closed:
                await client.aclose()

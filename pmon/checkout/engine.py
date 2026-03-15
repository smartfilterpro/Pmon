"""Checkout engine: API-first with optional Playwright browser fallback."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from pmon.config import Config, AccountCredentials, Profile
from pmon.models import CheckoutResult, CheckoutStatus
from pmon.checkout.api_checkout import ApiCheckout

logger = logging.getLogger(__name__)

# Directory to store browser session data (cookies, etc.)
SESSION_DIR = Path(__file__).parent.parent.parent / ".sessions"


class CheckoutEngine:
    """API-first checkout with browser fallback."""

    def __init__(self, config: Config):
        self.config = config
        self._api = ApiCheckout()
        self._browser = None
        self._playwright = None
        self._browser_available = False
        SESSION_DIR.mkdir(exist_ok=True)

    async def start(self):
        """Try to launch the browser for fallback. Not required."""
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )
            self._browser_available = True
            logger.info("Browser fallback available (headless)")
        except Exception as e:
            logger.info(f"Browser fallback unavailable: {e}")
            logger.info("API-only checkout mode — this is fine for most retailers")

    async def stop(self):
        """Close resources."""
        await self._api.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def attempt_checkout(
        self,
        url: str,
        retailer: str,
        product_name: str,
        profile_name: str = "default",
    ) -> CheckoutResult:
        """Attempt checkout: API first, browser fallback."""
        profile = self.config.profiles.get(profile_name) or Profile()
        creds = self.config.accounts.get(retailer)

        if not creds:
            return CheckoutResult(
                url=url,
                retailer=retailer,
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=f"No credentials for {retailer}. Add them in config.",
            )

        # Try API checkout first (fast, headless, works on cloud)
        logger.info(f"Trying API checkout for {product_name}...")
        result = await self._api.attempt(url, retailer, product_name, profile, creds)

        if result.status == CheckoutStatus.SUCCESS:
            return result

        # If API failed and browser is available, try browser fallback
        if self._browser_available:
            logger.info(f"API failed, trying browser fallback for {product_name}...")
            try:
                handler = getattr(self, f"_checkout_{retailer}", None)
                if handler:
                    return await handler(url, product_name, profile, creds)
            except Exception as e:
                logger.error(f"Browser checkout also failed: {e}")
                return CheckoutResult(
                    url=url,
                    retailer=retailer,
                    product_name=product_name,
                    status=CheckoutStatus.FAILED,
                    error_message=f"Both API and browser checkout failed: {e}",
                )

        # Return the API result (which has the error details)
        return CheckoutResult(
            url=url,
            retailer=retailer,
            product_name=product_name,
            status=CheckoutStatus.FAILED,
                error_message=str(e),
            )

    async def _get_context(self, retailer: str):
        """Get or create a browser context with persistent cookies."""
        storage_path = SESSION_DIR / f"{retailer}.json"
        if storage_path.exists():
            context = await self._browser.new_context(
                storage_state=str(storage_path),
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
            )
        else:
            context = await self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
            )
        return context

    async def _save_context(self, context, retailer: str):
        """Save browser cookies/state for reuse."""
        storage_path = SESSION_DIR / f"{retailer}.json"
        await context.storage_state(path=str(storage_path))

    async def _checkout_target(
        self, url: str, product_name: str, profile: Profile, creds: AccountCredentials
    ) -> CheckoutResult:
        """Target checkout flow."""
        context = await self._get_context("target")
        page = await context.new_page()

        try:
            # Navigate to product page
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            # Check if we need to sign in
            if not await self._is_signed_in_target(page):
                await self._sign_in_target(page, creds)

            # Try to add to cart
            add_btn = page.locator('button[data-test="shipItButton"], button:has-text("Add to cart")')
            await add_btn.first.click(timeout=5000)
            await page.wait_for_timeout(1500)

            # Go to cart
            await page.goto("https://www.target.com/cart", wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            # Click checkout
            checkout_btn = page.locator('button:has-text("Check out"), a:has-text("Check out")')
            await checkout_btn.first.click(timeout=5000)
            await page.wait_for_timeout(3000)

            # At this point the user should have payment info saved in their Target account
            # The bot will pause here for manual completion if needed
            place_order = page.locator('button:has-text("Place your order")')
            if await place_order.is_visible(timeout=10000):
                await place_order.click()
                await page.wait_for_timeout(5000)

                await self._save_context(context, "target")
                return CheckoutResult(
                    url=url,
                    retailer="target",
                    product_name=product_name,
                    status=CheckoutStatus.SUCCESS,
                )

            await self._save_context(context, "target")
            return CheckoutResult(
                url=url,
                retailer="target",
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message="Could not find place order button - manual intervention needed",
            )

        except Exception as e:
            return CheckoutResult(
                url=url,
                retailer="target",
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=str(e),
            )
        finally:
            await page.close()
            await context.close()

    async def _is_signed_in_target(self, page) -> bool:
        try:
            account = page.locator('#account, [data-test="accountNav"]')
            text = await account.inner_text(timeout=3000)
            return "sign in" not in text.lower()
        except Exception:
            return False

    async def _sign_in_target(self, page, creds: AccountCredentials):
        await page.goto("https://www.target.com/login", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        await page.fill('#username, input[name="username"]', creds.email)
        await page.fill('#password, input[name="password"]', creds.password)
        await page.click('button[type="submit"], button:has-text("Sign in")')
        await page.wait_for_timeout(3000)

    async def _checkout_walmart(
        self, url: str, product_name: str, profile: Profile, creds: AccountCredentials
    ) -> CheckoutResult:
        """Walmart checkout flow."""
        context = await self._get_context("walmart")
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            # Add to cart
            add_btn = page.locator('button[data-tl-id*="addToCart"], button:has-text("Add to cart")')
            await add_btn.first.click(timeout=5000)
            await page.wait_for_timeout(2000)

            # Go to checkout
            await page.goto("https://www.walmart.com/checkout", wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # Sign in if needed
            sign_in = page.locator('button:has-text("Sign in"), a:has-text("Sign in")')
            if await sign_in.is_visible(timeout=2000):
                await page.fill('input[type="email"]', creds.email)
                await page.fill('input[type="password"]', creds.password)
                await page.click('button[type="submit"]')
                await page.wait_for_timeout(3000)

            # Wait for user to handle captcha if present, then place order
            place_order = page.locator('button:has-text("Place order")')
            if await place_order.is_visible(timeout=15000):
                await place_order.click()
                await page.wait_for_timeout(5000)

                await self._save_context(context, "walmart")
                return CheckoutResult(
                    url=url,
                    retailer="walmart",
                    product_name=product_name,
                    status=CheckoutStatus.SUCCESS,
                )

            await self._save_context(context, "walmart")
            return CheckoutResult(
                url=url,
                retailer="walmart",
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message="Checkout flow interrupted - manual intervention needed",
            )
        except Exception as e:
            return CheckoutResult(
                url=url,
                retailer="walmart",
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=str(e),
            )
        finally:
            await page.close()
            await context.close()

    async def _checkout_pokemoncenter(
        self, url: str, product_name: str, profile: Profile, creds: AccountCredentials
    ) -> CheckoutResult:
        """Pokemon Center checkout flow."""
        context = await self._get_context("pokemoncenter")
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # PKC often has queue - wait for page to load
            # Add to cart
            add_btn = page.locator('button:has-text("Add to Cart"), button:has-text("Add to Bag")')
            await add_btn.first.click(timeout=10000)
            await page.wait_for_timeout(2000)

            # Navigate to cart/checkout
            cart_link = page.locator('a[href*="cart"], a:has-text("Cart"), a:has-text("Bag")')
            await cart_link.first.click(timeout=5000)
            await page.wait_for_timeout(2000)

            checkout_btn = page.locator('button:has-text("Checkout"), a:has-text("Checkout")')
            await checkout_btn.first.click(timeout=5000)
            await page.wait_for_timeout(3000)

            # Sign in if needed
            email_input = page.locator('input[type="email"]')
            if await email_input.is_visible(timeout=3000):
                await email_input.fill(creds.email)
                password_input = page.locator('input[type="password"]')
                await password_input.fill(creds.password)
                await page.click('button[type="submit"]')
                await page.wait_for_timeout(3000)

            # Place order - assumes saved payment on account
            place_order = page.locator('button:has-text("Place Order"), button:has-text("Submit Order")')
            if await place_order.is_visible(timeout=15000):
                await place_order.click()
                await page.wait_for_timeout(5000)

                await self._save_context(context, "pokemoncenter")
                return CheckoutResult(
                    url=url,
                    retailer="pokemoncenter",
                    product_name=product_name,
                    status=CheckoutStatus.SUCCESS,
                )

            await self._save_context(context, "pokemoncenter")
            return CheckoutResult(
                url=url,
                retailer="pokemoncenter",
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message="Checkout flow interrupted - manual intervention needed",
            )
        except Exception as e:
            return CheckoutResult(
                url=url,
                retailer="pokemoncenter",
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=str(e),
            )
        finally:
            await page.close()
            await context.close()

    async def _checkout_bestbuy(
        self, url: str, product_name: str, profile: Profile, creds: AccountCredentials
    ) -> CheckoutResult:
        """Best Buy checkout flow (limited due to invitation system)."""
        context = await self._get_context("bestbuy")
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            # Check for invitation system
            invite_text = await page.locator('text=/invitation/i').count()
            if invite_text > 0:
                return CheckoutResult(
                    url=url,
                    retailer="bestbuy",
                    product_name=product_name,
                    status=CheckoutStatus.FAILED,
                    error_message="Product uses Best Buy invitation system - auto-checkout not possible",
                )

            # Standard add to cart
            add_btn = page.locator('button.add-to-cart-button:not([disabled])')
            await add_btn.click(timeout=5000)
            await page.wait_for_timeout(2000)

            # Go to cart
            await page.goto("https://www.bestbuy.com/cart", wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            checkout_btn = page.locator('button:has-text("Checkout"), a:has-text("Checkout")')
            await checkout_btn.first.click(timeout=5000)
            await page.wait_for_timeout(3000)

            # Sign in if needed
            email_input = page.locator('input#fld-e')
            if await email_input.is_visible(timeout=3000):
                await email_input.fill(creds.email)
                await page.fill('input#fld-p1', creds.password)
                await page.click('button:has-text("Sign In")')
                await page.wait_for_timeout(3000)

            place_order = page.locator('button:has-text("Place Your Order")')
            if await place_order.is_visible(timeout=15000):
                await place_order.click()
                await page.wait_for_timeout(5000)

                await self._save_context(context, "bestbuy")
                return CheckoutResult(
                    url=url,
                    retailer="bestbuy",
                    product_name=product_name,
                    status=CheckoutStatus.SUCCESS,
                )

            await self._save_context(context, "bestbuy")
            return CheckoutResult(
                url=url,
                retailer="bestbuy",
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message="Checkout flow interrupted - manual intervention needed",
            )
        except Exception as e:
            return CheckoutResult(
                url=url,
                retailer="bestbuy",
                product_name=product_name,
                status=CheckoutStatus.FAILED,
                error_message=str(e),
            )
        finally:
            await page.close()
            await context.close()

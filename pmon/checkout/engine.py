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

            # Try to add to cart — decline coverage if offered
            add_btn = page.locator('button[data-test="shipItButton"], button:has-text("Add to cart"), button:has-text("Ship it")')
            await add_btn.first.click(timeout=5000)
            await page.wait_for_timeout(1500)

            # Decline optional coverage/warranty if modal appears
            decline_btn = page.locator('button[data-test="espModalContent-declineCoverageButton"]')
            try:
                if await decline_btn.is_visible(timeout=2000):
                    await decline_btn.click()
                    await page.wait_for_timeout(1000)
            except Exception:
                pass

            # Go to cart via modal button or direct navigation
            cart_checkout = page.locator('button[data-test="addToCartModalViewCartCheckout"], a[href*="/cart"]')
            try:
                if await cart_checkout.first.is_visible(timeout=2000):
                    await cart_checkout.first.click()
                    await page.wait_for_timeout(2000)
                else:
                    await page.goto("https://www.target.com/cart", wait_until="domcontentloaded")
                    await page.wait_for_timeout(2000)
            except Exception:
                await page.goto("https://www.target.com/cart", wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)

            # Click checkout
            checkout_btn = page.locator('button[data-test="checkout-button"], button:has-text("Check out"), a:has-text("Check out")')
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
        await page.goto(
            "https://www.target.com/login?client_id=ecom-web-1.0.0&ui_namespace=ui-default&back_button_action=browser&keep_me_signed_in=true&kmsi_default=true&actions=create_session_request_username",
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(2000)

        email_sel = '#username, input[name="username"], input[type="email"], input[type="tel"]'
        pass_sel = '#password, input[name="password"], input[type="password"]'
        submit_sel = 'button[type="submit"], button:has-text("Sign in"), button:has-text("Continue")'

        # Step 1: Enter email/phone
        await page.locator(email_sel).first.wait_for(state="visible", timeout=10000)
        await page.fill(email_sel, creds.email)

        # Check if password is already visible (single-step) or multi-step
        pass_visible = False
        try:
            pass_visible = await page.locator(pass_sel).first.is_visible(timeout=1000)
        except Exception:
            pass

        if pass_visible:
            await page.fill(pass_sel, creds.password)
            await page.click(submit_sel)
        else:
            # Step 2: Submit email, then select password auth method
            await page.click(submit_sel)
            await page.wait_for_timeout(2000)

            password_option = page.locator('button:has-text("Password"), a:has-text("Password"), [data-test*="password" i], button:has-text("Use password")')
            try:
                if await password_option.first.is_visible(timeout=3000):
                    await password_option.first.click()
                    await page.wait_for_timeout(1000)
            except Exception:
                pass

            # Step 3: Enter password
            await page.locator(pass_sel).first.wait_for(state="visible", timeout=10000)
            await page.fill(pass_sel, creds.password)
            await page.click(submit_sel)

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
            add_btn = page.locator('button[data-tl-id="ProductPrimaryCTA-cta_add_to_cart_button"], button[data-tl-id*="addToCart"], button:has-text("Add to cart")')
            await add_btn.first.click(timeout=5000)
            await page.wait_for_timeout(2000)

            # Go to checkout via button or direct navigation
            checkout_btn = page.locator('button[data-tl-id="IPPacCheckOutBtnBottom"], button:has-text("Check out")')
            try:
                if await checkout_btn.first.is_visible(timeout=3000):
                    await checkout_btn.first.click()
                    await page.wait_for_timeout(3000)
                else:
                    await page.goto("https://www.walmart.com/checkout", wait_until="domcontentloaded")
                    await page.wait_for_timeout(3000)
            except Exception:
                await page.goto("https://www.walmart.com/checkout", wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)

            # Sign in if needed
            sign_in = page.locator('button:has-text("Sign in"), a:has-text("Sign in")')
            try:
                if await sign_in.first.is_visible(timeout=2000):
                    email_sel = 'input[name="email"], input[type="email"]'
                    pass_sel = 'input[type="password"], input[name="password"]'
                    await page.fill(email_sel, creds.email)
                    await page.fill(pass_sel, creds.password)
                    await page.click('button[type="submit"]')
                    await page.wait_for_timeout(3000)
            except Exception:
                pass

            # Guest checkout fallback if not signed in
            guest_btn = page.locator('button[data-tl-id="Wel-Guest_cxo_btn"], button:has-text("Continue without account"), button:has-text("Guest")')
            try:
                if await guest_btn.first.is_visible(timeout=2000):
                    await guest_btn.first.click()
                    await page.wait_for_timeout(2000)
            except Exception:
                pass

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

            # Sign in if needed (may redirect to access.pokemon.com SSO)
            current_url = page.url
            if "access.pokemon.com" in current_url or "sso.pokemon.com" in current_url:
                email_sel = 'input[name="email"], input[name="username"], input[type="email"], input[type="text"]'
                pass_sel = 'input[type="password"], input[name="password"]'
                submit_sel = 'button[type="submit"], button:has-text("Sign In"), button:has-text("Log In"), button:has-text("Continue")'
            else:
                email_sel = 'input[type="email"], input[name="email"], input[id*="email" i], input[id*="login" i], input[name*="email" i]'
                pass_sel = 'input[type="password"], input[name="password"], input[id*="password" i]'
                submit_sel = 'button[type="submit"], button:has-text("Sign In"), button:has-text("Log In"), button:has-text("Continue")'

            email_input = page.locator(email_sel)
            if await email_input.first.is_visible(timeout=5000):
                await email_input.first.fill(creds.email)
                await page.locator(pass_sel).first.fill(creds.password)
                await page.locator(submit_sel).first.click()
                await page.wait_for_timeout(5000)

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
            add_btn = page.locator('button.add-to-cart-button:not([disabled]), button.btn-primary.add-to-cart-button')
            await add_btn.first.click(timeout=5000)
            await page.wait_for_timeout(2000)

            # Go to cart via popup button or direct navigation
            go_to_cart = page.locator('div.go-to-cart-button a, a:has-text("Go to Cart"), a[href*="/cart"]')
            try:
                if await go_to_cart.first.is_visible(timeout=3000):
                    await go_to_cart.first.click()
                    await page.wait_for_timeout(2000)
                else:
                    await page.goto("https://www.bestbuy.com/cart", wait_until="domcontentloaded")
                    await page.wait_for_timeout(2000)
            except Exception:
                await page.goto("https://www.bestbuy.com/cart", wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)

            # Click checkout
            checkout_btn = page.locator('button[data-track="Checkout - Top"], button:has-text("Checkout"), a:has-text("Checkout")')
            await checkout_btn.first.click(timeout=5000)
            await page.wait_for_timeout(3000)

            # Sign in if needed
            email_input = page.locator('input#fld-e, input[id="user.emailAddress"], input[type="email"]')
            try:
                if await email_input.first.is_visible(timeout=3000):
                    await email_input.first.fill(creds.email)
                    pass_input = page.locator('input#fld-p1, input[type="password"]')
                    await pass_input.first.fill(creds.password)
                    await page.click('button:has-text("Sign In"), button[type="submit"]')
                    await page.wait_for_timeout(3000)
            except Exception:
                pass

            # Guest checkout fallback
            guest_btn = page.locator('button.cia-guest-content__continue.guest, button:has-text("Continue as Guest"), button:has-text("Guest")')
            try:
                if await guest_btn.first.is_visible(timeout=2000):
                    await guest_btn.first.click()
                    await page.wait_for_timeout(2000)
            except Exception:
                pass

            place_order = page.locator('button:has-text("Place Your Order"), button:has-text("Place Order")')
            if await place_order.first.is_visible(timeout=15000):
                await place_order.first.click()
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

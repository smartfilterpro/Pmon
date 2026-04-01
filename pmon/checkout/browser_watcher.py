"""Browser-based real-time stock watcher.

Instead of polling via HTTP every 15-30 seconds, this opens product pages
in browser tabs and uses JavaScript MutationObserver to detect stock changes
**instantly** (milliseconds). When an "Add to Cart" or "Buy Now" button
appears, it clicks it immediately — no page load, no delay.

This is how the fastest bots work: sit on the page, react to DOM changes.

Usage:
    watcher = BrowserWatcher(persistent_context, config)
    await watcher.start(products)
    # Products are now being watched in background tabs
    # When in-stock detected → auto-adds to cart → signals for checkout
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)


# JavaScript injected into each watched page. Uses MutationObserver to
# detect when Add to Cart / Buy Now buttons appear or become enabled.
WATCHER_JS = """
() => {
    // Already watching this page
    if (window.__pmon_watching) return;
    window.__pmon_watching = true;
    window.__pmon_in_stock = false;
    window.__pmon_clicked = false;

    const BUTTON_SELECTORS = {
        amazon: [
            '#add-to-cart-button',
            'input[name="submit.add-to-cart"]',
            '#buy-now-button',
        ],
        target: [
            'button[data-test="shipItButton"]',
            'button[data-test="shippingButton"]',
            'button[data-test="buyNowButton"]',
            'button[data-test="addToCartButton"]',
        ],
        walmart: [
            'button[data-testid="add-to-cart-btn"]',
            'button:not([disabled])[aria-label*="Add to cart"]',
        ],
        bestbuy: [
            'button.add-to-cart-button:not([disabled])',
            'button[data-button-state="ADD_TO_CART"]',
        ],
        pokemoncenter: [
            'button[data-testid="add-to-cart"]',
            'button.add-to-cart',
        ],
    };

    // Detect retailer from URL
    const url = window.location.hostname;
    let retailer = 'unknown';
    if (url.includes('amazon')) retailer = 'amazon';
    else if (url.includes('target')) retailer = 'target';
    else if (url.includes('walmart')) retailer = 'walmart';
    else if (url.includes('bestbuy')) retailer = 'bestbuy';
    else if (url.includes('pokemoncenter')) retailer = 'pokemoncenter';

    const selectors = BUTTON_SELECTORS[retailer] || [];
    if (selectors.length === 0) return;

    function checkButtons() {
        for (const sel of selectors) {
            const btn = document.querySelector(sel);
            if (btn && !btn.disabled && btn.offsetParent !== null) {
                if (!window.__pmon_in_stock) {
                    window.__pmon_in_stock = true;
                    window.__pmon_stock_time = Date.now();
                    console.log('[PMON] IN STOCK detected via: ' + sel);
                }
                return true;
            }
        }
        window.__pmon_in_stock = false;
        return false;
    }

    // Check immediately
    checkButtons();

    // Watch for DOM changes (button appearing, becoming enabled, etc.)
    const observer = new MutationObserver(() => {
        checkButtons();
    });

    observer.observe(document.body, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ['disabled', 'style', 'class'],
    });

    // Also poll every 500ms as backup (some sites update without DOM mutations)
    setInterval(checkButtons, 500);
}
"""

# JavaScript to auto-click the first available buy button
AUTO_CLICK_JS = """
() => {
    const BUTTON_SELECTORS = {
        amazon: ['#add-to-cart-button', 'input[name="submit.add-to-cart"]'],
        target: [
            'button[data-test="buyNowButton"]',
            'button[data-test="shipItButton"]',
            'button[data-test="shippingButton"]',
        ],
        walmart: ['button[data-testid="add-to-cart-btn"]'],
        bestbuy: ['button.add-to-cart-button:not([disabled])'],
        pokemoncenter: ['button[data-testid="add-to-cart"]', 'button.add-to-cart'],
    };

    const url = window.location.hostname;
    let retailer = 'unknown';
    if (url.includes('amazon')) retailer = 'amazon';
    else if (url.includes('target')) retailer = 'target';
    else if (url.includes('walmart')) retailer = 'walmart';
    else if (url.includes('bestbuy')) retailer = 'bestbuy';
    else if (url.includes('pokemoncenter')) retailer = 'pokemoncenter';

    const selectors = BUTTON_SELECTORS[retailer] || [];
    for (const sel of selectors) {
        const btn = document.querySelector(sel);
        if (btn && !btn.disabled && btn.offsetParent !== null) {
            btn.click();
            window.__pmon_clicked = true;
            console.log('[PMON] CLICKED: ' + sel);
            return sel;
        }
    }
    return null;
}
"""


@dataclass
class WatchedProduct:
    url: str
    name: str
    retailer: str
    page: object  # Playwright page
    auto_checkout: bool = False
    max_price: float = 0


class BrowserWatcher:
    """Watches product pages in real-time using browser tabs."""

    def __init__(self, context, on_in_stock: Callable | None = None):
        """
        Args:
            context: Playwright BrowserContext (persistent context from --my-browser)
            on_in_stock: async callback(product_url, retailer, page) called when
                         a product comes in stock
        """
        self._context = context
        self._on_in_stock = on_in_stock
        self._watched: dict[str, WatchedProduct] = {}
        self._running = False
        self._check_task: asyncio.Task | None = None

    async def watch(self, url: str, name: str, retailer: str,
                    auto_checkout: bool = False, max_price: float = 0):
        """Open a product page in a new tab and start watching it."""
        if url in self._watched:
            return  # Already watching

        page = await self._context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await page.evaluate(WATCHER_JS)
            logger.info("Watching: %s (%s)", name, retailer)

            self._watched[url] = WatchedProduct(
                url=url, name=name, retailer=retailer,
                page=page, auto_checkout=auto_checkout,
                max_price=max_price,
            )
        except Exception as e:
            logger.error("Failed to open watch tab for %s: %s", name, e)
            try:
                await page.close()
            except Exception:
                pass

    async def unwatch(self, url: str):
        """Stop watching a product and close its tab."""
        wp = self._watched.pop(url, None)
        if wp and wp.page:
            try:
                await wp.page.close()
            except Exception:
                pass

    async def start(self):
        """Start the background check loop."""
        self._running = True
        self._check_task = asyncio.create_task(self._check_loop())
        logger.info("Browser watcher started — checking tabs every 1s")

    async def stop(self):
        """Stop watching and close all tabs."""
        self._running = False
        if self._check_task:
            self._check_task.cancel()
        for url in list(self._watched):
            await self.unwatch(url)

    async def _check_loop(self):
        """Check all watched pages for stock changes every second."""
        try:
            while self._running:
                for url, wp in list(self._watched.items()):
                    try:
                        in_stock = await wp.page.evaluate("() => window.__pmon_in_stock")
                        already_clicked = await wp.page.evaluate("() => window.__pmon_clicked")

                        if in_stock and not already_clicked:
                            logger.info("⚡ INSTANT STOCK DETECTED: %s", wp.name)

                            # Auto-click Add to Cart immediately
                            clicked_sel = await wp.page.evaluate(AUTO_CLICK_JS)
                            if clicked_sel:
                                logger.info("⚡ AUTO-CLICKED '%s' on %s", clicked_sel, wp.name)

                            # Notify the engine for checkout
                            if self._on_in_stock:
                                asyncio.create_task(
                                    self._on_in_stock(url, wp.retailer, wp.page)
                                )

                    except Exception as e:
                        # Page might have navigated or crashed
                        # Don't auto-reload Pokemon Center — their bot detection
                        # triggers on rapid page loads
                        if wp.retailer != "pokemoncenter":
                            logger.debug("Watch tab error for %s: %s — reloading", wp.name, e)
                            try:
                                await wp.page.reload(timeout=10000)
                                await wp.page.evaluate(WATCHER_JS)
                            except Exception:
                                pass
                        else:
                            logger.debug("Watch tab error for %s (PKC, no auto-reload): %s", wp.name, e)

                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def refresh_all(self):
        """Reload all watched pages (useful if pages go stale)."""
        for url, wp in self._watched.items():
            try:
                await wp.page.reload(timeout=10000)
                await wp.page.evaluate(WATCHER_JS)
                # Reset click state on refresh
                await wp.page.evaluate("() => { window.__pmon_clicked = false; }")
                logger.debug("Refreshed watch tab: %s", wp.name)
            except Exception as e:
                logger.debug("Failed to refresh %s: %s", wp.name, e)

    @property
    def watching_count(self) -> int:
        return len(self._watched)

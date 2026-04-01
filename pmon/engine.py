"""Main engine that coordinates monitoring, notifications, and checkout."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timezone

from pmon.config import Config, Product
from pmon.models import MonitorState, StockStatus, CheckoutStatus
from pmon.monitors import get_monitor
from pmon.monitors.base import BaseMonitor
from pmon.notifications.console import ConsoleNotifier
from pmon.notifications.discord import DiscordNotifier
from pmon.checkout.engine import CheckoutEngine
from pmon import database as db

logger = logging.getLogger(__name__)


def _parse_price(price_str: str) -> float:
    """Parse a price string like '$49.99' into a float. Returns 0 if unparseable."""
    if not price_str:
        return 0.0
    match = re.search(r"[\d]+(?:[.,]\d{1,2})?", price_str.replace(",", ""))
    if match:
        return float(match.group())
    return 0.0


class PmonEngine:
    """Main engine that ties everything together."""

    def __init__(self, config: Config):
        self.config = config
        self.state = MonitorState()
        self.checkout_engine: CheckoutEngine | None = None

        # Track which products we've already notified about (avoid spam)
        self._notified: set[str] = set()

        # Track products that have been successfully purchased (user_id:url).
        # Once purchased, auto-checkout is disabled and monitoring skips the product.
        self._purchased: set[str] = set()

        # Monitor instances (cached per retailer)
        self._monitors: dict[str, BaseMonitor] = {}

        # Console notifier (always on)
        self._console_notifier = ConsoleNotifier()

        # Per-user discord notifiers, keyed by webhook URL
        self._discord_notifiers: dict[str, DiscordNotifier] = {}

        self._running = False
        self._monitor_task: asyncio.Task | None = None
        self._browser_watcher = None
        # URLs where browser watcher detected stock but price exceeded max
        self._price_rejected: set[str] = set()

        # All products across all users (synced from DB)
        self._all_products: list[dict] = []

    def sync_products_from_db(self):
        """Reload all products and settings from the database."""
        all_products = []
        # Get all users' products
        conn = db.get_db()
        rows = conn.execute(
            "SELECT p.*, u.id as owner_id FROM products p JOIN users u ON p.user_id = u.id"
        ).fetchall()
        self._all_products = [dict(r) for r in rows]

        # Also sync into config.products for the monitor loop
        self.config.products = []
        seen_urls = set()
        for p in self._all_products:
            if p["url"] not in seen_urls:
                seen_urls.add(p["url"])
                self.config.products.append(Product(
                    url=p["url"],
                    name=p["name"],
                    auto_checkout=bool(p["auto_checkout"]),
                ))

        # Sync poll interval from any user's settings (use lowest if multiple users)
        try:
            row = conn.execute(
                "SELECT MIN(poll_interval) as min_poll FROM user_settings WHERE poll_interval > 0"
            ).fetchone()
            if row and row["min_poll"] and row["min_poll"] > 0:
                new_interval = row["min_poll"]
                if new_interval != self.config.poll_interval:
                    logger.info("Poll interval changed: %ds → %ds", self.config.poll_interval, new_interval)
                    self.config.poll_interval = new_interval
        except Exception:
            pass

        # Refresh session cookies on existing monitors so newly imported
        # cookies take effect without restarting the monitor.
        for retailer, monitor in self._monitors.items():
            self._load_monitor_cookies(retailer, monitor)

    def _get_monitor(self, retailer: str) -> BaseMonitor:
        if retailer not in self._monitors:
            monitor_class = get_monitor(retailer)
            monitor = monitor_class()
            self._monitors[retailer] = monitor
            self._load_monitor_cookies(retailer, monitor)
        return self._monitors[retailer]

    def _load_monitor_cookies(self, retailer: str, monitor: BaseMonitor):
        """Load session cookies from any user into the monitor's httpx client.

        Monitors are shared across users (one per retailer).  We pick the
        first user that has stored session cookies for this retailer.
        """
        try:
            conn = db.get_db()
            row = conn.execute(
                "SELECT cookies_json FROM retailer_sessions "
                "WHERE retailer = ? AND cookies_json != '{}' "
                "ORDER BY updated_at DESC LIMIT 1",
                (retailer,),
            ).fetchone()
            if row and row["cookies_json"]:
                cookies = json.loads(row["cookies_json"])
                if cookies:
                    monitor.load_session_cookies(cookies)
        except Exception as exc:
            logger.debug("Could not load session cookies for %s monitor: %s", retailer, exc)

    async def _start_browser_watcher(self):
        """Start browser-based real-time monitoring in --my-browser mode.

        Opens each product in a browser tab with a MutationObserver that
        detects stock changes instantly. When "Add to Cart" appears, clicks
        it immediately — no HTTP polling delay.
        """
        if not self.config.use_my_browser or not self.checkout_engine:
            return
        ctx = getattr(self.checkout_engine, "_persistent_context", None)
        if not ctx:
            return

        try:
            from pmon.checkout.browser_watcher import BrowserWatcher

            self._browser_watcher = BrowserWatcher(
                ctx, on_in_stock=self._on_browser_stock_detected
            )

            # Open tabs for auto-checkout products (limit to avoid overwhelming)
            auto_products = [p for p in self._all_products if p.get("auto_checkout")]
            for p in auto_products:
                retailer = p.get("retailer", "")
                if retailer in ("amazon", "target", "walmart", "bestbuy"):
                    # Note: Pokemon Center excluded — their PerimeterX bot detection
                    # triggers on browser tabs with MutationObserver. Use HTTP polling instead.
                    await self._browser_watcher.watch(
                        url=p["url"],
                        name=p["name"],
                        retailer=retailer,
                        auto_checkout=True,
                        max_price=p.get("max_price", 0),
                    )

            if self._browser_watcher.watching_count > 0:
                await self._browser_watcher.start()
                logger.info(
                    "⚡ Browser watcher active: %d product tabs open for INSTANT stock detection",
                    self._browser_watcher.watching_count,
                )
            else:
                logger.info("No auto-checkout products — browser watcher not started")
        except Exception as e:
            logger.error("Browser watcher failed to start: %s", e)

    async def _sync_browser_watcher_tabs(self):
        """Open tabs for any new auto-checkout products added after startup."""
        if not self._browser_watcher:
            return
        try:
            for p in self._all_products:
                if not p.get("auto_checkout"):
                    continue
                url = p["url"]
                retailer = p.get("retailer", "")
                # Skip Pokemon Center (bot detection) and already-watched URLs
                if retailer not in ("amazon", "target", "walmart", "bestbuy"):
                    continue
                if url in self._browser_watcher._watched:
                    continue
                # New product — open a tab
                await self._browser_watcher.watch(
                    url=url, name=p["name"], retailer=retailer,
                    auto_checkout=True, max_price=p.get("max_price", 0),
                )
                logger.info("⚡ Opened new watch tab for: %s", p["name"])

            # Also close tabs for products no longer set to auto-checkout
            auto_urls = {p["url"] for p in self._all_products if p.get("auto_checkout")}
            for url in list(self._browser_watcher._watched):
                if url not in auto_urls:
                    await self._browser_watcher.unwatch(url)
                    logger.info("Closed watch tab (auto-buy disabled): %s", url[:80])
        except Exception as e:
            logger.debug("Browser watcher tab sync error: %s", e)

    async def _on_browser_stock_detected(self, url: str, retailer: str, page):
        """Called by BrowserWatcher when a product comes in stock.

        The watcher already clicked "Add to Cart" — now we need to
        complete checkout (after price check).
        """
        if url in self._price_rejected:
            return  # Already rejected due to price — stop spamming

        for p in self._all_products:
            if p["url"] == url:
                user_id = p["owner_id"]
                purchase_key = f"{user_id}:{url}"

                if purchase_key in self._purchased:
                    return  # Already bought or in progress

                max_price = p.get("max_price", 0)
                if max_price and max_price > 0:
                    # Try to get price from the browser page first
                    page_price = 0
                    try:
                        price_text = await page.evaluate("""
                            () => {
                                // Try common price selectors
                                const sels = [
                                    '[data-testid="price"] span', '.price-current',
                                    '.a-price .a-offscreen', '.a-price-whole',
                                    'span[data-test="product-price"]', '.priceView-customer-price span',
                                    '[itemprop="price"]', '.prod-PriceHero .price-group',
                                    'span.a-color-price',
                                ];
                                for (const sel of sels) {
                                    const el = document.querySelector(sel);
                                    if (el) return el.textContent;
                                }
                                // Generic: find any element with a $ price pattern
                                const body = document.body.innerText;
                                const match = body.match(/\\$([\\d,]+\\.\\d{2})/);
                                return match ? match[0] : '';
                            }
                        """)
                        page_price = _parse_price(price_text or "")
                    except Exception:
                        pass

                    # Also check HTTP monitor's price as backup
                    if page_price == 0:
                        stock = self.state.products.get(url)
                        if stock and stock.price:
                            page_price = _parse_price(stock.price)

                    if page_price > 0 and page_price > max_price:
                        logger.warning(
                            "⚡ Browser watcher: price $%.2f exceeds max $%.2f for %s — SKIPPING checkout",
                            page_price, max_price, p["name"]
                        )
                        # Keep __pmon_clicked = true so the watcher stops re-detecting.
                        # It will reset when the page refreshes or price changes.
                        self._price_rejected.add(url)
                        return

                # Price check passed — now click Add to Cart
                logger.info("⚡ Browser watcher: price OK, clicking Add to Cart for %s", p["name"])
                try:
                    from pmon.checkout.browser_watcher import AUTO_CLICK_JS
                    clicked = await page.evaluate(AUTO_CLICK_JS)
                    if clicked:
                        logger.info("⚡ AUTO-CLICKED '%s' on %s", clicked, p["name"])
                except Exception as e:
                    logger.debug("Auto-click failed: %s", e)

                self._purchased.add(purchase_key)
                asyncio.create_task(self._auto_checkout_for_user(p, user_id))
                return

    async def _sync_browser_cookies_to_monitors(self):
        """In --my-browser mode, export cookies from the Playwright browser
        to the HTTP monitors so they benefit from the user's logged-in session.

        This prevents CAPTCHAs on Amazon, Walmart, etc. because the HTTP
        requests carry the same session cookies as the real browser.
        """
        if not self.config.use_my_browser or not self.checkout_engine:
            return
        ctx = getattr(self.checkout_engine, "_persistent_context", None)
        if not ctx:
            return

        try:
            cookies = await ctx.cookies()
            if not cookies:
                return

            # Group cookies by domain → retailer
            retailer_cookies: dict[str, dict[str, str]] = {}
            domain_map = {
                "amazon": [".amazon.com"],
                "target": [".target.com"],
                "walmart": [".walmart.com"],
                "bestbuy": [".bestbuy.com"],
                "pokemoncenter": [".pokemoncenter.com"],
                "costco": [".costco.com"],
                "samsclub": [".samsclub.com"],
            }
            for cookie in cookies:
                domain = cookie.get("domain", "")
                for retailer, domains in domain_map.items():
                    if any(d in domain for d in domains):
                        if retailer not in retailer_cookies:
                            retailer_cookies[retailer] = {}
                        retailer_cookies[retailer][cookie["name"]] = cookie["value"]

            for retailer, cookie_dict in retailer_cookies.items():
                if retailer in self._monitors and cookie_dict:
                    self._monitors[retailer].load_session_cookies(cookie_dict)
        except Exception as e:
            logger.debug("Could not sync browser cookies to monitors: %s", e)

    def _get_discord_notifier(self, webhook: str) -> DiscordNotifier | None:
        if not webhook:
            return None
        if webhook not in self._discord_notifiers:
            self._discord_notifiers[webhook] = DiscordNotifier(webhook)
        return self._discord_notifiers[webhook]

    def start_monitoring_task(self):
        """Start monitoring as a background asyncio task.

        Safe to call multiple times — restarts if previously stopped.
        Used by the CLI and dashboard to start/restart monitoring without
        blocking the caller.
        """
        if self._running and self._monitor_task and not self._monitor_task.done():
            logger.warning("Monitor is already running")
            return
        self._monitor_task = asyncio.create_task(self._monitoring_loop())

    async def _monitoring_loop(self):
        """Core monitoring loop. Runs until stop_monitoring() is called."""
        self.sync_products_from_db()
        self._running = True
        self.state.is_running = True
        self.state.started_at = datetime.now(timezone.utc)
        logger.info(f"Starting monitor with {len(self.config.products)} products, "
                     f"polling every {self.config.poll_interval}s")

        # In --my-browser mode, share the browser's cookies with the HTTP monitors
        # so they don't get CAPTCHAs from Amazon/Walmart/etc.
        await self._sync_browser_cookies_to_monitors()

        # In --my-browser mode, also open product pages in browser tabs
        # for instant stock detection (MutationObserver-based, sub-second)
        await self._start_browser_watcher()

        try:
            while self._running:
                self.sync_products_from_db()
                # Re-sync browser cookies to HTTP monitors each cycle
                await self._sync_browser_cookies_to_monitors()
                # Open tabs for any new auto-checkout products added since startup
                await self._sync_browser_watcher_tabs()
                await self._check_all()
                jitter = self.config.poll_interval * random.uniform(-0.2, 0.2)
                sleep_time = self.config.poll_interval + jitter
                logger.info(f"Poll complete — next check in {sleep_time:.0f}s "
                            f"({len(self.config.products)} products)")
                await asyncio.sleep(sleep_time)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            self.state.is_running = False

    async def start_monitoring(self):
        """Start the monitoring loop (blocking). Kept for backward compat."""
        if self._running:
            logger.warning("Monitor is already running")
            return
        await self._monitoring_loop()

    def stop_monitoring(self):
        """Stop the monitoring loop."""
        self._running = False
        self.state.is_running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            self._monitor_task = None
        logger.info("Monitor stopped")

    async def _check_all(self):
        """Check stock on all monitored products.

        Groups products by retailer and runs each retailer's checks
        sequentially (with per-retailer throttling enforced by BaseMonitor),
        while different retailers are checked concurrently.
        """
        if not self.config.products:
            return

        # Group products by retailer so we can throttle per-retailer
        by_retailer: dict[str, list] = {}
        for product in self.config.products:
            by_retailer.setdefault(product.retailer, []).append(product)

        # Each retailer gets its own sequential task; retailers run in parallel
        tasks = []
        for retailer, products in by_retailer.items():
            try:
                monitor = self._get_monitor(retailer)
            except ValueError:
                logger.warning(f"Skipping unsupported retailer: {retailer}")
                continue
            random.shuffle(products)
            tasks.append(self._check_retailer_group(monitor, products))

        await asyncio.gather(*tasks, return_exceptions=True)

    async def _check_retailer_group(self, monitor, products: list):
        """Check all products for a single retailer sequentially.

        BaseMonitor.safe_check() already enforces per-retailer throttle
        and rate-limit cooldowns, so we just iterate.
        """
        for product in products:
            await self._check_product(monitor, product)

    async def _check_product(self, monitor: BaseMonitor, product: Product):
        result = await monitor.safe_check(product.url, product.name)
        self.state.update_stock(result)

        if result.status == StockStatus.IN_STOCK:
            db.update_last_in_stock(product.url)

            # Notify only once per in-stock event (resets when product goes OOS)
            if product.url not in self._notified:
                self._notified.add(product.url)
                logger.info(f"IN STOCK: {product.name} at {product.retailer}")

                # Notify console
                await self._console_notifier.notify_in_stock(result)

                # Discord notifications per user
                for p in self._all_products:
                    if p["url"] == product.url:
                        user_id = p["owner_id"]
                        settings = db.get_user_settings(user_id)
                        webhook = settings.get("discord_webhook", "")
                        notifier = self._get_discord_notifier(webhook)
                        if notifier:
                            await notifier.notify_in_stock(result)

            # Auto-checkout: checked EVERY poll cycle (not just first detection)
            # so that enabling auto-buy while a product is in stock works immediately
            for p in self._all_products:
                if p["url"] == product.url:
                    user_id = p["owner_id"]
                    purchase_key = f"{user_id}:{product.url}"
                    if p["auto_checkout"] and purchase_key not in self._purchased:
                        # Re-read product data from DB in case auto_checkout was just toggled
                        self.sync_products_from_db()

                        # Check per-product max_price guard
                        max_price = p.get("max_price", 0)
                        if max_price and max_price > 0:
                            price = _parse_price(result.price)
                            if price > 0 and price > max_price:
                                logger.warning(
                                    f"Price too high for {product.name}: "
                                    f"${price:.2f} exceeds max ${max_price:.2f}. "
                                    f"Skipping auto-checkout (likely 3rd-party seller)."
                                )
                                continue

                        # Check spend limit before attempting checkout
                        settings = db.get_user_settings(user_id)
                        spend_limit = settings.get("spend_limit", 0)
                        if spend_limit and spend_limit > 0:
                            total_spent = db.get_user_total_spent(user_id)
                            price = _parse_price(result.price)
                            estimated_cost = price * p.get("quantity", 1)
                            if total_spent + estimated_cost > spend_limit:
                                logger.warning(
                                    f"Spend limit reached for user {user_id}: "
                                    f"${total_spent:.2f} spent + ${estimated_cost:.2f} "
                                    f"would exceed ${spend_limit:.2f} limit. "
                                    f"Skipping auto-checkout for {product.name}"
                                )
                                continue
                        # Run checkout in background so it doesn't block polling
                        # Mark as purchased IMMEDIATELY to prevent duplicate orders
                        # from the next poll cycle (checkout runs in background)
                        self._purchased.add(purchase_key)
                        asyncio.create_task(self._auto_checkout_for_user(p, user_id))

        elif result.status == StockStatus.OUT_OF_STOCK:
            self._notified.discard(product.url)

        if result.status == StockStatus.ERROR:
            db.add_error_log(
                user_id=None,
                level="ERROR",
                source=f"monitor.{product.retailer}",
                message=f"Failed to check {product.name}: {result.error_message}",
            )

    async def _auto_checkout_for_user(self, product_row: dict, user_id: int):
        """Auto-checkout a product for a specific user.

        On successful checkout, disables auto-checkout for this product so the
        bot doesn't keep buying it.
        """
        if not self.checkout_engine:
            self.checkout_engine = CheckoutEngine(self.config)
            await self.checkout_engine.start()

        logger.info(f"Auto-checkout for user {user_id}: {product_row['name']}")

        retailer = product_row["retailer"]

        checkout_result = await self.checkout_engine.attempt_checkout(
            url=product_row["url"],
            retailer=retailer,
            product_name=product_row["name"],
            user_id=user_id,
        )

        # Calculate price amount for spend tracking
        stock = self.state.products.get(product_row["url"])
        price = _parse_price(stock.price if stock else "")
        price_amount = price * product_row.get("quantity", 1)

        # Log to database
        db.add_checkout_log(
            user_id=user_id,
            url=product_row["url"],
            retailer=retailer,
            product_name=product_row["name"],
            status=checkout_result.status.value,
            order_number=checkout_result.order_number,
            error_message=checkout_result.error_message,
            price_amount=price_amount if checkout_result.status == CheckoutStatus.SUCCESS else 0,
        )

        self.state.add_checkout(checkout_result)

        purchase_key = f"{user_id}:{product_row['url']}"

        # On failure: unmark so it retries on the next poll cycle
        if checkout_result.status != CheckoutStatus.SUCCESS:
            self._purchased.discard(purchase_key)
            logger.info(f"Checkout failed for {product_row['name']} — will retry next poll")

        # On success: keep marked and disable auto-checkout for this product
        if checkout_result.status == CheckoutStatus.SUCCESS:
            logger.info(
                f"PURCHASED: {product_row['name']} for user {user_id} — "
                f"disabling auto-checkout to prevent duplicate orders"
            )

            # Disable auto_checkout in the database so it stays off across restarts
            try:
                conn = db.get_db()
                conn.execute(
                    "UPDATE products SET auto_checkout = 0 WHERE user_id = ? AND url = ?",
                    (user_id, product_row["url"]),
                )
                conn.commit()
            except Exception as exc:
                logger.error(f"Failed to disable auto-checkout in DB: {exc}")

        # Notify
        await self._console_notifier.notify_checkout(checkout_result)
        settings = db.get_user_settings(user_id)
        webhook = settings.get("discord_webhook", "")
        if webhook:
            notifier = self._get_discord_notifier(webhook)
            if notifier:
                await notifier.notify_checkout(checkout_result)
                logger.info("Discord notification sent for %s checkout", checkout_result.status.value)
        else:
            logger.debug("No Discord webhook set for user %d — skipping notification", user_id)

    async def manual_checkout(self, product: Product, user_id: int | None = None, dry_run: bool = False):
        """Trigger a manual checkout attempt. If dry_run=True, stops before placing order."""
        if not self.checkout_engine:
            self.checkout_engine = CheckoutEngine(self.config)
            await self.checkout_engine.start()

        checkout_result = await self.checkout_engine.attempt_checkout(
            url=product.url,
            retailer=product.retailer,
            product_name=product.name,
            dry_run=dry_run,
            user_id=user_id,
        )

        if user_id:
            stock = self.state.products.get(product.url)
            price = _parse_price(stock.price if stock else "")
            price_amount = price  # manual checkout uses product's own quantity

            db.add_checkout_log(
                user_id=user_id,
                url=product.url,
                retailer=product.retailer,
                product_name=product.name,
                status=checkout_result.status.value,
                order_number=checkout_result.order_number,
                error_message=checkout_result.error_message,
                price_amount=price_amount if checkout_result.status == CheckoutStatus.SUCCESS else 0,
            )

            # On success: disable auto-buy to prevent duplicate orders
            if checkout_result.status == CheckoutStatus.SUCCESS:
                purchase_key = f"{user_id}:{product.url}"
                self._purchased.add(purchase_key)
                logger.info(
                    "PURCHASED (manual): %s for user %s — disabling auto-checkout",
                    product.name, user_id,
                )
                try:
                    conn = db.get_db()
                    conn.execute(
                        "UPDATE products SET auto_checkout = 0 WHERE user_id = ? AND url = ?",
                        (user_id, product.url),
                    )
                    conn.commit()
                except Exception as exc:
                    logger.error("Failed to disable auto-checkout in DB: %s", exc)

        self.state.add_checkout(checkout_result)
        return checkout_result

    async def init_checkout(self):
        """Initialize the checkout engine (API + optional browser)."""
        self.checkout_engine = CheckoutEngine(self.config)
        await self.checkout_engine.start()

    async def cleanup(self):
        """Clean up resources."""
        if self._browser_watcher:
            await self._browser_watcher.stop()
        for monitor in self._monitors.values():
            await monitor.close()
        if self.checkout_engine:
            await self.checkout_engine.stop()
        for notifier in self._discord_notifiers.values():
            await notifier.close()

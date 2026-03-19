"""Main engine that coordinates monitoring, notifications, and checkout."""

from __future__ import annotations

import asyncio
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

        # All products across all users (synced from DB)
        self._all_products: list[dict] = []

    def sync_products_from_db(self):
        """Reload all products from the database."""
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

    def _get_monitor(self, retailer: str) -> BaseMonitor:
        if retailer not in self._monitors:
            monitor_class = get_monitor(retailer)
            self._monitors[retailer] = monitor_class()
        return self._monitors[retailer]

    def _get_discord_notifier(self, webhook: str) -> DiscordNotifier | None:
        if not webhook:
            return None
        if webhook not in self._discord_notifiers:
            self._discord_notifiers[webhook] = DiscordNotifier(webhook)
        return self._discord_notifiers[webhook]

    async def start_monitoring(self):
        """Start the monitoring loop."""
        if self._running:
            logger.warning("Monitor is already running")
            return

        self.sync_products_from_db()
        self._running = True
        self.state.is_running = True
        self.state.started_at = datetime.now(timezone.utc)
        logger.info(f"Starting monitor with {len(self.config.products)} products, "
                     f"polling every {self.config.poll_interval}s")

        while self._running:
            self.sync_products_from_db()
            await self._check_all()
            # Add ±20% jitter to poll interval to avoid exact-interval bot fingerprint.
            # e.g. 30s → sleeps between 24s and 36s each cycle.
            jitter = self.config.poll_interval * random.uniform(-0.2, 0.2)
            await asyncio.sleep(self.config.poll_interval + jitter)

        self.state.is_running = False

    def stop_monitoring(self):
        """Stop the monitoring loop."""
        self._running = False
        self.state.is_running = False
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
            if product.url not in self._notified:
                self._notified.add(product.url)
                logger.info(f"IN STOCK: {product.name} at {product.retailer}")

                # Notify console
                await self._console_notifier.notify_in_stock(result)

                # Find all users watching this product and notify/auto-buy
                for p in self._all_products:
                    if p["url"] == product.url:
                        user_id = p["owner_id"]
                        settings = db.get_user_settings(user_id)

                        # Discord notification per user
                        webhook = settings.get("discord_webhook", "")
                        notifier = self._get_discord_notifier(webhook)
                        if notifier:
                            await notifier.notify_in_stock(result)

                        # Auto-checkout if enabled and not already purchased
                        purchase_key = f"{user_id}:{product.url}"
                        if p["auto_checkout"] and purchase_key not in self._purchased:
                            # Check spend limit before attempting checkout
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
                            await self._auto_checkout_for_user(p, user_id)

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

        # On success: mark as purchased and disable auto-checkout for this product
        if checkout_result.status == CheckoutStatus.SUCCESS:
            purchase_key = f"{user_id}:{product_row['url']}"
            self._purchased.add(purchase_key)
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
        notifier = self._get_discord_notifier(settings.get("discord_webhook", ""))
        if notifier:
            await notifier.notify_checkout(checkout_result)

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
        for monitor in self._monitors.values():
            await monitor.close()
        if self.checkout_engine:
            await self.checkout_engine.stop()
        for notifier in self._discord_notifiers.values():
            await notifier.close()

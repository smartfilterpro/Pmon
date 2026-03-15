"""Main engine that coordinates monitoring, notifications, and checkout."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from pmon.config import Config, Product
from pmon.models import MonitorState, StockStatus, CheckoutStatus
from pmon.monitors import get_monitor
from pmon.monitors.base import BaseMonitor
from pmon.notifications.console import ConsoleNotifier
from pmon.notifications.discord import DiscordNotifier
from pmon.checkout.engine import CheckoutEngine

logger = logging.getLogger(__name__)


class PmonEngine:
    """Main engine that ties everything together."""

    def __init__(self, config: Config):
        self.config = config
        self.state = MonitorState()
        self.checkout_engine: CheckoutEngine | None = None

        # Track which products we've already notified about (avoid spam)
        self._notified: set[str] = set()

        # Monitor instances (cached per retailer)
        self._monitors: dict[str, BaseMonitor] = {}

        # Notifiers
        self._notifiers = []
        if config.console_notifications:
            self._notifiers.append(ConsoleNotifier())
        if config.discord_webhook:
            self._notifiers.append(DiscordNotifier(config.discord_webhook))

        self._running = False
        self._task: asyncio.Task | None = None

    def _get_monitor(self, retailer: str) -> BaseMonitor:
        if retailer not in self._monitors:
            monitor_class = get_monitor(retailer)
            self._monitors[retailer] = monitor_class()
        return self._monitors[retailer]

    async def start_monitoring(self):
        """Start the monitoring loop."""
        if self._running:
            logger.warning("Monitor is already running")
            return

        self._running = True
        self.state.is_running = True
        self.state.started_at = datetime.now()
        logger.info(f"Starting monitor with {len(self.config.products)} products, "
                     f"polling every {self.config.poll_interval}s")

        while self._running:
            await self._check_all()
            await asyncio.sleep(self.config.poll_interval)

        self.state.is_running = False

    def stop_monitoring(self):
        """Stop the monitoring loop."""
        self._running = False
        self.state.is_running = False
        logger.info("Monitor stopped")

    async def _check_all(self):
        """Check stock on all monitored products."""
        if not self.config.products:
            return

        tasks = []
        for product in self.config.products:
            monitor = self._get_monitor(product.retailer)
            tasks.append(self._check_product(monitor, product))

        await asyncio.gather(*tasks, return_exceptions=True)

    async def _check_product(self, monitor: BaseMonitor, product: Product):
        result = await monitor.safe_check(product.url, product.name)
        self.state.update_stock(result)

        if result.status == StockStatus.IN_STOCK:
            # Only notify if we haven't already for this product
            if product.url not in self._notified:
                self._notified.add(product.url)
                logger.info(f"IN STOCK: {product.name} at {product.retailer}")

                for notifier in self._notifiers:
                    await notifier.notify_in_stock(result)

                # Auto-checkout if enabled
                if product.auto_checkout:
                    await self._auto_checkout(product)

        elif result.status == StockStatus.OUT_OF_STOCK:
            # Reset notification flag when product goes OOS
            self._notified.discard(product.url)

    async def _auto_checkout(self, product: Product):
        if not self.checkout_engine:
            self.checkout_engine = CheckoutEngine(self.config)
            await self.checkout_engine.start()

        logger.info(f"Attempting auto-checkout for {product.name}")
        checkout_result = await self.checkout_engine.attempt_checkout(
            url=product.url,
            retailer=product.retailer,
            product_name=product.name,
        )
        self.state.add_checkout(checkout_result)

        for notifier in self._notifiers:
            await notifier.notify_checkout(checkout_result)

    async def manual_checkout(self, product: Product):
        """Trigger a manual checkout attempt."""
        await self._auto_checkout(product)

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
        for notifier in self._notifiers:
            if hasattr(notifier, 'close'):
                await notifier.close()

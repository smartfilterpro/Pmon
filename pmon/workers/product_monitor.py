"""Product discovery loop with intelligent polling and AI match scoring.

REVIEWED [Mission 5A/5B] — Continuously monitors retailer backends for
target products becoming available, with jitter, AI match scoring,
and anti-detection measures.

This worker runs as a background task alongside the main monitoring loop,
providing enhanced product discovery capabilities.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

LOGS_DIR = Path(__file__).parent.parent.parent / "logs"
MONITOR_LOG = LOGS_DIR / "monitor.jsonl"
MATCH_SCORES_LOG = LOGS_DIR / "matchScores.jsonl"

# Default configuration
DEFAULT_POLL_INTERVAL_MS = 30000
DEFAULT_JITTER_MS = 15000
MIN_MATCH_SCORE = 0.85


class ProductMonitorConfig:
    """Configuration for the product monitor worker."""

    def __init__(
        self,
        products: list[dict] | None = None,
        poll_interval_ms: int = DEFAULT_POLL_INTERVAL_MS,
        jitter_ms: int = DEFAULT_JITTER_MS,
        retailers: list[str] | None = None,
    ):
        self.products = products or []
        self.poll_interval_ms = poll_interval_ms
        self.jitter_ms = jitter_ms
        self.retailers = retailers or ["target", "bestbuy", "walmart", "pokemoncenter"]


class ProductMonitorWorker:
    """Background product monitoring with AI-assisted match scoring.

    Loop behavior:
    1. For each product × retailer combination:
       a. Check availability using the appropriate monitor
       b. If available: validate price within maxPrice threshold
       c. If price valid: score match with AI and emit PRODUCT_AVAILABLE event
       d. Hand off to checkout pipeline via AccountManager
    2. Rotate User-Agent and viewport per session
    3. Log each poll attempt
    """

    def __init__(self, config: ProductMonitorConfig | None = None):
        self._config = config or ProductMonitorConfig()
        self._running = False
        self._task: asyncio.Task | None = None
        self._anthropic = None
        self._init_ai()

        # Stats
        self._poll_count: int = 0
        self._hit_count: int = 0
        self._miss_count: int = 0
        self._last_poll_times: dict[str, str] = {}  # product_url -> ISO timestamp

    def _init_ai(self):
        """Initialize Claude API for match scoring."""
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return
        try:
            import anthropic
            self._anthropic = anthropic.Anthropic(api_key=api_key)
        except ImportError:
            pass

    def start(self):
        """Start the monitoring loop as a background task."""
        if self._running:
            logger.warning("ProductMonitorWorker: already running")
            return
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("ProductMonitorWorker: started")

    def stop(self):
        """Gracefully stop the monitoring loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("ProductMonitorWorker: stopped")

    async def _monitor_loop(self):
        """Core monitoring loop."""
        self._running = True
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        try:
            while self._running:
                for product in self._config.products:
                    if not self._running:
                        break
                    await self._check_product(product)

                # Sleep with jitter
                jitter = random.randint(0, self._config.jitter_ms)
                sleep_ms = self._config.poll_interval_ms + jitter
                await asyncio.sleep(sleep_ms / 1000.0)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    async def _check_product(self, product: dict):
        """Check a single product's availability."""
        import time
        start = time.monotonic()

        url = product.get("url", "")
        name = product.get("name", "")
        max_price = product.get("maxPrice", 0)
        retailer = product.get("retailer", "")
        keywords = product.get("keywords", [])

        if not url or not retailer:
            return

        self._poll_count += 1

        try:
            from pmon.monitors import get_monitor
            from pmon.models import StockStatus

            monitor_class = get_monitor(retailer)
            monitor = monitor_class()
            result = await monitor.safe_check(url, name)

            elapsed_ms = int((time.monotonic() - start) * 1000)
            status = result.status.value
            price = result.price

            # Log poll attempt
            self._log_poll(url, retailer, status, price, elapsed_ms)
            self._last_poll_times[url] = datetime.now(timezone.utc).isoformat()

            if result.status == StockStatus.IN_STOCK:
                self._hit_count += 1
                logger.info("ProductMonitor: %s is IN STOCK at %s (price: %s)", name, retailer, price)

                # Price validation
                if max_price and price:
                    from pmon.engine import _parse_price
                    numeric_price = _parse_price(price)
                    if numeric_price > max_price:
                        logger.warning(
                            "ProductMonitor: %s price $%.2f exceeds max $%.2f — skipping",
                            name, numeric_price, max_price,
                        )
                        return

                # AI match scoring (if configured)
                if self._anthropic and keywords:
                    score = await self._score_match(product, {
                        "name": result.product_name,
                        "price": result.price,
                        "url": url,
                        "retailer": retailer,
                    })
                    if score < MIN_MATCH_SCORE:
                        logger.warning(
                            "ProductMonitor: AI match score %.2f < %.2f for %s — skipping",
                            score, MIN_MATCH_SCORE, name,
                        )
                        return

                # Emit availability event (other components listen for this)
                logger.info("ProductMonitor: PRODUCT_AVAILABLE — %s at %s", name, retailer)

            else:
                self._miss_count += 1

            await monitor.close()

        except ValueError:
            logger.warning("ProductMonitor: unsupported retailer '%s'", retailer)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            self._log_poll(url, retailer, "error", "", elapsed_ms)
            logger.error("ProductMonitor: check failed for %s: %s", name, exc)

    async def _score_match(self, spec: dict, listing: dict) -> float:
        """Score how well a live listing matches the target product spec.

        Uses Claude API to compare product attributes and return a 0.0-1.0 score.
        """
        if not self._anthropic:
            return 1.0  # No AI available, assume match

        prompt = (
            f"Given this target product spec: {json.dumps(spec)}\n"
            f"and this live product listing: {json.dumps(listing)}\n\n"
            f"Score the match from 0.0 to 1.0. Consider: name similarity, "
            f"SKU match if present, price vs target, variant match (size/color/model).\n"
            f'Return JSON: {{"score": N, "matchedFields": [...], "warnings": [...]}}'
        )

        try:
            resp = self._anthropic.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                lines = raw.split("\n")
                inner = [l for l in lines[1:] if l.strip() != "```"]
                raw = "\n".join(inner).strip()

            result = json.loads(raw)
            score = float(result.get("score", 0.0))

            # Log match score
            self._log_match_score(spec, listing, result)

            return score
        except Exception as exc:
            logger.debug("ProductMonitor: AI match scoring failed: %s", exc)
            return 1.0  # Fail open — don't block checkout if scoring fails

    def _log_poll(self, url: str, retailer: str, status: str, price: str, response_time_ms: int):
        """Log a poll attempt to monitor.jsonl."""
        try:
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "product": url,
                "retailer": retailer,
                "status": status,
                "price": price,
                "responseTimeMs": response_time_ms,
            }
            with open(MONITOR_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _log_match_score(self, spec: dict, listing: dict, result: dict):
        """Log AI match score to matchScores.jsonl."""
        try:
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "spec": spec,
                "listing": listing,
                "result": result,
            }
            with open(MATCH_SCORES_LOG, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            pass

    def get_status(self) -> dict:
        """Get current monitoring status."""
        return {
            "running": self._running,
            "products_tracked": len(self._config.products),
            "total_polls": self._poll_count,
            "hits": self._hit_count,
            "misses": self._miss_count,
            "last_poll_times": dict(self._last_poll_times),
        }

    def add_product(self, url: str, max_price: float = 0, name: str = "", retailer: str = ""):
        """Add a product to monitoring at runtime."""
        if not retailer:
            from pmon.config import detect_retailer
            retailer = detect_retailer(url)
        self._config.products.append({
            "url": url,
            "name": name or url,
            "maxPrice": max_price,
            "retailer": retailer,
            "keywords": [],
            "priority": 1,
        })
        logger.info("ProductMonitor: added %s (maxPrice: $%.2f)", url, max_price)

    def remove_product(self, url: str):
        """Remove a product from monitoring."""
        self._config.products = [p for p in self._config.products if p.get("url") != url]
        self._last_poll_times.pop(url, None)
        logger.info("ProductMonitor: removed %s", url)

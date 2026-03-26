# REVIEWED [Mission 2] — Queue wait handler with idle simulation.
"""Queue wait handler — waits patiently when a virtual queue is detected.

Stays on the queue page without navigating away, performs periodic
human-like idle actions to signal activity, and monitors for queue
exit (admission or timeout).
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pmon.checkout.human_behavior import (
    idle_scroll,
    random_delay,
    random_mouse_jitter,
)
from pmon.notifications.notify import NotificationEvent, notify
from pmon.queue.detector import detect_queue

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class QueueExitResult:
    """Outcome of waiting in a virtual queue."""

    admitted: bool
    retailer: str
    wait_duration_seconds: int
    reason: str | None  # 'admitted' | 'timeout' | 'error'
    queue_position_snapshots: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class QueueHandler:
    """Handles waiting in a retail virtual queue until admitted or timeout."""

    # Interval between QUEUE_WAITING notifications (seconds).
    _NOTIFICATION_INTERVAL = 300  # 5 minutes

    # Interval between idle actions (seconds).
    _IDLE_ACTION_INTERVAL = 60

    def __init__(self) -> None:
        self._last_notification_time: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def wait_in_queue(
        self,
        page,
        retailer: str,
        max_wait_seconds: int = 1800,
    ) -> QueueExitResult:
        """Wait in queue until admitted or *max_wait_seconds* elapsed.

        Behavior
        --------
        - Polls queue status every 15-30 s (randomised).
        - Does **not** navigate away (preserves queue position).
        - Performs a small idle action (scroll / mouse jitter) every ~60 s.
        - Emits a SYSTEM notification every 5 minutes.
        - On page error (blank / 500), attempts a single reload.
        - Returns as soon as the queue disappears (admitted) or timeout.
        """
        start = time.monotonic()
        self._last_notification_time = start
        last_idle_time = start
        snapshots: list[dict] = []
        has_reloaded = False

        logger.info(
            "%s: entering virtual queue — max wait %ds",
            retailer, max_wait_seconds,
        )

        while True:
            elapsed = time.monotonic() - start

            # Timeout check
            if elapsed >= max_wait_seconds:
                logger.warning(
                    "%s: queue timeout after %ds", retailer, int(elapsed),
                )
                return QueueExitResult(
                    admitted=False,
                    retailer=retailer,
                    wait_duration_seconds=int(elapsed),
                    reason="timeout",
                    queue_position_snapshots=snapshots,
                )

            # Check if still in queue
            try:
                still_in_queue = await self._check_still_in_queue(page, retailer)
            except Exception as exc:
                logger.warning(
                    "%s: error checking queue status: %s", retailer, exc,
                )
                # Try a single reload on error
                if not has_reloaded:
                    has_reloaded = True
                    try:
                        logger.info("%s: attempting page reload", retailer)
                        await page.reload(wait_until="domcontentloaded")
                    except Exception as reload_exc:
                        logger.error(
                            "%s: reload failed: %s", retailer, reload_exc,
                        )
                        return QueueExitResult(
                            admitted=False,
                            retailer=retailer,
                            wait_duration_seconds=int(elapsed),
                            reason="error",
                            queue_position_snapshots=snapshots,
                        )
                    # Resume waiting after reload
                    still_in_queue = True

            if not still_in_queue:
                wait_secs = int(time.monotonic() - start)
                logger.info(
                    "%s: admitted from queue after %ds", retailer, wait_secs,
                )
                return QueueExitResult(
                    admitted=True,
                    retailer=retailer,
                    wait_duration_seconds=wait_secs,
                    reason="admitted",
                    queue_position_snapshots=snapshots,
                )

            # Extract and log position snapshot
            try:
                snapshot = await self._extract_queue_position(page)
                snapshot["timestamp"] = datetime.now(timezone.utc).isoformat()
                snapshots.append(snapshot)
                if snapshot.get("position") or snapshot.get("estimated_wait"):
                    logger.info(
                        "%s: queue position=%s estimated_wait=%ss",
                        retailer,
                        snapshot.get("position", "?"),
                        snapshot.get("estimated_wait", "?"),
                    )
            except Exception:
                pass

            # Periodic idle action to signal activity
            now = time.monotonic()
            if now - last_idle_time >= self._IDLE_ACTION_INTERVAL:
                await self._perform_idle_action(page)
                last_idle_time = now

            # Periodic notification
            if now - self._last_notification_time >= self._NOTIFICATION_INTERVAL:
                self._last_notification_time = now
                wait_so_far = int(now - start)
                await notify(
                    NotificationEvent.SYSTEM,
                    result={
                        "status": "waiting",
                        "message": (
                            f"{retailer}: still in queue after "
                            f"{wait_so_far}s"
                        ),
                        "retailer": retailer,
                        "elapsed_seconds": wait_so_far,
                        "position": snapshot.get("position") if snapshots else None,
                    },
                )

            # Randomised poll interval: 15-30 seconds
            poll_delay = random.uniform(15.0, 30.0)
            remaining = max_wait_seconds - (time.monotonic() - start)
            if poll_delay > remaining:
                poll_delay = max(remaining, 0)
            await _async_sleep(poll_delay)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _check_still_in_queue(self, page, retailer: str) -> bool:
        """Return True if we are still in the queue (not yet admitted)."""
        result = await detect_queue(page, retailer)
        return result.in_queue

    async def _extract_queue_position(self, page) -> dict:
        """Best-effort extraction of queue position and wait time.

        Looks for text like:
        - "You are number 1234"  -> position=1234
        - "Estimated wait: 5 minutes"  -> estimated_wait=300
        - "less than a minute"  -> estimated_wait=60
        """
        position: int | None = None
        estimated_wait: int | None = None

        try:
            body_text = await page.inner_text("body", timeout=2000)
        except Exception:
            return {"position": None, "estimated_wait": None}

        # Position extraction
        pos_match = re.search(
            r"(?:number|position|#)\s*(\d[\d,]*)", body_text, re.IGNORECASE,
        )
        if pos_match:
            position = int(pos_match.group(1).replace(",", ""))

        # Wait time extraction
        if re.search(r"less than a minute", body_text, re.IGNORECASE):
            estimated_wait = 60
        else:
            hours_match = re.search(r"(\d+)\s*hours?", body_text, re.IGNORECASE)
            mins_match = re.search(r"(\d+)\s*minutes?", body_text, re.IGNORECASE)
            if hours_match:
                estimated_wait = int(hours_match.group(1)) * 3600
            if mins_match:
                mins_val = int(mins_match.group(1)) * 60
                estimated_wait = (estimated_wait or 0) + mins_val

        return {"position": position, "estimated_wait": estimated_wait}

    async def _perform_idle_action(self, page) -> None:
        """Small human-like action to signal browser activity."""
        try:
            action = random.choice(["jitter", "scroll", "delay"])
            if action == "jitter":
                await random_mouse_jitter(page)
            elif action == "scroll":
                await idle_scroll(page)
            else:
                await random_delay(page, 300, 800)
        except Exception as exc:
            logger.debug("Idle action failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

async def _async_sleep(seconds: float) -> None:
    """Thin wrapper so tests can mock sleep easily."""
    import asyncio

    await asyncio.sleep(seconds)

# REVIEWED [Mission 2] — Virtual queue detection for all retailers.
"""Virtual queue detection for all supported retailers."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Detection result
# ---------------------------------------------------------------------------

@dataclass
class QueueDetectionResult:
    """Result of a virtual queue detection check."""

    in_queue: bool
    queue_type: str  # 'queue-it', 'custom', 'waiting-room', 'none'
    retailer: str
    estimated_wait_seconds: int | None


# ---------------------------------------------------------------------------
# Queue signatures per retailer
# ---------------------------------------------------------------------------

QUEUE_SIGNATURES: dict[str, list[dict[str, str]]] = {
    "target": [
        {"type": "url", "pattern": "queue-it.net"},
        {"type": "url", "pattern": "target.com/q/"},
        {"type": "selector", "value": "#queueit_overlay"},
        {"type": "text", "value": "You are in line"},
        {"type": "text", "value": "Please wait"},
    ],
    "walmart": [
        {"type": "url", "pattern": "queue.walmart.com"},
        {"type": "selector", "value": ".queue-overlay"},
        {"type": "text", "value": "You're in the queue"},
    ],
    "pokemoncenter": [
        {"type": "url", "pattern": "queue-it.net"},
        {"type": "selector", "value": "#queueit_overlay"},
        {"type": "text", "value": "high demand"},
        {"type": "text", "value": "waiting room"},
    ],
    "bestbuy": [
        {"type": "url", "pattern": "bestbuy.com/site/misc/waiting-room"},
        {"type": "text", "value": "waiting room"},
        {"type": "text", "value": "You are number"},
    ],
    "costco": [
        {"type": "url", "pattern": "queue-it.net"},
    ],
    "samsclub": [
        {"type": "url", "pattern": "queue-it.net"},
    ],
}

# Patterns used to infer the queue technology from a matched signature.
_QUEUE_TYPE_HINTS: dict[str, str] = {
    "queue-it.net": "queue-it",
    "queueit": "queue-it",
    "waiting-room": "waiting-room",
    "waiting room": "waiting-room",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def detect_queue(page, retailer: str) -> QueueDetectionResult:
    """Check if the current page is a virtual queue or waiting room.

    Checks in order: URL patterns, CSS selectors, text content.
    Also attempts to extract estimated wait time from page text.

    Parameters
    ----------
    page : playwright.async_api.Page
        The current Playwright page.
    retailer : str
        Retailer key (must exist in QUEUE_SIGNATURES).

    Returns
    -------
    QueueDetectionResult
    """
    signatures = QUEUE_SIGNATURES.get(retailer, [])
    if not signatures:
        logger.debug("No queue signatures configured for retailer=%s", retailer)
        return QueueDetectionResult(
            in_queue=False, queue_type="none",
            retailer=retailer, estimated_wait_seconds=None,
        )

    current_url = page.url
    matched_queue_type: str | None = None

    # 1. URL pattern checks
    for sig in signatures:
        if sig["type"] != "url":
            continue
        pattern = sig["pattern"]
        if pattern in current_url:
            logger.info("Queue detected via URL pattern %r on %s", pattern, retailer)
            matched_queue_type = _infer_queue_type(pattern)
            break

    # 2. Selector checks (only if not already matched)
    if matched_queue_type is None:
        for sig in signatures:
            if sig["type"] != "selector":
                continue
            try:
                visible = await page.locator(sig["value"]).is_visible(timeout=1000)
                if visible:
                    logger.info("Queue detected via selector %r on %s", sig["value"], retailer)
                    matched_queue_type = _infer_queue_type(sig["value"])
                    break
            except Exception:
                continue

    # 3. Text content checks (only if not already matched)
    if matched_queue_type is None:
        try:
            body_text = await page.inner_text("body", timeout=2000)
        except Exception:
            body_text = ""

        for sig in signatures:
            if sig["type"] != "text":
                continue
            if sig["value"].lower() in body_text.lower():
                logger.info("Queue detected via text %r on %s", sig["value"], retailer)
                matched_queue_type = _infer_queue_type(sig["value"])
                break

    if matched_queue_type is None:
        return QueueDetectionResult(
            in_queue=False, queue_type="none",
            retailer=retailer, estimated_wait_seconds=None,
        )

    # 4. Try to extract estimated wait time
    estimated_wait = await _extract_wait_time(page)

    return QueueDetectionResult(
        in_queue=True,
        queue_type=matched_queue_type,
        retailer=retailer,
        estimated_wait_seconds=estimated_wait,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_queue_type(matched_value: str) -> str:
    """Infer the queue technology from the matched signature value."""
    lower = matched_value.lower()
    for hint, qtype in _QUEUE_TYPE_HINTS.items():
        if hint in lower:
            return qtype
    return "custom"


async def _extract_wait_time(page) -> int | None:
    """Best-effort extraction of estimated wait seconds from page text."""
    try:
        body_text = await page.inner_text("body", timeout=2000)
    except Exception:
        return None

    # "less than a minute" → 60s
    if re.search(r"less than a minute", body_text, re.IGNORECASE):
        return 60

    # "X hours" → seconds
    hours_match = re.search(r"(\d+)\s*hours?", body_text, re.IGNORECASE)
    if hours_match:
        return int(hours_match.group(1)) * 3600

    # "X minutes" → seconds
    mins_match = re.search(r"(\d+)\s*minutes?", body_text, re.IGNORECASE)
    if mins_match:
        return int(mins_match.group(1)) * 60

    return None

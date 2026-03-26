"""Centralized notification dispatcher with accuracy tracking.

REVIEWED [Mission 3] — All notifications must flow through notify() to ensure:
1. Status is read from a resolved result object (never intermediate catch vars)
2. Every notification is logged to logs/notification_log.jsonl
3. Notifications only fire for terminal states (success/failure/cancelled)
4. Post-session retroactive accuracy marking is supported

Usage:
    from pmon.notifications.notify import notify, NotificationEvent

    result = await checkout_engine.attempt_checkout(...)
    await notify(
        NotificationEvent.CHECKOUT_RESULT,
        result={"status": result.status.value, "product": name, ...},
        notifiers=[console_notifier, discord_notifier],
        session_id=session_id,
    )
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

# Log file for notification audit trail
LOG_DIR = Path(__file__).parent.parent.parent / "logs"
NOTIFICATION_LOG = LOG_DIR / "notification_log.jsonl"

# Valid terminal statuses that justify sending a notification
TERMINAL_STATUSES = {"success", "failed", "cancelled"}


class NotificationEvent(Enum):
    """Known notification event types."""
    STOCK_IN_STOCK = "stock_in_stock"
    STOCK_OUT_OF_STOCK = "stock_out_of_stock"
    CHECKOUT_RESULT = "checkout_result"
    CHECKOUT_SUCCESS = "checkout_success"
    CHECKOUT_FAILED = "checkout_failed"
    ERROR = "error"
    SYSTEM = "system"


def _ensure_log_dir():
    """Create logs directory if needed."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def _append_log(entry: dict):
    """Append a notification log entry to the JSONL file."""
    try:
        _ensure_log_dir()
        with open(NOTIFICATION_LOG, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as exc:
        logger.debug("Failed to write notification log: %s", exc)


async def notify(
    event: NotificationEvent,
    result: dict,
    *,
    notifiers: list | None = None,
    session_id: str = "",
) -> bool:
    """Central notification dispatcher.

    Validates the result status, logs the notification, then dispatches
    to all configured notifier channels.

    Parameters
    ----------
    event : the type of notification event
    result : dict with at minimum {"status": "success"|"failed"|"cancelled", ...}
             For stock events: {"product_name", "retailer", "url", "price"}
             For checkout events: {"product_name", "retailer", "url", "order_number", "error_message"}
    notifiers : list of BaseNotifier instances to dispatch to
    session_id : optional session identifier for retroactive accuracy marking

    Returns
    -------
    True if notification was sent, False if blocked (invalid status, etc.)
    """
    status = result.get("status", "")

    # Validate terminal status for checkout events
    if event in (
        NotificationEvent.CHECKOUT_RESULT,
        NotificationEvent.CHECKOUT_SUCCESS,
        NotificationEvent.CHECKOUT_FAILED,
    ):
        if status not in TERMINAL_STATUSES:
            logger.warning(
                "Notification blocked: event=%s has non-terminal status '%s'. "
                "Notifications must only fire after operation has fully resolved.",
                event.value, status,
            )
            return False

    # Build log entry
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event.value,
        "status": status,
        "payload": result,
        "session_id": session_id,
        "accurate": None,  # Set retroactively after session completes
    }
    _append_log(log_entry)

    # Dispatch to notifiers
    if not notifiers:
        return True

    from pmon.models import StockResult, StockStatus, CheckoutResult, CheckoutStatus

    for notifier in notifiers:
        if notifier is None:
            continue
        try:
            if event == NotificationEvent.STOCK_IN_STOCK:
                stock_result = StockResult(
                    url=result.get("url", ""),
                    retailer=result.get("retailer", ""),
                    product_name=result.get("product_name", ""),
                    status=StockStatus.IN_STOCK,
                    price=result.get("price", ""),
                )
                await notifier.notify_in_stock(stock_result)

            elif event in (
                NotificationEvent.CHECKOUT_RESULT,
                NotificationEvent.CHECKOUT_SUCCESS,
                NotificationEvent.CHECKOUT_FAILED,
            ):
                checkout_status = (
                    CheckoutStatus.SUCCESS if status == "success"
                    else CheckoutStatus.FAILED
                )
                checkout_result = CheckoutResult(
                    url=result.get("url", ""),
                    retailer=result.get("retailer", ""),
                    product_name=result.get("product_name", ""),
                    status=checkout_status,
                    order_number=result.get("order_number", ""),
                    error_message=result.get("error_message", ""),
                )
                await notifier.notify_checkout(checkout_result)

        except Exception as exc:
            logger.error("Notification dispatch failed for %s: %s", type(notifier).__name__, exc)

    return True


def mark_notifications_accuracy(session_id: str, final_status: str):
    """Retroactively mark all notifications in a session as accurate or inaccurate.

    Called after a checkout session completes to tag prior notifications
    with their accuracy relative to the final outcome.

    Parameters
    ----------
    session_id : the session to update
    final_status : the true final status ("success" or "failed")
    """
    if not NOTIFICATION_LOG.exists():
        return

    try:
        lines = NOTIFICATION_LOG.read_text().strip().split("\n")
        updated_lines = []

        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if entry.get("session_id") == session_id and session_id:
                    entry_status = entry.get("status", "")
                    if entry_status in TERMINAL_STATUSES:
                        # A notification is accurate if its status matches final outcome
                        entry["accurate"] = (entry_status == final_status)
                updated_lines.append(json.dumps(entry, default=str))
            except json.JSONDecodeError:
                updated_lines.append(line)

        NOTIFICATION_LOG.write_text("\n".join(updated_lines) + "\n")
        logger.info(
            "Marked notification accuracy for session %s (final: %s)",
            session_id, final_status,
        )
    except Exception as exc:
        logger.debug("Failed to mark notification accuracy: %s", exc)


def get_notification_stats(hours: int = 24) -> dict:
    """Get notification statistics for the health dashboard.

    Returns counts of total, accurate, and inaccurate notifications
    within the specified time window.
    """
    if not NOTIFICATION_LOG.exists():
        return {"total": 0, "accurate": 0, "inaccurate": 0, "unmarked": 0}

    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    total = accurate = inaccurate = unmarked = 0
    try:
        for line in NOTIFICATION_LOG.read_text().strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                ts = entry.get("timestamp", "")
                if ts:
                    entry_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if entry_time < cutoff:
                        continue
                total += 1
                acc = entry.get("accurate")
                if acc is True:
                    accurate += 1
                elif acc is False:
                    inaccurate += 1
                else:
                    unmarked += 1
            except (json.JSONDecodeError, ValueError):
                continue
    except Exception:
        pass

    return {
        "total": total,
        "accurate": accurate,
        "inaccurate": inaccurate,
        "unmarked": unmarked,
    }

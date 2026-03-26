"""Checkout flow runner — dispatches to per-retailer handlers.

CREATED [Mission 3] — Orchestrates the step-based checkout flow for any
supported retailer.  Handles queue detection between steps, logs each step
to session log, and maps results to the legacy CheckoutResult model.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from pmon.checkout_flows.base import (
    BaseCheckoutHandler,
    CheckoutFlowResult,
    CheckoutStatus,
    StepResult,
)
from pmon.models import CheckoutResult
from pmon.models import CheckoutStatus as LegacyCheckoutStatus
from pmon.queue.detector import detect_queue

logger = logging.getLogger(__name__)

# Lazy imports to avoid circular dependencies at module level
_SESSION_LOG_WRITER = None


def _get_session_log_writer():
    """Lazy import of write_session_log to avoid circular imports."""
    global _SESSION_LOG_WRITER
    if _SESSION_LOG_WRITER is None:
        try:
            from pmon.workers.log_review_worker import write_session_log
            _SESSION_LOG_WRITER = write_session_log
        except ImportError:
            logger.warning("write_session_log not available — session logging disabled")
            _SESSION_LOG_WRITER = lambda sid, entry: None
    return _SESSION_LOG_WRITER


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

_HANDLER_REGISTRY: dict[str, type[BaseCheckoutHandler]] = {}


def _ensure_registry():
    """Populate the handler registry on first use."""
    if _HANDLER_REGISTRY:
        return

    from pmon.checkout_flows.target import TargetCheckoutHandler
    from pmon.checkout_flows.walmart import WalmartCheckoutHandler
    from pmon.checkout_flows.pokemoncenter import PokemonCenterCheckoutHandler
    from pmon.checkout_flows.bestbuy import BestBuyCheckoutHandler
    from pmon.checkout_flows.costco import CostcoCheckoutHandler
    from pmon.checkout_flows.samsclub import SamsClubCheckoutHandler

    _HANDLER_REGISTRY.update({
        "target": TargetCheckoutHandler,
        "walmart": WalmartCheckoutHandler,
        "pokemoncenter": PokemonCenterCheckoutHandler,
        "bestbuy": BestBuyCheckoutHandler,
        "costco": CostcoCheckoutHandler,
        "samsclub": SamsClubCheckoutHandler,
    })


def get_handler(retailer: str, **kwargs) -> BaseCheckoutHandler:
    """Get the checkout handler for the given retailer."""
    _ensure_registry()
    handler_cls = _HANDLER_REGISTRY.get(retailer.lower())
    if handler_cls is None:
        raise ValueError(f"No checkout handler registered for retailer: {retailer}")
    return handler_cls(**kwargs)


# ---------------------------------------------------------------------------
# Checkout runner
# ---------------------------------------------------------------------------

# Steps in execution order.  fill_shipping is optional (skipped if handler
# returns success=True with message="skipped").
_STEP_ORDER = [
    "navigate_to_cart",
    "verify_cart_contents",
    "proceed_to_checkout",
    "fill_shipping",
    "fill_payment",
    "review_order",
    "place_order",
]


class CheckoutRunner:
    """Runs the per-retailer checkout flow with logging and queue detection."""

    def __init__(self, engine=None, vision_helper=None):
        self._engine = engine
        self._vision = vision_helper

    async def run(
        self,
        page,
        retailer: str,
        url: str,
        product_name: str,
        creds=None,
        profile=None,
        max_price: float = 0,
    ) -> CheckoutFlowResult:
        """Execute the full checkout flow for the given retailer.

        Returns a CheckoutFlowResult with step-level detail.
        """
        session_id = uuid.uuid4().hex[:12]
        write_log = _get_session_log_writer()

        handler = get_handler(
            retailer,
            engine=self._engine,
            vision_helper=self._vision,
            max_price=max_price,
        )

        # Set profile for handlers that need it (e.g. PKC shipping)
        if hasattr(handler, "_profile"):
            handler._profile = profile

        steps_completed: list[str] = []
        result = CheckoutFlowResult(
            url=url, retailer=retailer, product_name=product_name,
            status=CheckoutStatus.FAILED,
        )

        for step_name in _STEP_ORDER:
            # Queue detection between steps
            queue_result = await detect_queue(page, retailer)
            if queue_result.in_queue:
                logger.warning("%s: queue detected before %s — aborting", retailer, step_name)
                write_log(session_id, {
                    "step": step_name, "event": "queue_detected",
                    "queue_type": queue_result.queue_type,
                })
                result.status = CheckoutStatus.QUEUE_TIMEOUT
                result.error_message = f"Queue detected before {step_name}"
                return result

            # Execute the step
            step_method = getattr(handler, step_name)
            try:
                if step_name == "fill_payment":
                    step_result: StepResult = await step_method(page, creds)
                else:
                    step_result = await step_method(page)
            except Exception as exc:
                logger.error("%s: step %s raised %s: %s", retailer, step_name, type(exc).__name__, exc)
                write_log(session_id, {"step": step_name, "event": "error", "error": str(exc)})
                result.error_message = f"{step_name}: {exc}"
                return result

            write_log(session_id, {
                "step": step_name,
                "success": step_result.success,
                "message": step_result.message,
            })

            if not step_result.success:
                # Price guard produces a specific status
                if step_result.message == "price_exceeded":
                    result.status = CheckoutStatus.PRICE_EXCEEDED
                    result.error_message = "Price exceeded maximum"
                else:
                    result.error_message = f"{step_name}: {step_result.message}"
                return result

            steps_completed.append(step_name)

        # All steps passed — now confirm the order
        try:
            confirm_result = await handler.confirm_order_placed(page)
        except Exception as exc:
            logger.error("%s: confirm_order_placed raised: %s", retailer, exc)
            confirm_result = StepResult(success=True, step_name="confirm_order_placed", message="unknown")

        steps_completed.append("confirm_order_placed")
        write_log(session_id, {
            "step": "confirm_order_placed",
            "success": confirm_result.success,
            "message": confirm_result.message,
        })

        if confirm_result.message == "unknown":
            result.status = CheckoutStatus.UNKNOWN
        else:
            result.status = CheckoutStatus.PLACED
            result.order_number = confirm_result.message

        result.steps_completed = steps_completed
        return result


# ---------------------------------------------------------------------------
# Legacy compatibility
# ---------------------------------------------------------------------------

def flow_result_to_legacy(flow_result: CheckoutFlowResult) -> CheckoutResult:
    """Map a CheckoutFlowResult to the legacy CheckoutResult model.

    This allows the new flow system to integrate with existing code that
    expects the old CheckoutResult/CheckoutStatus types.
    """
    status_map = {
        CheckoutStatus.PLACED: LegacyCheckoutStatus.SUCCESS,
        CheckoutStatus.UNKNOWN: LegacyCheckoutStatus.SUCCESS,
        CheckoutStatus.FAILED: LegacyCheckoutStatus.FAILED,
        CheckoutStatus.CANCELLED: LegacyCheckoutStatus.FAILED,
        CheckoutStatus.QUEUE_TIMEOUT: LegacyCheckoutStatus.FAILED,
        CheckoutStatus.OUT_OF_STOCK: LegacyCheckoutStatus.FAILED,
        CheckoutStatus.PRICE_EXCEEDED: LegacyCheckoutStatus.FAILED,
        CheckoutStatus.AUTH_FAILED: LegacyCheckoutStatus.FAILED,
    }

    return CheckoutResult(
        url=flow_result.url,
        retailer=flow_result.retailer,
        product_name=flow_result.product_name,
        status=status_map.get(flow_result.status, LegacyCheckoutStatus.FAILED),
        order_number=flow_result.order_number,
        error_message=flow_result.error_message,
        timestamp=flow_result.timestamp,
    )

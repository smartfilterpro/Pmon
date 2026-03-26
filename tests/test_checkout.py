"""Tests for the checkout flow extraction (Mission 3).

Validates the step-based checkout architecture: data models, price guard,
confirmation logic, and runner dispatch.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pmon.checkout_flows.base import (
    BaseCheckoutHandler,
    CheckoutFlowResult,
    CheckoutStatus,
    StepResult,
)
from pmon.checkout_flows.runner import (
    CheckoutRunner,
    flow_result_to_legacy,
    get_handler,
)
from pmon.models import CheckoutStatus as LegacyCheckoutStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CheckoutStatus enum
# ---------------------------------------------------------------------------

class TestCheckoutStatus:
    """Verify CheckoutStatus has all required values."""

    def test_placed(self):
        assert CheckoutStatus.PLACED.value == "placed"

    def test_unknown(self):
        assert CheckoutStatus.UNKNOWN.value == "unknown"

    def test_failed(self):
        assert CheckoutStatus.FAILED.value == "failed"

    def test_cancelled(self):
        assert CheckoutStatus.CANCELLED.value == "cancelled"

    def test_queue_timeout(self):
        assert CheckoutStatus.QUEUE_TIMEOUT.value == "queue_timeout"

    def test_out_of_stock(self):
        assert CheckoutStatus.OUT_OF_STOCK.value == "out_of_stock"

    def test_price_exceeded(self):
        assert CheckoutStatus.PRICE_EXCEEDED.value == "price_exceeded"

    def test_auth_failed(self):
        assert CheckoutStatus.AUTH_FAILED.value == "auth_failed"

    def test_all_values_present(self):
        expected = {
            "placed", "unknown", "failed", "cancelled",
            "queue_timeout", "out_of_stock", "price_exceeded", "auth_failed",
        }
        actual = {s.value for s in CheckoutStatus}
        assert actual == expected


# ---------------------------------------------------------------------------
# StepResult
# ---------------------------------------------------------------------------

class TestStepResult:
    """Verify StepResult creation and fields."""

    def test_basic_creation(self):
        result = StepResult(success=True, step_name="test_step")
        assert result.success is True
        assert result.step_name == "test_step"
        assert result.message == ""
        assert result.screenshot_b64 is None

    def test_with_message(self):
        result = StepResult(success=False, step_name="add_to_cart", message="Button not found")
        assert result.success is False
        assert result.message == "Button not found"

    def test_with_screenshot(self):
        result = StepResult(success=True, step_name="confirm", screenshot_b64="abc123")
        assert result.screenshot_b64 == "abc123"


# ---------------------------------------------------------------------------
# CheckoutFlowResult
# ---------------------------------------------------------------------------

class TestCheckoutFlowResult:
    """Verify CheckoutFlowResult creation and defaults."""

    def test_basic_creation(self):
        result = CheckoutFlowResult(
            url="https://example.com/product",
            retailer="target",
            product_name="Test Product",
            status=CheckoutStatus.PLACED,
        )
        assert result.url == "https://example.com/product"
        assert result.retailer == "target"
        assert result.status == CheckoutStatus.PLACED
        assert result.order_number == ""
        assert result.steps_completed == []
        assert result.total_price == 0.0

    def test_with_order_number(self):
        result = CheckoutFlowResult(
            url="https://example.com",
            retailer="walmart",
            product_name="Widget",
            status=CheckoutStatus.PLACED,
            order_number="WMT-12345",
            steps_completed=["navigate_to_cart", "place_order"],
            total_price=29.99,
        )
        assert result.order_number == "WMT-12345"
        assert len(result.steps_completed) == 2
        assert result.total_price == 29.99

    def test_timestamp_auto_set(self):
        result = CheckoutFlowResult(
            url="", retailer="", product_name="",
            status=CheckoutStatus.FAILED,
        )
        assert result.timestamp is not None
        assert result.timestamp.tzinfo is not None  # timezone-aware


# ---------------------------------------------------------------------------
# confirm_order_placed returns UNKNOWN (not FAILED) on timeout
# ---------------------------------------------------------------------------

class TestConfirmOrderPlaced:
    """Verify confirm_order_placed never returns FAILED."""

    @pytest.mark.asyncio
    async def test_base_returns_unknown(self):
        handler = BaseCheckoutHandler()
        result = await handler.confirm_order_placed(MagicMock())
        assert result.success is True
        assert result.message == "unknown"
        assert result.step_name == "confirm_order_placed"

    @pytest.mark.asyncio
    async def test_target_returns_unknown_on_timeout(self):
        from pmon.checkout_flows.target import TargetCheckoutHandler

        handler = TargetCheckoutHandler()
        page = AsyncMock()
        page.url = "https://www.target.com/checkout"

        # Mock body text with no confirmation signals
        body_loc = AsyncMock()
        body_loc.inner_text = AsyncMock(return_value="Loading...")
        page.locator = MagicMock(return_value=body_loc)

        # Patch _wait_for_confirmation_signals to return quickly
        handler._wait_for_confirmation_signals = AsyncMock(return_value=(False, ""))

        result = await handler.confirm_order_placed(page)
        assert result.success is True
        assert result.message == "unknown"

    @pytest.mark.asyncio
    async def test_walmart_returns_unknown_on_timeout(self):
        from pmon.checkout_flows.walmart import WalmartCheckoutHandler

        handler = WalmartCheckoutHandler()
        handler._wait_for_confirmation_signals = AsyncMock(return_value=(False, ""))

        page = AsyncMock()
        result = await handler.confirm_order_placed(page)
        assert result.success is True
        assert result.message == "unknown"


# ---------------------------------------------------------------------------
# Price guard triggers on review_order
# ---------------------------------------------------------------------------

class TestPriceGuard:
    """Verify price guard on review_order step."""

    @pytest.mark.asyncio
    async def test_price_guard_triggers(self):
        handler = BaseCheckoutHandler(max_price=50.0)
        # Mock _extract_order_total to return a price above max
        handler._extract_order_total = AsyncMock(return_value=75.00)
        handler._screenshot = AsyncMock(return_value=None)

        page = MagicMock()
        result = await handler.review_order(page)
        assert result.success is False
        assert result.message == "price_exceeded"

    @pytest.mark.asyncio
    async def test_price_guard_passes(self):
        handler = BaseCheckoutHandler(max_price=100.0)
        handler._extract_order_total = AsyncMock(return_value=49.99)
        handler._screenshot = AsyncMock(return_value=None)

        page = MagicMock()
        result = await handler.review_order(page)
        assert result.success is True
        assert "49.99" in result.message

    @pytest.mark.asyncio
    async def test_no_max_price_always_passes(self):
        handler = BaseCheckoutHandler(max_price=0)
        handler._extract_order_total = AsyncMock(return_value=999.99)
        handler._screenshot = AsyncMock(return_value=None)

        page = MagicMock()
        result = await handler.review_order(page)
        assert result.success is True


# ---------------------------------------------------------------------------
# Runner dispatches to correct handler
# ---------------------------------------------------------------------------

class TestRunnerDispatch:
    """Verify runner dispatches to the correct handler class."""

    def test_dispatch_target(self):
        from pmon.checkout_flows.target import TargetCheckoutHandler
        handler = get_handler("target")
        assert isinstance(handler, TargetCheckoutHandler)
        assert handler.retailer_name == "target"

    def test_dispatch_walmart(self):
        from pmon.checkout_flows.walmart import WalmartCheckoutHandler
        handler = get_handler("walmart")
        assert isinstance(handler, WalmartCheckoutHandler)
        assert handler.retailer_name == "walmart"

    def test_dispatch_pokemoncenter(self):
        from pmon.checkout_flows.pokemoncenter import PokemonCenterCheckoutHandler
        handler = get_handler("pokemoncenter")
        assert isinstance(handler, PokemonCenterCheckoutHandler)
        assert handler.retailer_name == "pokemoncenter"

    def test_dispatch_bestbuy(self):
        from pmon.checkout_flows.bestbuy import BestBuyCheckoutHandler
        handler = get_handler("bestbuy")
        assert isinstance(handler, BestBuyCheckoutHandler)

    def test_dispatch_costco(self):
        from pmon.checkout_flows.costco import CostcoCheckoutHandler
        handler = get_handler("costco")
        assert isinstance(handler, CostcoCheckoutHandler)

    def test_dispatch_samsclub(self):
        from pmon.checkout_flows.samsclub import SamsClubCheckoutHandler
        handler = get_handler("samsclub")
        assert isinstance(handler, SamsClubCheckoutHandler)

    def test_dispatch_unknown_raises(self):
        with pytest.raises(ValueError, match="No checkout handler"):
            get_handler("unknown_retailer")

    def test_dispatch_case_insensitive(self):
        handler = get_handler("Target")
        assert handler.retailer_name == "target"


# ---------------------------------------------------------------------------
# Legacy compatibility
# ---------------------------------------------------------------------------

class TestLegacyCompatibility:
    """Verify flow_result_to_legacy mapping."""

    def test_placed_maps_to_success(self):
        flow = CheckoutFlowResult(
            url="https://example.com", retailer="target",
            product_name="Test", status=CheckoutStatus.PLACED,
            order_number="T-123",
        )
        legacy = flow_result_to_legacy(flow)
        assert legacy.status == LegacyCheckoutStatus.SUCCESS
        assert legacy.order_number == "T-123"

    def test_unknown_maps_to_success(self):
        flow = CheckoutFlowResult(
            url="", retailer="walmart", product_name="",
            status=CheckoutStatus.UNKNOWN,
        )
        legacy = flow_result_to_legacy(flow)
        assert legacy.status == LegacyCheckoutStatus.SUCCESS

    def test_failed_maps_to_failed(self):
        flow = CheckoutFlowResult(
            url="", retailer="target", product_name="",
            status=CheckoutStatus.FAILED,
            error_message="Something broke",
        )
        legacy = flow_result_to_legacy(flow)
        assert legacy.status == LegacyCheckoutStatus.FAILED
        assert legacy.error_message == "Something broke"

    def test_price_exceeded_maps_to_failed(self):
        flow = CheckoutFlowResult(
            url="", retailer="target", product_name="",
            status=CheckoutStatus.PRICE_EXCEEDED,
        )
        legacy = flow_result_to_legacy(flow)
        assert legacy.status == LegacyCheckoutStatus.FAILED

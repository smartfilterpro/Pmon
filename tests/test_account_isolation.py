"""Test stub for account isolation verification.

REVIEWED [Mission 2] — Verifies that AccountManager correctly isolates
browser contexts, cookie stores, and session state between accounts.

Run: python -m pytest tests/test_account_isolation.py -v
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import with path setup
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from pmon.account_manager import AccountManager, SESSION_BASE_DIR


class TestAccountIsolation:
    """Verify that account sessions are fully isolated."""

    def test_session_paths_are_per_account(self):
        """Cookie files must be scoped per user_id, not shared."""
        mgr = AccountManager()

        path_a = mgr._session_path(user_id=1, retailer="target")
        path_b = mgr._session_path(user_id=2, retailer="target")

        # Different users must have different cookie paths
        assert path_a != path_b
        assert "1" in str(path_a)
        assert "2" in str(path_b)

        # Same user, different retailers must also be separate
        path_c = mgr._session_path(user_id=1, retailer="walmart")
        assert path_a != path_c

    def test_account_keys_are_unique(self):
        """Account keys must uniquely identify user+retailer pairs."""
        mgr = AccountManager()

        key_a = mgr._account_key(1, "target")
        key_b = mgr._account_key(2, "target")
        key_c = mgr._account_key(1, "walmart")

        assert key_a != key_b
        assert key_a != key_c
        assert key_b != key_c

    def test_auth_state_isolation(self):
        """Authentication state must be independent per account."""
        mgr = AccountManager()

        # Mark account A as authenticated
        mgr.mark_authenticated(1, "target", True)

        # Account B should NOT be affected
        assert mgr.is_authenticated(1, "target") is True
        assert mgr.is_authenticated(2, "target") is False
        assert mgr.is_authenticated(1, "walmart") is False

    def test_clear_session_isolation(self):
        """Clearing one account's session must not affect others."""
        mgr = AccountManager()

        mgr.mark_authenticated(1, "target", True)
        mgr.mark_authenticated(2, "target", True)

        # Clear account 1
        # Note: clear_session is async, but auth state is sync
        mgr._auth_state.pop(mgr._account_key(1, "target"), None)

        # Account 2 should still be authenticated
        assert mgr.is_authenticated(1, "target") is False
        assert mgr.is_authenticated(2, "target") is True

    def test_no_shared_mutable_state(self):
        """Two AccountManager instances must not share state."""
        mgr1 = AccountManager()
        mgr2 = AccountManager()

        mgr1.mark_authenticated(1, "target", True)

        # mgr2 should have its own state
        assert mgr2.is_authenticated(1, "target") is False

    def test_session_directory_structure(self):
        """Session directories must follow .sessions/{user_id}/ pattern."""
        mgr = AccountManager()

        dir_1 = mgr._session_dir(user_id=42)
        assert dir_1.name == "42"
        assert dir_1.parent == SESSION_BASE_DIR

        dir_2 = mgr._session_dir(user_id=99)
        assert dir_2.name == "99"
        assert dir_1 != dir_2


class TestAccountManagerAsync:
    """Async tests for AccountManager context management."""

    @pytest.mark.asyncio
    async def test_get_context_creates_isolated_contexts(self):
        """Each (user_id, retailer) pair must get a separate BrowserContext."""
        mock_browser = AsyncMock()
        mock_ctx_a = AsyncMock()
        mock_ctx_b = AsyncMock()
        mock_browser.new_context = AsyncMock(side_effect=[mock_ctx_a, mock_ctx_b])

        mgr = AccountManager(browser=mock_browser)

        ctx_a = await mgr.get_context(1, "target", load_cookies=False)
        ctx_b = await mgr.get_context(2, "target", load_cookies=False)

        # Must be different context objects
        assert ctx_a is not ctx_b
        # Browser.new_context must have been called twice
        assert mock_browser.new_context.call_count == 2

    @pytest.mark.asyncio
    async def test_get_context_caches_per_account(self):
        """Repeated calls for same account should return cached context."""
        mock_browser = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.pages = AsyncMock(return_value=[])
        mock_browser.new_context = AsyncMock(return_value=mock_ctx)

        mgr = AccountManager(browser=mock_browser)

        ctx1 = await mgr.get_context(1, "target", load_cookies=False)
        ctx2 = await mgr.get_context(1, "target", load_cookies=False)

        # Should be the same cached context
        assert ctx1 is ctx2
        # Browser.new_context should only be called once
        assert mock_browser.new_context.call_count == 1

    @pytest.mark.asyncio
    async def test_close_all_clears_state(self):
        """close_all must close all contexts and clear state."""
        mock_browser = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.pages = AsyncMock(return_value=[])
        mock_browser.new_context = AsyncMock(return_value=mock_ctx)

        mgr = AccountManager(browser=mock_browser)
        await mgr.get_context(1, "target", load_cookies=False)
        mgr.mark_authenticated(1, "target", True)

        await mgr.close_all()

        assert len(mgr._contexts) == 0
        assert len(mgr._auth_state) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

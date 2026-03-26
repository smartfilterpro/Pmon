"""Tests for the pmon.login module."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pmon.login.base import LoginResult, LoginStatus
from pmon.login.runner import LoginRunner


@dataclass
class FakeCredentials:
    email: str = "test@example.com"
    password: str = "hunter2"
    user_id: int = 42


@pytest.fixture
def credentials():
    return FakeCredentials()


@pytest.fixture
def mock_page():
    page = AsyncMock()
    page.url = "https://www.target.com"
    page.context = AsyncMock()
    page.context.cookies = AsyncMock(return_value=[])
    loc = AsyncMock()
    loc.first = AsyncMock()
    loc.first.is_visible = AsyncMock(return_value=False)
    loc.first.wait_for = AsyncMock()
    loc.first.input_value = AsyncMock(return_value="test@example.com")
    loc.first.inner_text = AsyncMock(return_value="Account")
    page.locator.return_value = loc
    role = AsyncMock()
    role.first = AsyncMock()
    role.first.is_visible = AsyncMock(return_value=False)
    page.get_by_role = MagicMock(return_value=role)
    txt = AsyncMock()
    txt.first = AsyncMock()
    txt.first.is_visible = AsyncMock(return_value=False)
    page.get_by_text = MagicMock(return_value=txt)
    page.goto = AsyncMock()
    page.reload = AsyncMock()
    page.screenshot = AsyncMock(return_value=b"\x89PNG")
    page.evaluate = AsyncMock(return_value=None)
    return page


@pytest.fixture
def mock_account_manager():
    mgr = MagicMock()
    mgr.is_authenticated = MagicMock(return_value=False)
    mgr.save_session = AsyncMock()
    mgr.mark_authenticated = MagicMock()
    return mgr


#LoginResult tests

class TestLoginResult:
    def test_creation(self):
        result = LoginResult(
            status=LoginStatus.SUCCESS,
            retailer="target",
            user_id=1,
            session_saved=True,
            duration_ms=1234,
        )
        assert result.status == LoginStatus.SUCCESS
        assert result.retailer == "target"
        assert result.user_id == 1
        assert result.session_saved is True
        assert result.duration_ms == 1234

    def test_ok_property_success(self):
        result = LoginResult(status=LoginStatus.SUCCESS, retailer="target")
        assert result.ok is True

    def test_ok_property_session_reused(self):
        result = LoginResult(status=LoginStatus.SESSION_REUSED, retailer="target")
        assert result.ok is True

    def test_ok_property_failed(self):
        result = LoginResult(status=LoginStatus.FAILED, retailer="target")
        assert result.ok is False

    def test_ok_property_blocked(self):
        result = LoginResult(status=LoginStatus.BLOCKED, retailer="target")
        assert result.ok is False

    def test_ok_property_captcha(self):
        result = LoginResult(status=LoginStatus.CAPTCHA, retailer="target")
        assert result.ok is False

    def test_ok_property_requires_2fa(self):
        result = LoginResult(status=LoginStatus.REQUIRES_2FA, retailer="target")
        assert result.ok is False

    def test_defaults(self):
        result = LoginResult(status=LoginStatus.FAILED, retailer="walmart")
        assert result.user_id is None
        assert result.session_saved is False
        assert result.failure_reason is None
        assert result.screenshot_b64 is None
        assert result.duration_ms == 0


#LoginStatus tests

class TestLoginStatus:
    def test_all_values(self):
        expected = {"success", "failed", "blocked", "requires_2fa", "captcha", "session_reused"}
        actual = {s.value for s in LoginStatus}
        assert actual == expected

    def test_enum_count(self):
        assert len(LoginStatus) == 6


#LoginRunner tests

class TestLoginRunner:
    @pytest.mark.asyncio
    async def test_session_reused_when_authenticated(self, mock_page, credentials, mock_account_manager):
        mock_account_manager.is_authenticated.return_value = True
        runner = LoginRunner(mock_account_manager)

        result = await runner.run("target", mock_page, credentials, user_id=42)

        assert result.status == LoginStatus.SESSION_REUSED
        assert result.retailer == "target"
        assert result.user_id == 42

    @pytest.mark.asyncio
    async def test_force_skips_auth_check(self, mock_page, credentials, mock_account_manager):
        mock_account_manager.is_authenticated.return_value = True
        runner = LoginRunner(mock_account_manager)

        # Patch the handler to return a controlled result
        with patch("pmon.login.runner._get_registry") as mock_reg:
            mock_handler = AsyncMock()
            mock_handler.return_value.login = AsyncMock(return_value=LoginResult(
                status=LoginStatus.SUCCESS, retailer="target", user_id=42,
            ))
            mock_handler.return_value.retailer = "target"
            mock_reg.return_value = {"target": mock_handler}

            result = await runner.run("target", mock_page, credentials, user_id=42, force=True)

        assert result.status != LoginStatus.SESSION_REUSED

    @pytest.mark.asyncio
    async def test_unknown_retailer_returns_failed(self, mock_page, credentials, mock_account_manager):
        runner = LoginRunner(mock_account_manager)

        result = await runner.run("unknown_store", mock_page, credentials, user_id=1)

        assert result.status == LoginStatus.FAILED
        assert "No login handler" in result.failure_reason

    @pytest.mark.asyncio
    async def test_dispatches_to_correct_handler(self, mock_page, credentials, mock_account_manager):
        runner = LoginRunner(mock_account_manager)

        with patch("pmon.login.runner._get_registry") as mock_reg:
            mock_handler_cls = MagicMock()
            mock_handler_inst = AsyncMock()
            mock_handler_inst.login = AsyncMock(return_value=LoginResult(
                status=LoginStatus.SUCCESS, retailer="walmart", user_id=42,
            ))
            mock_handler_cls.return_value = mock_handler_inst
            mock_reg.return_value = {"walmart": mock_handler_cls}

            result = await runner.run("walmart", mock_page, credentials, user_id=42)

        mock_handler_cls.assert_called_once_with(vision_helper=None)
        mock_handler_inst.login.assert_called_once()
        assert result.status == LoginStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_saves_session_on_success(self, mock_page, credentials, mock_account_manager):
        runner = LoginRunner(mock_account_manager)

        with patch("pmon.login.runner._get_registry") as mock_reg:
            mock_handler_cls = MagicMock()
            mock_handler_inst = AsyncMock()
            mock_handler_inst.login = AsyncMock(return_value=LoginResult(
                status=LoginStatus.SUCCESS, retailer="target", user_id=42,
            ))
            mock_handler_cls.return_value = mock_handler_inst
            mock_reg.return_value = {"target": mock_handler_cls}

            result = await runner.run("target", mock_page, credentials, user_id=42)

        mock_account_manager.save_session.assert_called_once_with(42, "target", mock_page.context)
        mock_account_manager.mark_authenticated.assert_called_once_with(42, "target", authenticated=True)
        assert result.session_saved is True

    @pytest.mark.asyncio
    async def test_no_save_on_failure(self, mock_page, credentials, mock_account_manager):
        runner = LoginRunner(mock_account_manager)

        with patch("pmon.login.runner._get_registry") as mock_reg:
            mock_handler_cls = MagicMock()
            mock_handler_inst = AsyncMock()
            mock_handler_inst.login = AsyncMock(return_value=LoginResult(
                status=LoginStatus.FAILED, retailer="target", user_id=42,
                failure_reason="test failure",
            ))
            mock_handler_cls.return_value = mock_handler_inst
            mock_reg.return_value = {"target": mock_handler_cls}

            result = await runner.run("target", mock_page, credentials, user_id=42)

        mock_account_manager.save_session.assert_not_called()
        assert result.session_saved is False

    def test_get_handler(self, mock_account_manager):
        runner = LoginRunner(mock_account_manager)
        handler = runner.get_handler("target")
        assert handler is not None
        assert handler.retailer == "target"

    def test_get_handler_unknown(self, mock_account_manager):
        runner = LoginRunner(mock_account_manager)
        handler = runner.get_handler("nonexistent")
        assert handler is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_target_login_live():
    pytest.skip("Integration test — requires live browser and credentials")

@pytest.mark.integration
@pytest.mark.asyncio
async def test_walmart_login_live():
    pytest.skip("Integration test — requires live browser and credentials")

@pytest.mark.integration
@pytest.mark.asyncio
async def test_pokemoncenter_login_live():
    pytest.skip("Integration test — requires live browser and credentials")

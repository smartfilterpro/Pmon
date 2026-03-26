"""Base classes and data types for the login module.

Defines the LoginStatus enum, LoginResult dataclass, and the abstract
BaseLoginHandler that every retailer-specific handler must implement.
"""

from __future__ import annotations

import abc
import base64
import enum
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class LoginStatus(enum.Enum):
    """Outcome of a login attempt."""

    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"
    REQUIRES_2FA = "requires_2fa"
    CAPTCHA = "captcha"
    SESSION_REUSED = "session_reused"


@dataclass
class LoginResult:
    """Immutable record of a single login attempt."""

    status: LoginStatus
    retailer: str
    user_id: int | None = None
    session_saved: bool = False
    failure_reason: str | None = None
    screenshot_b64: str | None = None
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        """Return *True* when login succeeded or session was reused."""
        return self.status in (LoginStatus.SUCCESS, LoginStatus.SESSION_REUSED)


class BaseLoginHandler(abc.ABC):
    """Abstract base for retailer login handlers.

    Subclasses must implement :meth:`login` and :meth:`verify_authenticated`.
    The default :meth:`handle_obstacles` delegates to the three private
    detection helpers and can be overridden for retailer-specific logic.
    """

    retailer: str = "unknown"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def login(self, page, credentials, **kwargs) -> LoginResult:
        """Execute the full login flow on *page* using *credentials*.

        Parameters
        ----------
        page : playwright.async_api.Page
        credentials : object with ``email`` and ``password`` attributes
        """

    @abc.abstractmethod
    async def verify_authenticated(self, page) -> bool:
        """Return *True* if the page indicates an authenticated session."""

    async def handle_obstacles(self, page) -> bool:
        """Detect and (if possible) dismiss obstacles.

        Returns *True* if an obstacle was found — the caller decides
        whether to abort or retry.
        """
        if await self._detect_captcha(page):
            logger.warning("%s login: CAPTCHA detected", self.retailer)
            return True
        if await self._detect_2fa(page):
            logger.warning("%s login: 2FA prompt detected", self.retailer)
            return True
        if await self._detect_account_locked(page):
            logger.warning("%s login: account locked / disabled", self.retailer)
            return True
        return False

    # ------------------------------------------------------------------
    # Detection helpers (overridable per-retailer)
    # ------------------------------------------------------------------

    async def _detect_captcha(self, page) -> bool:
        """Check for reCAPTCHA, hCaptcha, or PerimeterX challenge."""
        captcha_selectors = [
            'iframe[src*="recaptcha"]',
            'iframe[src*="hcaptcha"]',
            '#px-captcha',
            '[class*="captcha" i]',
            'iframe[title*="challenge" i]',
        ]
        for sel in captcha_selectors:
            try:
                if await page.locator(sel).first.is_visible(timeout=500):
                    return True
            except Exception:
                continue
        return False

    async def _detect_2fa(self, page) -> bool:
        """Check for two-factor / verification prompts."""
        twofa_texts = [
            "verification code", "two-factor", "2-step",
            "verify your identity", "enter the code",
            "security code", "one-time password",
        ]
        for text in twofa_texts:
            try:
                if await page.get_by_text(text, exact=False).first.is_visible(timeout=300):
                    return True
            except Exception:
                continue
        return False

    async def _detect_account_locked(self, page) -> bool:
        """Check for account-locked or disabled messaging."""
        locked_texts = [
            "account has been locked", "account is locked",
            "account disabled", "temporarily locked",
            "too many attempts", "account suspended",
        ]
        for text in locked_texts:
            try:
                if await page.get_by_text(text, exact=False).first.is_visible(timeout=300):
                    return True
            except Exception:
                continue
        return False

    async def _screenshot(self, page) -> str | None:
        """Capture a base64-encoded PNG screenshot of *page*."""
        try:
            png_bytes = await page.screenshot(type="png")
            return base64.b64encode(png_bytes).decode("ascii")
        except Exception:
            logger.debug("%s login: screenshot capture failed", self.retailer)
            return None

    def _make_result(
        self,
        status: LoginStatus,
        *,
        user_id: int | None = None,
        session_saved: bool = False,
        failure_reason: str | None = None,
        screenshot_b64: str | None = None,
        start_time: float = 0.0,
    ) -> LoginResult:
        """Convenience factory for ``LoginResult``."""
        duration = int((time.monotonic() - start_time) * 1000) if start_time else 0
        return LoginResult(
            status=status,
            retailer=self.retailer,
            user_id=user_id,
            session_saved=session_saved,
            failure_reason=failure_reason,
            screenshot_b64=screenshot_b64,
            duration_ms=duration,
        )

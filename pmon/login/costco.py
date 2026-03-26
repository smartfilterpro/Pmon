"""Costco.com login handler (stub).

TODO [Mission 1]: Implement Costco login flow.
  - Navigate to costco.com/LogonForm
  - Fill logonId and logonPassword fields
  - Handle CAPTCHA (Costco uses Akamai Bot Manager)
  - Verify via account menu or membership indicator
"""

from __future__ import annotations

import logging
import time

from pmon.login.base import BaseLoginHandler, LoginResult, LoginStatus

logger = logging.getLogger(__name__)


class CostcoLoginHandler(BaseLoginHandler):
    """Handle Costco.com authentication (stub — not yet implemented)."""

    retailer = "costco"

    async def login(self, page, credentials, **kwargs) -> LoginResult:
        """Costco login — not yet implemented."""
        start = time.monotonic()
        user_id = getattr(credentials, "user_id", None)
        logger.warning("Costco login handler is a stub — not yet implemented")
        return self._make_result(
            LoginStatus.FAILED,
            user_id=user_id,
            start_time=start,
            failure_reason="Costco login not yet implemented",
        )

    async def verify_authenticated(self, page) -> bool:
        """Costco auth verification — not yet implemented."""
        # TODO: Check for account/membership indicators
        return False

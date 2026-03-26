"""Sam's Club login handler (stub).

TODO [Mission 1]: Implement Sam's Club login flow.
  - Navigate to samsclub.com/sams/account/signin
  - Fill membershipNum/email and password fields
  - Handle CAPTCHA (Sam's uses PerimeterX)
  - Verify via account greeting or membership badge
"""

from __future__ import annotations

import logging
import time

from pmon.login.base import BaseLoginHandler, LoginResult, LoginStatus

logger = logging.getLogger(__name__)


class SamsClubLoginHandler(BaseLoginHandler):
    """Handle Sam's Club authentication (stub — not yet implemented)."""

    retailer = "samsclub"

    async def login(self, page, credentials, **kwargs) -> LoginResult:
        """Sam's Club login — not yet implemented."""
        start = time.monotonic()
        user_id = getattr(credentials, "user_id", None)
        logger.warning("Sam's Club login handler is a stub — not yet implemented")
        return self._make_result(
            LoginStatus.FAILED,
            user_id=user_id,
            start_time=start,
            failure_reason="Sam's Club login not yet implemented",
        )

    async def verify_authenticated(self, page) -> bool:
        """Sam's Club auth verification — not yet implemented."""
        # TODO: Check for account greeting or membership badge
        return False

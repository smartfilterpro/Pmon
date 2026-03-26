"""Login module — retailer-specific authentication handlers.

Extracted from checkout/engine.py as part of Mission 1.  Each retailer
gets its own handler class that inherits from BaseLoginHandler.

Usage:
    from pmon.login import LoginRunner, LoginStatus, LoginResult
    from pmon.login.target import TargetLoginHandler

    runner = LoginRunner(account_manager)
    result = await runner.run("target", page, credentials)
"""

from __future__ import annotations

from pmon.login.base import BaseLoginHandler, LoginResult, LoginStatus
from pmon.login.runner import LoginRunner

__all__ = [
    "BaseLoginHandler",
    "LoginResult",
    "LoginRunner",
    "LoginStatus",
]

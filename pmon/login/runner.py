"""Login runner — dispatches to the correct retailer handler.

Coordinates session checks via AccountManager before delegating to
the retailer-specific handler.  On success, persists session state.
"""

from __future__ import annotations

import logging

from pmon.login.base import BaseLoginHandler, LoginResult, LoginStatus

logger = logging.getLogger(__name__)

# Lazy imports to avoid circular dependencies at module level.
_HANDLER_REGISTRY: dict[str, type[BaseLoginHandler]] | None = None


def _get_registry() -> dict[str, type[BaseLoginHandler]]:
    """Build the handler registry on first access."""
    global _HANDLER_REGISTRY
    if _HANDLER_REGISTRY is not None:
        return _HANDLER_REGISTRY

    from pmon.login.bestbuy import BestBuyLoginHandler
    from pmon.login.costco import CostcoLoginHandler
    from pmon.login.pokemoncenter import PokemonCenterLoginHandler
    from pmon.login.samsclub import SamsClubLoginHandler
    from pmon.login.target import TargetLoginHandler
    from pmon.login.walmart import WalmartLoginHandler

    _HANDLER_REGISTRY = {
        "target": TargetLoginHandler,
        "walmart": WalmartLoginHandler,
        "pokemoncenter": PokemonCenterLoginHandler,
        "bestbuy": BestBuyLoginHandler,
        "costco": CostcoLoginHandler,
        "samsclub": SamsClubLoginHandler,
    }
    return _HANDLER_REGISTRY


class LoginRunner:
    """Orchestrate login: check session, dispatch handler, persist results.

    Parameters
    ----------
    account_manager : AccountManager
        Used to check/save authentication state.
    vision_helper : optional
        Object with ``_smart_click``, ``_smart_fill``, ``_smart_sign_in``
        methods, passed through to handlers that support vision fallback.
    """

    def __init__(self, account_manager, *, vision_helper=None):
        self._acct = account_manager
        self._vision = vision_helper

    async def run(
        self,
        retailer: str,
        page,
        credentials,
        *,
        user_id: int | None = None,
        force: bool = False,
    ) -> LoginResult:
        """Run the login flow for *retailer*.

        Parameters
        ----------
        retailer : retailer slug (e.g. "target", "walmart")
        page : Playwright Page object
        credentials : object with ``email`` and ``password`` attributes
        user_id : account identifier for session tracking
        force : skip the ``is_authenticated`` check and always log in
        """
        uid = user_id or getattr(credentials, "user_id", None)

        # Step 1 — check if already authenticated
        if not force and uid is not None and self._acct.is_authenticated(uid, retailer):
            logger.info("Login runner: %s user %s already authenticated — reusing session", retailer, uid)
            return LoginResult(
                status=LoginStatus.SESSION_REUSED,
                retailer=retailer,
                user_id=uid,
                session_saved=False,
            )

        # Step 2 — get handler
        registry = _get_registry()
        handler_cls = registry.get(retailer)
        if handler_cls is None:
            return LoginResult(
                status=LoginStatus.FAILED,
                retailer=retailer,
                user_id=uid,
                failure_reason=f"No login handler for retailer: {retailer}",
            )

        handler = handler_cls(vision_helper=self._vision)
        logger.info("Login runner: dispatching to %s handler for user %s", retailer, uid)

        # Step 3 — execute login
        result = await handler.login(page, credentials, user_id=uid)

        # Step 4 — on success, persist session and mark authenticated
        if result.ok and uid is not None:
            try:
                await self._acct.save_session(uid, retailer, page.context)
                result = LoginResult(
                    status=result.status,
                    retailer=result.retailer,
                    user_id=result.user_id,
                    session_saved=True,
                    failure_reason=result.failure_reason,
                    screenshot_b64=result.screenshot_b64,
                    duration_ms=result.duration_ms,
                )
                self._acct.mark_authenticated(uid, retailer, authenticated=True)
                logger.info("Login runner: %s session saved for user %s", retailer, uid)
            except Exception:
                logger.warning("Login runner: failed to save %s session for user %s", retailer, uid, exc_info=True)

        return result

    def get_handler(self, retailer: str, **kwargs) -> BaseLoginHandler | None:
        """Return a handler instance for *retailer* (or None)."""
        registry = _get_registry()
        handler_cls = registry.get(retailer)
        if handler_cls is None:
            return None
        return handler_cls(vision_helper=self._vision, **kwargs)

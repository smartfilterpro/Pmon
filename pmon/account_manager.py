"""Account isolation manager for multi-account browser sessions.

REVIEWED [Mission 2] — Each retailer account gets its own browser context,
cookie jar, and session state. No shared mutable state between accounts.

Updating, refreshing, or invalidating Account A has zero effect on Account B.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from pmon import database as db

logger = logging.getLogger(__name__)

# Per-account session storage directory
SESSION_BASE_DIR = Path(__file__).parent.parent / ".sessions"


class AccountManager:
    """Manages isolated browser contexts and sessions per account.

    Each account (identified by user_id + retailer) gets:
    - Its own Playwright BrowserContext (never shared)
    - Its own cookie persistence file: .sessions/{user_id}/{retailer}.json
    - Its own error tracking state

    No global page or context variables are used.
    """

    def __init__(self, browser=None):
        self._browser = browser
        # Cache of active contexts: {account_key: BrowserContext}
        self._contexts: dict[str, object] = {}
        # Track authentication state per account
        self._auth_state: dict[str, bool] = {}

    def _account_key(self, user_id: int, retailer: str) -> str:
        """Generate a unique key for an account."""
        return f"{user_id}:{retailer}"

    def _session_dir(self, user_id: int) -> Path:
        """Get the session directory for a specific user."""
        path = SESSION_BASE_DIR / str(user_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _session_path(self, user_id: int, retailer: str) -> Path:
        """Get the cookie file path for a specific account."""
        return self._session_dir(user_id) / f"{retailer}.json"

    async def get_context(
        self,
        user_id: int,
        retailer: str,
        *,
        load_cookies: bool = True,
        stealth_js: str = "",
        context_kwargs: dict | None = None,
    ):
        """Get or create an isolated BrowserContext for an account.

        Each call with the same (user_id, retailer) returns the cached context
        if it's still open, or creates a new one.

        Parameters
        ----------
        user_id : account owner
        retailer : retailer slug (e.g. "target", "walmart")
        load_cookies : whether to load persisted cookies
        stealth_js : JavaScript to inject via add_init_script
        context_kwargs : extra kwargs for browser.new_context()
        """
        if not self._browser:
            raise RuntimeError("AccountManager has no browser instance. Call set_browser() first.")

        key = self._account_key(user_id, retailer)

        # Return cached context if still open
        if key in self._contexts:
            ctx = self._contexts[key]
            try:
                # Check if context is still usable
                await ctx.pages  # noqa — just test accessibility
                return ctx
            except Exception:
                # Context was closed, remove from cache
                del self._contexts[key]

        # Build context kwargs
        kwargs = dict(context_kwargs or {})
        storage_path = self._session_path(user_id, retailer)
        if load_cookies and storage_path.exists():
            kwargs["storage_state"] = str(storage_path)

        # Create new isolated context
        context = await self._browser.new_context(**kwargs)

        if stealth_js:
            await context.add_init_script(stealth_js)

        self._contexts[key] = context
        logger.info(
            "AccountManager: created context for user %d, retailer %s "
            "(cookies: %s)",
            user_id, retailer, "loaded" if load_cookies and storage_path.exists() else "none",
        )
        return context

    async def save_session(self, user_id: int, retailer: str, context=None):
        """Save browser cookies/state for an account.

        Persists to both:
        1. File: .sessions/{user_id}/{retailer}.json (for browser context reload)
        2. Database: retailer_sessions table (for API checkout and cross-restart persistence)
        """
        key = self._account_key(user_id, retailer)
        ctx = context or self._contexts.get(key)
        if not ctx:
            logger.warning("AccountManager: no context to save for %s", key)
            return

        storage_path = self._session_path(user_id, retailer)
        try:
            # Save Playwright storage state (cookies + localStorage)
            await ctx.storage_state(path=str(storage_path))
            logger.info("AccountManager: saved session for user %d, %s", user_id, retailer)

            # Also persist cookies to database for API checkout
            try:
                state = json.loads(storage_path.read_text())
                cookies_dict = {}
                for cookie in state.get("cookies", []):
                    cookies_dict[cookie["name"]] = cookie["value"]
                if cookies_dict:
                    db.set_retailer_session(
                        user_id, retailer,
                        cookies_json=json.dumps(cookies_dict),
                    )
            except Exception as exc:
                logger.debug("AccountManager: DB session save failed for %s: %s", key, exc)

        except Exception as exc:
            logger.error("AccountManager: failed to save session for %s: %s", key, exc)

    async def clear_session(self, user_id: int, retailer: str):
        """Clear all session data for an account (cookies, context, DB).

        This operation affects ONLY the specified account.
        """
        key = self._account_key(user_id, retailer)

        # Close and remove cached context
        if key in self._contexts:
            try:
                await self._contexts[key].close()
            except Exception:
                pass
            del self._contexts[key]

        # Remove session file
        storage_path = self._session_path(user_id, retailer)
        if storage_path.exists():
            storage_path.unlink()

        # Clear from database
        try:
            db.delete_retailer_session(user_id, retailer)
        except Exception:
            pass

        # Clear auth state
        self._auth_state.pop(key, None)

        logger.info("AccountManager: cleared session for user %d, %s", user_id, retailer)

    def is_authenticated(self, user_id: int, retailer: str) -> bool:
        """Check if an account is marked as authenticated."""
        key = self._account_key(user_id, retailer)
        return self._auth_state.get(key, False)

    def mark_authenticated(self, user_id: int, retailer: str, authenticated: bool = True):
        """Mark an account's authentication state."""
        key = self._account_key(user_id, retailer)
        self._auth_state[key] = authenticated

    def set_browser(self, browser):
        """Set or update the browser instance."""
        self._browser = browser

    async def close_all(self):
        """Close all cached contexts."""
        for key, ctx in list(self._contexts.items()):
            try:
                await ctx.close()
            except Exception:
                pass
        self._contexts.clear()
        self._auth_state.clear()

    def get_active_accounts(self) -> list[str]:
        """Return list of active account keys."""
        return list(self._contexts.keys())

    async def load_db_cookies(self, user_id: int, retailer: str) -> dict:
        """Load session cookies from the database for a specific account.

        Returns a dict of {cookie_name: cookie_value}.
        """
        try:
            session = db.get_retailer_session(user_id, retailer)
            if session and session.get("cookies_json"):
                cookies = json.loads(session["cookies_json"])
                return cookies if isinstance(cookies, dict) else {}
        except Exception as exc:
            logger.debug(
                "AccountManager: failed to load DB cookies for user %d, %s: %s",
                user_id, retailer, exc,
            )
        return {}
